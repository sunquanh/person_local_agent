# -*- coding: utf-8 -*-
"""下一批优化复现测试：改前失败、改后通过。

#1 RAG 小语料检索全灭、#2 RAG 0 块刷新残留、#3 /api/jobs 全量 load 取标题、
#4 _settings 每次全树算 pending —— 复现测试。
#7 start.py 死代码兜底 —— 冒烟测试（只断言源码里已删，不证明运行行为）。
JS 项（#5 横幅切回立即查、#6 批量详情跳过重拉）无自动化测试，
用 node --check + 代码审查验证。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestSingleDocSearch(unittest.TestCase):
    def test_single_doc_search_returns_results(self):
        from agent.rag import Rag
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'ws'
            (root / 'inputs').mkdir(parents=True)
            (root / 'inputs' / 'ai.txt').write_text(
                '人工智能改变世界。人工智能很有用。' * 60, encoding='utf-8')
            rag = Rag(root)
            st = rag.refresh()
            self.assertGreater(st['chunks'], 0, '应先入库')
            res = rag.search('人工智能')
            self.assertTrue(res, '单文档也应能搜到包含查询词的块')


class TestZeroChunkRefresh(unittest.TestCase):
    def test_zero_chunk_refresh_clears_old_chunks(self):
        from agent.rag import Rag
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'ws'
            (root / 'inputs').mkdir(parents=True)
            p = root / 'inputs' / 'note.txt'
            p.write_text('有内容的文件。' * 100, encoding='utf-8')
            rag = Rag(root)
            st1 = rag.refresh()
            self.assertGreater(st1['chunks'], 0, '应先入库')
            self.assertTrue(rag.search('有内容'), '旧内容改前应能搜到')
            p.write_text('', encoding='utf-8')
            os.utime(p, (time.time() + 2, time.time() + 2))
            st2 = rag.refresh()
            self.assertEqual(st2['chunks'], 0, '清空后块数应为 0')
            row = rag.db.execute(
                "SELECT chunk_count FROM docs WHERE relpath='inputs/note.txt'"
            ).fetchone()
            self.assertIsNotNone(row, 'docs 行应保留（带新 mtime/size）')
            self.assertEqual(row[0], 0, 'chunk_count 应写回 0')
            self.assertEqual(rag.pending(), 0, '空文件已入库，不应永久报过期')
            st3 = rag.refresh()
            self.assertEqual(st3['updated'], 0, '再刷应收敛，不再判变化')
            self.assertEqual(rag.search('有内容'), [], '旧块不应再被搜到')


class TestJobsTitleSingleQuery(unittest.TestCase):
    def test_jobs_title_without_full_session_load(self):
        from server.httpd import Handler
        from server.sessions import SessionManager
        with tempfile.TemporaryDirectory() as d:
            sm = SessionManager(os.path.join(d, '.agent'))
            s = sm.create('长会话标题')
            for _ in range(50):
                sm.add_message(s.id, 'user', 'x' * 100)
            h = Handler.__new__(Handler)
            h.headers = {}
            # Handler.app 是只读 property，读 self.server.agent_server
            h.server = SimpleNamespace(agent_server=SimpleNamespace(
                _running_sid=s.id, sessions=sm, _jobs=[], _queue=[],
                _running=None, _running_owner='', _run_started=0.0,
                client_cookie_key=b'k'))
            captured = {}
            h._json = lambda obj: captured.setdefault('obj', obj)

            def boom_load(sid):
                raise AssertionError('不应全量 load 会话只取标题')

            sm.load = boom_load
            try:
                h._get_api_jobs({}, '')
            finally:
                # SessionManager.load 是实例上被替换的方法，恢复需删实例属性
                del sm.load
            self.assertEqual(captured['obj']['running_title'], '长会话标题')


class TestStatsCached(unittest.TestCase):
    def test_settings_stats_cached(self):
        from agent.rag import Rag
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'ws'
            (root / 'inputs').mkdir(parents=True)
            (root / 'inputs' / 'a.txt').write_text('hello world', encoding='utf-8')
            rag = Rag(root)
            rag.refresh()
            s1 = rag.stats()
            (root / 'inputs' / 'b.txt').write_text('another file here',
                                                   encoding='utf-8')
            s2 = rag.stats()
            self.assertEqual(s2['pending'], s1['pending'],
                             '短时间内 stats 应返回缓存，不应每次全树重算')
            rag.refresh()
            s3 = rag.stats()
            self.assertEqual(s3['pending'], 0, 'refresh 后缓存应失效，重新算出 0')


class TestStartPyDeadFallback(unittest.TestCase):
    def test_no_old_workspace_fallback(self):
        src = (Path(__file__).resolve().parent.parent / 'start.py').read_text(
            encoding='utf-8')
        self.assertNotIn('我的工作台', src, 'start.py 不应再有 ~/我的工作台 兜底')
        self.assertNotIn('_drive_missing', src, '_drive_missing 应随兜底一并删除')


if __name__ == '__main__':
    unittest.main()
