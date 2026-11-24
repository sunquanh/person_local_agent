# -*- coding: utf-8 -*-
"""6-15 批复现测试：先失败、修后通过。JS 项（6/12/13/14）用 node --check + 代码审查验证。"""
import io
import json
import logging
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestEstimateCache(unittest.TestCase):
    def test_repeat_estimate_hits_cache(self):
        from agent.core import estimate_messages, _estimate_tokens_str
        msgs = []
        for i in range(50):
            msgs.append({'role': 'assistant', 'content': '这是一段很长的中文回复内容' * 20,
                         'tool_calls': [{'name': 'read_file', 'args': {'path': f'/x/f{i}.py', 'n': i}}]})
        t1 = estimate_messages(msgs)
        info = _estimate_tokens_str.cache_info()
        t2 = estimate_messages(msgs)
        info2 = _estimate_tokens_str.cache_info()
        self.assertEqual(t1, t2)
        self.assertGreater(info2.hits, info.hits, '第二次估算应命中缓存')


class TestReadJsonBody(unittest.TestCase):
    def _handler(self, body: bytes):
        from server.httpd import Handler
        h = Handler.__new__(Handler)
        h.rfile = io.BytesIO(body)
        h.headers = {'Content-Length': str(len(body))}
        return h

    def test_parses_json(self):
        h = self._handler(b'{"a": 1}')
        self.assertEqual(h._read_json_body(), {'a': 1})

    def test_empty_body_is_empty_dict(self):
        h = self._handler(b'')
        self.assertEqual(h._read_json_body(), {})

    def test_limit_passthrough(self):
        h = self._handler(b'{"a": 1}')
        self.assertEqual(h._read_json_body(limit=2 * 1024 * 1024), {'a': 1})


class TestMemoryExtractLogs(unittest.TestCase):
    def test_extract_failure_logs_warning(self):
        from agent.memory import Memory
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            mem = Memory(os.path.join(d, 'm.db'))

            class Boom:
                def chat(self, *a, **k):
                    raise RuntimeError('llm down')

            # 直接挂 handler 等日志记录本身：工作线程可能在第一次轮询前就
            # 跑完（set 后立刻 clear），等 event 置位会误等满 5 秒。
            records = []

            class _H(logging.Handler):
                def emit(self, record):
                    records.append(record.getMessage())

            lg = logging.getLogger('agent.memory')
            h = _H()
            lg.addHandler(h)
            lg.setLevel(logging.WARNING)
            try:
                # 签名是 extract_summary(llm, session_id, title, history)
                mem.extract_summary(Boom(), 's1', 't', [{'role': 'user', 'content': 'hello world'}])
                for _ in range(100):
                    if records:
                        break
                    time.sleep(0.05)
                # 等线程收尾，避免日志写到别的测试里
                for _ in range(100):
                    if not mem._extracting.is_set():
                        break
                    time.sleep(0.05)
            finally:
                lg.removeHandler(h)
            self.assertTrue(any('llm down' in m or '记忆提取' in m for m in records),
                            f'应记录提取失败，实际: {records}')


class TestLoadConfigRemoved(unittest.TestCase):
    def test_dead_load_config_gone(self):
        import app
        self.assertFalse(hasattr(app, 'load_config'), '死代码 load_config 应删除')
        self.assertFalse(hasattr(app, 'DEFAULT_CONFIG'), '配套的 DEFAULT_CONFIG 应一并删除')


class TestWorkLogRotation(unittest.TestCase):
    def test_work_log_bounded(self):
        from server.sessions import SessionManager as SessionStore
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            # data_dir 放在临时子目录下：每日日志写到 data_dir.parent/notes，
            # 父目录也在临时目录内，不会漏到 /tmp 外面
            store = SessionStore(os.path.join(d, 'ws', '.agent'))
            sid = 'abcdef123456'  # create() 生成的 12 位 hex 格式
            big = 'x' * 5000
            for i in range(120):
                store.write_work_log(sid, f'第{i}轮', [], big)
            p = Path(d) / 'ws' / '.agent' / 'sessions' / sid / 'work_log.md'
            # _sdir 实际落点以 store 为准
            if not p.exists():
                found = list(Path(d).rglob('work_log.md'))
                self.assertTrue(found, '应写出 work_log.md')
                p = found[0]
            self.assertLess(p.stat().st_size, 300_000, f'会话日志应有上限，实际 {p.stat().st_size}')
            self.assertIn('第119轮', p.read_text(encoding='utf-8'), '最新一轮必须保留')
            # 每日日志同样有界且保留最新一轮
            daily = list((Path(d) / 'ws' / 'notes' / '工作日志').glob('*.md'))
            self.assertTrue(daily, '应写出每日日志')
            self.assertLess(daily[0].stat().st_size, 300_000,
                            f'每日日志应有上限，实际 {daily[0].stat().st_size}')
            self.assertIn('第119轮', daily[0].read_text(encoding='utf-8'), '每日日志最新一轮必须保留')


if __name__ == '__main__':
    unittest.main()
