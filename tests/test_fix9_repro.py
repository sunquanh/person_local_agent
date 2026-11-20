# -*- coding: utf-8 -*-
"""9 项缺陷复现（与 46 项原有单测分开，可单独跑）。

跑法：python3 -m unittest tests.test_fix9_repro -v
每项在旧代码上应失败（复现），修复后应通过。前端 JS 两项为静态检查，
只能证明代码含回滚/清理逻辑，不能替代浏览器实测——见最终报告残留风险。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.core import Agent  # noqa: E402
from agent.llm import LLMError  # noqa: E402
from agent.memory import Memory  # noqa: E402
from agent.workspace import SessionCtx, Workspace  # noqa: E402
from server.httpd import AgentHTTPServer, AgentServer, Handler  # noqa: E402
from server.runs import RunRegistry  # noqa: E402
from server.storepath import StorePathError, save_ui  # noqa: E402
from tests.test_backend import FakeLLM  # noqa: E402
from tests.test_runs_stop_undo import _config  # noqa: E402


class TestR1ConfigFakeSave(unittest.TestCase):
    def test_persist_failure_raises_and_rolls_back_memory(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg_path = tmp / 'ws' / '.agent' / 'config.json'
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        app = AgentServer(_config(tmp / 'ws'), config_path=str(cfg_path))
        self.addCleanup(app.batch.close)
        app.llm = FakeLLM('x')
        before_name = app.config['model']['name']
        # 先正常落一次盘，确认基线
        app.save_config(limits={'chat_timeout_s': 21})
        on_disk = json.loads(cfg_path.read_text(encoding='utf-8'))
        self.assertEqual(on_disk['limits']['chat_timeout_s'], 21)
        # 注入写盘失败：os.replace 抛错
        with mock.patch('server.httpd.os.replace',
                        side_effect=OSError('disk full (injected)')):
            with self.assertRaises(Exception, msg='写盘失败必须抛错，不能假保存'):
                app.save_config(model={'name': 'SHOULD-NOT-STICK',
                                       'base_url': 'http://127.0.0.1:9/v1',
                                       'api_key': '', 'thinking': 'hide',
                                       'max_tokens': 0, 'context_window': 8192})
        # 内存必须回滚，磁盘保持旧值
        self.assertEqual(app.config['model']['name'], before_name)
        on_disk2 = json.loads(cfg_path.read_text(encoding='utf-8'))
        self.assertEqual(on_disk2['model']['name'], before_name)


class _AbortLLM(FakeLLM):
    """run() 被调用后等放行信号，再抛中止异常（模拟 cancel 期间 abort）。"""
    def __init__(self):
        super().__init__('x')
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools=None, on_delta=None, on_reason=None,
             temperature=0.7):
        self.calls.append(list(messages))
        self.entered.set()
        self.release.wait(timeout=10)
        raise LLMError('请求被中止')


class TestR2CancelNotRerun(unittest.TestCase):
    def test_cancel_during_abort_stays_canceled(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        app = AgentServer(_config(tmp / 'ws'))
        self.addCleanup(app.batch.close)
        llm = _AbortLLM()
        app.llm = llm
        inp = app.ws.root / 'inputs' / 'a.txt'
        inp.parent.mkdir(parents=True, exist_ok=True)
        inp.write_text('hello', encoding='utf-8')
        job = app.batch.create('取消复现', '处理该文件', ['inputs/a.txt'])
        self.assertTrue(llm.entered.wait(timeout=10), 'batch 未开跑')
        app.batch.control(job['id'], 'cancel')
        llm.release.set()
        # 等 worker 真正收尾（_cur 清空），再无锁静置确认状态不再被改写
        deadline = time.time() + 10
        while time.time() < deadline:
            if app.batch._cur is None:
                break
            time.sleep(0.05)
        time.sleep(0.6)
        with app.batch._lock:
            status = app.batch.jobs[job['id']]['items'][0]['status']
        self.assertIn(status, ('canceled', 'uncertain'),
                      f'取消后异常不得标 failed，实际 {status}')
        calls_before = len(llm.calls)
        out = app.batch.control(job['id'], 'retry')
        time.sleep(0.5)
        self.assertEqual(len(llm.calls), calls_before,
                         '已取消项点重试不得重跑')
        self.assertNotEqual(out['items'][0]['status'], 'pending')


class TestR3RetryUnregisteredOutput(unittest.TestCase):
    def test_retry_skips_when_output_dir_has_unregistered_file(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        app = AgentServer(_config(tmp / 'ws'))
        self.addCleanup(app.batch.close)
        app.llm = FakeLLM('已处理')
        out_rel = 'outputs/2026-11-20/job-x/a.md'
        p = app.ws.root / out_rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('上次已写但未登记', encoding='utf-8')
        job = {
            'id': 'job-x', 'name': '摘录', 'template': 'x',
            'files': ['inputs/a.txt'], 'status': 'done', 'note': '',
            'output_dir': 'outputs/2026-11-20/job-x/',
            'items': [{'idx': 0, 'file': 'inputs/a.txt', 'status': 'failed',
                       'outcome': '', 'error': 'boom', 'response': '',
                       'artifacts': [], 'elapsed': 1}],
        }
        with app.batch._lock:
            app.batch.jobs[job['id']] = job
        out = app.batch.control(job['id'], 'retry')
        it = out['items'][0]
        self.assertIn(it['status'], ('skipped', 'uncertain'),
                      f'未登记但已落盘的输出必须跳过重试，实际 {it["status"]}')
        self.assertEqual(p.read_text(encoding='utf-8'), '上次已写但未登记')


class TestR4RunsLoadNewest(unittest.TestCase):
    def test_load_keeps_newest_per_session(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / 'runs'
        root.mkdir(parents=True)
        old = {'id': 'run-old', 'sid': 's1', 'owner': 'me', 'kind': 'chat',
               'status': 'done', 'started_at': 1000.0, 'finished_at': 1001.0,
               'client_req_id': 'req-old-01', 'action': '',
               'updated_at': 1001.0, 'text_len': 0, 'events': []}
        new = {'id': 'run-new', 'sid': 's1', 'owner': 'me', 'kind': 'chat',
               'status': 'running', 'started_at': 2000.0, 'finished_at': 0,
               'client_req_id': 'req-new-01', 'action': '',
               'updated_at': 2000.0, 'text_len': 0, 'events': []}
        p_old = root / 'run-old.json'
        p_new = root / 'run-new.json'
        p_old.write_text(json.dumps(old), encoding='utf-8')
        p_new.write_text(json.dumps(new), encoding='utf-8')
        # 强制 glob 返回 [新, 旧]：旧实现后写覆盖会留下旧 done
        with mock.patch.object(Path, 'glob',
                               return_value=[p_new, p_old]):
            reg = RunRegistry(root=str(root))
        run = reg.for_session('s1')
        self.assertIsNotNone(run)
        self.assertEqual(run.id, 'run-new')
        self.assertEqual(run.status, 'interrupted')


class TestR5UndoRefused(unittest.TestCase):
    def test_refused_returns_409_and_not_marked(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        app = AgentServer(_config(tmp / 'ws'))
        app.llm = FakeLLM('x')
        httpd = AgentHTTPServer(('127.0.0.1', 0), Handler)
        httpd.agent_server = app
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        self.addCleanup(app.batch.close)
        host, port = httpd.server_address
        base = 'http://%s:%d' % (host, port)
        sess = app.sessions.create('撤销复现')
        sid = sess.id
        app.ws.set_session(SessionCtx(sid, '撤销复现', '2026-11-20'))
        turn = app.ws.begin_turn(sid, '777')
        real, rel = app.ws.resolve_write('notes/a.md')
        real.write_text('本轮写入', encoding='utf-8')
        info = app.ws.end_turn()
        app.ws.clear_session()
        app.sessions.add_message(sid, 'assistant', '已写入',
                                 extra={'snapshot': info})
        real.write_text('之后被用户改过', encoding='utf-8')
        body = json.dumps({'session_id': sid, 'turn': turn}).encode('utf-8')
        req = urllib.request.Request(base + '/api/undo', data=body,
                                     method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = resp.status
                payload = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            code = e.code
            payload = json.loads(e.read().decode('utf-8'))
        self.assertEqual(code, 409, f'冲突拒绝应返回 409，实际 {code} {payload}')
        self.assertTrue(payload.get('refused'))
        self.assertIn(rel, payload.get('conflicts') or [])
        loaded = app.sessions.load(sid)
        snap = (loaded.messages[-1].get('snapshot') or {})
        self.assertFalse(snap.get('undone'),
                         '被拒绝的撤销不得标记 undone')
        self.assertEqual(real.read_text(encoding='utf-8'), '之后被用户改过')


class TestR6ThemeCacheStatic(unittest.TestCase):
    def test_persist_rollback_and_boot_clear_present(self):
        core = (ROOT / 'web' / 'js' / '01-core.js').read_bytes().decode('utf-8')
        boot = (ROOT / 'web' / 'js' / '09-boot.js').read_bytes().decode('utf-8')
        # persistUi 失败必须回滚：出现 removeItem 或恢复旧值逻辑
        seg = core.split('async function persistUi')[1].split('function toggleTheme')[0]
        self.assertTrue(('removeItem' in seg) or ('prev' in seg),
                        'persistUi 失败未回滚 localStorage')
        # 启动 /api/ui 失败必须清主题缓存（catch 块内须删 theme 键）
        idx = boot.index('toast(uiSaveMessage(false)')
        cstart = boot.rindex('.catch(', 0, idx)
        seg_boot = boot[cstart:idx]
        self.assertIn('removeItem', seg_boot,
                      '启动读 ui 失败未清主题缓存')
        self.assertIn("'theme'", seg_boot,
                      '启动读 ui 失败未清主题缓存')


class TestR7StopNotFailed(unittest.TestCase):
    def test_abort_while_stopped_returns_stopped(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = Workspace(str(tmp / 'ws'))
        mem = Memory(str(tmp / 'ws' / '.agent' / 'memory.db'))
        llm = _AbortLLM()
        ag = Agent(ws, llm, mem, context_window=8192, max_turns=2)
        box = {}

        def go():
            try:
                box['r'] = ag.run([], '你好', mode='craft')
            except Exception as e:
                box['e'] = e

        t = threading.Thread(target=go)
        t.start()
        self.assertTrue(llm.entered.wait(timeout=10))
        ag.request_stop()
        llm.release.set()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertNotIn('e', box, f'停止不应抛异常冒成失败: {box.get("e")}')
        self.assertTrue(box['r'].get('stopped'))


class TestR8FollowStreamStatic(unittest.TestCase):
    def test_no_120s_cap_and_heartbeat_present(self):
        src = (ROOT / 'server' / 'httpd.py').read_text(encoding='utf-8')
        seg = src.split('def _follow_existing_run')[1].split('def _handle_chat')[0]
        self.assertNotIn('idle < 600', seg, '跟随流仍有 120 秒硬上限')
        self.assertIn('ping', seg, '跟随流无心跳保活')


class TestR9UiUnknownKeys(unittest.TestCase):
    def test_save_preserves_unknown_and_corrupt_raises(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        agent_dir = tmp / '.agent'
        agent_dir.mkdir(parents=True)
        ui_path = agent_dir / 'ui.json'
        ui_path.write_text(json.dumps(
            {'theme': 'light', 'custom_future': {'x': 1}, 'note': 'keep-me'},
            ensure_ascii=False), encoding='utf-8')
        save_ui(str(tmp), {'theme': 'dark'})
        data = json.loads(ui_path.read_text(encoding='utf-8'))
        self.assertEqual(data['theme'], 'dark')
        self.assertEqual(data.get('custom_future'), {'x': 1})
        self.assertEqual(data.get('note'), 'keep-me')
        # 损坏文件不得静默覆盖
        ui_path.write_text('{not json', encoding='utf-8')
        with self.assertRaises(StorePathError):
            save_ui(str(tmp), {'theme': 'light'})
        self.assertEqual(ui_path.read_text(encoding='utf-8'), '{not json')


class TestBlocker1PersistDualPathAtomic(unittest.TestCase):
    """复审拦下1：双路径 _persist 第二路失败时不得一新一旧。"""

    def test_second_path_failure_rolls_back_both_disks_and_memory(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        p1 = tmp / 'a' / '.agent' / 'config.json'
        p2 = tmp / 'b' / 'config.json'
        p1.parent.mkdir(parents=True, exist_ok=True)
        p2.parent.mkdir(parents=True, exist_ok=True)
        app = AgentServer(_config(tmp / 'ws'), config_path=str(p1),
                          also_config_path=str(p2))
        self.addCleanup(app.batch.close)
        app.llm = FakeLLM('x')
        before_name = app.config['model']['name']
        app.save_config(limits={'chat_timeout_s': 21})
        for p in (p1, p2):
            disk = json.loads(p.read_text(encoding='utf-8'))
            self.assertEqual(disk['limits']['chat_timeout_s'], 21)
            self.assertEqual(disk['model']['name'], before_name)
        real_replace = os.replace
        calls = {'n': 0}

        def flaky(src, dst):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('second path fail (injected)')
            return real_replace(src, dst)

        with mock.patch('server.httpd.os.replace', side_effect=flaky):
            with self.assertRaises(Exception,
                                   msg='第二路写盘失败必须抛错'):
                app.save_config(model={
                    'name': 'SHOULD-NOT-STICK',
                    'base_url': 'http://127.0.0.1:9/v1',
                    'api_key': '', 'thinking': 'hide',
                    'max_tokens': 0, 'context_window': 8192})
        self.assertEqual(app.config['model']['name'], before_name,
                         '内存必须回滚到旧值')
        for p in (p1, p2):
            disk = json.loads(p.read_text(encoding='utf-8'))
            self.assertEqual(disk['model']['name'], before_name,
                             f'{p} 磁盘必须仍是旧值，不得一新一旧')
            self.assertEqual(disk['limits']['chat_timeout_s'], 21)
            leftovers = list(p.parent.glob('config.*.tmp'))
            self.assertEqual(leftovers, [],
                             f'{p.parent} 不得残留 mkstemp tmp: {leftovers}')


class TestBlocker2UnregisteredStemAndMtime(unittest.TestCase):
    """复审拦下2：stem 子串太宽 + 必须按 started_at 过滤 mtime。"""

    def _app_with_out(self, files_with_mtime):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        app = AgentServer(_config(tmp / 'ws'))
        self.addCleanup(app.batch.close)
        app.llm = FakeLLM('已处理')
        out_rel = 'outputs/2026-11-20/job-b2/'
        base = app.ws.root / out_rel
        base.mkdir(parents=True, exist_ok=True)
        for name, mtime in files_with_mtime:
            f = base / name
            f.write_text('out', encoding='utf-8')
            os.utime(f, (mtime, mtime))
        return app, out_rel

    @staticmethod
    def _started(epoch):
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))

    def test_short_stem_substring_must_not_hit(self):
        now = time.time()
        app, out_rel = self._app_with_out([('summary.md', now)])
        job = {'output_dir': out_rel}
        item = {'file': 'inputs/a.txt', 'started_at': self._started(now)}
        self.assertFalse(
            app.batch._has_unregistered_output(job, item),
            '输入 a.txt 不得命中 summary.md（旧逻辑 stem in name 误杀）')

    def test_prefix_output_after_start_must_hit(self):
        now = time.time()
        app, out_rel = self._app_with_out([('report-摘录.md', now)])
        job = {'output_dir': out_rel}
        item = {'file': 'inputs/report.txt',
                'started_at': self._started(now - 10)}
        self.assertTrue(
            app.batch._has_unregistered_output(job, item),
            'report-摘录.md 且 mtime 在开跑后必须命中')

    def test_same_name_old_file_before_start_must_not_hit(self):
        now = time.time()
        app, out_rel = self._app_with_out([('report.md', now - 3600)])
        job = {'output_dir': out_rel}
        item = {'file': 'inputs/report.txt',
                'started_at': self._started(now)}
        self.assertFalse(
            app.batch._has_unregistered_output(job, item),
            '同名旧文件 mtime 在开跑前不得命中（须按 started_at 过滤）')


if __name__ == '__main__':
    unittest.main()
