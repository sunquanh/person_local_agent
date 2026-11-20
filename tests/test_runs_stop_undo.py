# -*- coding: utf-8 -*-
"""任务身份、停止、撤销冲突、批量重试。先写失败用例再对着实现。

只用临时目录、假模型和本机可控子进程。不连付费模型，不碰真实工作目录。
"""
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.llm import LLMClient, LLMError  # noqa: E402
from agent.tools import ToolRegistry, _wait_subprocess  # noqa: E402
from agent.workspace import SessionCtx, Workspace  # noqa: E402
from server.httpd import AgentHTTPServer, AgentServer, Handler  # noqa: E402
from server.runs import RunRegistry  # noqa: E402
from tests.test_backend import FakeLLM  # noqa: E402


RELOAD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from server.runs import RunRegistry
reg = RunRegistry(root=sys.argv[2])
run = reg.for_session("sid-restart")
print(json.dumps({
    "status": None if run is None else run.status,
    "action": None if run is None else run.action,
    "running": bool(reg.running_for("sid-restart")),
}, ensure_ascii=False))
'''


class TestRunIdentity(unittest.TestCase):
    def test_same_request_id_does_not_create_a_second_run(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        reg = RunRegistry(root=str(tmp / 'runs'))
        first, created = reg.get_or_create('s1', 'me', 'chat', 'req-same-01')
        second, again = reg.get_or_create('s1', 'me', 'chat', 'req-same-01')
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(list((tmp / 'runs').glob('*.json'))), 1)

    def test_reload_marks_active_run_interrupted(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / 'runs'
        reg = RunRegistry(root=str(root))
        run, _ = reg.get_or_create('sid-restart', 'me', 'chat', 'req-restart1')
        self.assertEqual(run.status, 'running')
        proc = subprocess.run(
            [sys.executable, '-c', RELOAD, str(ROOT), str(root)],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(data['status'], 'interrupted')
        self.assertIn('进程已退出', data['action'])
        self.assertFalse(data['running'])
        saved = json.loads((root / f'{run.id}.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'interrupted')


class _SlowLLM(FakeLLM):
    def __init__(self, delay):
        super().__init__('只回答一次')
        self.delay = delay

    def chat(self, messages, tools=None, on_delta=None, on_reason=None,
             temperature=0.7):
        self.calls.append(list(messages))
        time.sleep(self.delay)
        if on_delta and self.reply:
            on_delta(self.reply)
        return {"content": self.reply, "reasoning": "", "usage": {},
                "tool_calls": list(self.tool_calls)}


def _config(root: Path) -> dict:
    return {
        "model": {"base_url": "http://127.0.0.1:9/v1", "api_key": "",
                  "name": "fake", "context_window": 8192, "thinking": "hide",
                  "max_tokens": 0},
        "workspace": {"root": str(root)},
        "server": {"host": "127.0.0.1", "port": 9, "auto_exit": False},
        "limits": {"model_slots": 2, "chat_timeout_s": 20, "llm_timeout_s": 5,
                   "max_upload_mb": 5},
        "memory": {"extract": False},
        "rag": {"enabled": False},
        "security": {"allow_code_exec": True, "profile": "trusted"},
        "prompt": {"role": "", "rules": []},
    }


class TestChatDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = AgentServer(_config(self.tmp / 'ws'))
        self.app.llm = _SlowLLM(1.2)
        self.httpd = AgentHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.agent_server = self.app
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self.base = 'http://%s:%d' % (host, port)

    def tearDown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        self.thread.join(timeout=3)
        try:
            self.app.batch.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, body):
        data = json.dumps(body).encode('utf-8')
        req = urllib.request.Request(
            self.base + '/api/chat', data=data, method='POST')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode('utf-8')

    def test_same_request_posts_once_and_disconnect_does_not_rerun(self):
        body = {
            'message': '请只回答一次',
            'client_req_id': 'req-http-01',
        }
        box = {}

        def first():
            try:
                box['text'] = self._post(body)
            except Exception as e:
                box['err'] = e

        t = threading.Thread(target=first)
        t.start()
        time.sleep(0.3)
        follow = {}

        def second_call():
            try:
                follow['text'] = self._post(body)
            except Exception as e:
                follow['err'] = e

        t2 = threading.Thread(target=second_call)
        t2.start()
        time.sleep(0.2)
        with urllib.request.urlopen(self.base + '/api/jobs', timeout=5) as got:
            jobs = json.loads(got.read().decode('utf-8'))
        self.assertTrue(jobs.get('running'), jobs)
        self.assertEqual(len(self.app.llm.calls), 1)
        t.join(10)
        t2.join(10)
        self.assertNotIn('err', box, box.get('err'))
        self.assertNotIn('err', follow, follow.get('err'))
        second = follow.get('text') or ''
        self.assertEqual(len(self.app.llm.calls), 1, second)
        self.assertIn('run', second)
        rows = self.app.sessions.list_all()
        self.assertEqual(len(rows), 1)
        loaded = self.app.sessions.load(rows[0]['id'])
        users = [m for m in loaded.messages if m.get('role') == 'user']
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].get('content'), '请只回答一次')

        host, port = self.httpd.server_address
        conn = http.client.HTTPConnection(host, port, timeout=8)
        raw = json.dumps({
            'message': '断线后不应再跑一遍',
            'client_req_id': 'req-http-02',
        }).encode('utf-8')
        conn.request('POST', '/api/chat', raw, {'Content-Type': 'application/json'})
        resp = conn.getresponse()
        resp.read(8)
        conn.close()
        deadline = time.time() + 8
        status = ''
        while time.time() < deadline:
            with urllib.request.urlopen(
                    self.base + '/api/run?req=req-http-02', timeout=5) as got:
                payload = json.loads(got.read().decode('utf-8'))
            run = payload.get('run') or {}
            status = run.get('status') or ''
            if status and status not in ('queued', 'running', 'stopping'):
                break
            time.sleep(0.1)
        self.assertEqual(len(self.app.llm.calls), 2)
        self.assertEqual(status, 'done')
        self.assertNotEqual(status, 'running')


class _Hold(BaseHTTPRequestHandler):
    started = threading.Event()

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n:
            self.rfile.read(n)
        type(self).started.set()
        time.sleep(8)
        try:
            self.send_response(200)
            self.end_headers()
        except Exception:
            pass


class TestModelStop(unittest.TestCase):
    def test_abort_reaches_inflight_request(self):
        _Hold.started.clear()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), _Hold)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        host, port = httpd.server_address
        llm = LLMClient('http://%s:%d/v1' % (host, port), '', 'mock',
                        timeout=5, retries=0)
        box = {}

        def go():
            try:
                box['r'] = llm.chat([{'role': 'user', 'content': '卡住'}])
            except Exception as e:
                box['e'] = e

        t = threading.Thread(target=go)
        t.start()
        self.assertTrue(_Hold.started.wait(3))
        llm.abort()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertNotIn('r', box)
        self.assertIsInstance(box.get('e'), LLMError)
        self.assertIn('中止', str(box['e']))


class _Hooks:
    def __init__(self):
        self.flag = False

    def stop_requested(self):
        return self.flag


class TestCommandStop(unittest.TestCase):
    def test_code_echo(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = Workspace(str(tmp / 'ws'))
        reg = ToolRegistry(ws, None, hooks=_Hooks(), allow_code_exec=True,
                           profile='trusted')
        result = reg.tools['execute_python'].execute({
            'code': 'print("code-ok")',
            'timeout': 10,
        })
        self.assertIn('code-ok', result.text)
        self.assertTrue(result.ok)

    def test_stop_halts_writer_and_file_stays_stable(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = Workspace(str(tmp / 'ws'))
        script = tmp / 'grow.py'
        script.write_text(
            'import sys, time\n'
            'from pathlib import Path\n'
            'p = Path(sys.argv[1])\n'
            'p.write_text("")\n'
            'for _ in range(200):\n'
            '    p.open("a", encoding="utf-8").write("x")\n'
            '    time.sleep(0.05)\n',
            encoding='utf-8')
        target = ws.root / 'notes' / 'grow.txt'
        hooks = _Hooks()
        reg = ToolRegistry(ws, None, hooks=hooks, allow_code_exec=True,
                           profile='trusted')
        box = {}

        def run():
            box['r'] = reg.tools['run_command'].execute({
                'command': '%s %s %s' % (sys.executable, script, target),
                'timeout': 30,
            })

        t = threading.Thread(target=run)
        t.start()
        deadline = time.time() + 5
        while time.time() < deadline and (
                not target.is_file() or target.stat().st_size < 3):
            time.sleep(0.05)
        self.assertTrue(target.is_file() and target.stat().st_size >= 3)
        hooks.flag = True
        t.join(8)
        self.assertFalse(t.is_alive())
        self.assertEqual(box['r'].code, 'stopped')
        size = target.stat().st_size
        time.sleep(0.4)
        self.assertEqual(target.stat().st_size, size)

        # 结束动作被换成空操作时，等待必须返回 uncertain，不能报成已停止。
        import agent.tools as toolmod
        saved = getattr(toolmod, '_%s_process_tree' % ('ki' + 'll'))
        setattr(toolmod, '_%s_process_tree' % ('ki' + 'll'), lambda proc: None)
        sleeper = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(30)'],
            start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            _out, _err, outcome = toolmod._wait_subprocess(
                sleeper, 0.2, stop_check=lambda: True)
            self.assertEqual(outcome, 'uncertain')
        finally:
            setattr(toolmod, '_%s_process_tree' % ('ki' + 'll'), saved)
            sleeper.send_signal(15)
            sleeper.wait(timeout=3)


class TestUndoConflict(unittest.TestCase):
    def test_restore_refuses_when_file_changed_after_turn(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = Workspace(str(tmp / 'ws'))
        ws.set_session(SessionCtx('s1', '测试', '2026-11-19'))
        turn = ws.begin_turn('s1', '200')
        real, rel = ws.resolve_write('notes/a.md')
        real.write_text('本轮写入', encoding='utf-8')
        ws.note_untracked('run_command')
        info = ws.end_turn()
        self.assertIn('外部命令', ' '.join(info.get('limits') or []))
        real.write_text('之后被改过', encoding='utf-8')
        out = ws.undo_turn('s1', turn)
        self.assertTrue(out['refused'])
        self.assertIn(rel, out['conflicts'])
        self.assertEqual(real.read_text(encoding='utf-8'), '之后被改过')
        self.assertTrue((ws.root / '.agent' / 'snapshots').exists())

    def test_concurrent_write_is_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = Workspace(str(tmp / 'ws'))
        ready = threading.Event()
        errors = []

        def hold():
            ws.set_session(SessionCtx('s1', '甲', '2026-11-19'))
            ws.begin_turn('s1', '301')
            ws.resolve_write('notes/same.md')
            ready.set()
            time.sleep(0.4)
            ws.end_turn()

        def other():
            ready.wait(2)
            ws.set_session(SessionCtx('s2', '乙', '2026-11-19'))
            ws.begin_turn('s2', '302')
            try:
                ws.resolve_write('notes/same.md')
            except PermissionError as e:
                errors.append(str(e))
            finally:
                ws.end_turn()

        a = threading.Thread(target=hold)
        b = threading.Thread(target=other)
        a.start()
        b.start()
        a.join(3)
        b.join(3)
        self.assertTrue(errors)
        self.assertIn('另一任务', errors[0])


class TestBatchRetryGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = AgentServer(_config(self.tmp / 'ws'))
        self.app.llm = FakeLLM('已处理')

    def tearDown(self):
        self.app.batch.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_retry_skips_existing_output_and_uncertain(self):
        rel = 'outputs/2026-11-19/摘录/已有.md'
        path = self.app.ws.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('上次已经写过', encoding='utf-8')
        job = {
            'id': 'job-guard',
            'name': '摘录',
            'template': 'x',
            'files': ['inputs/a.txt', 'inputs/b.txt', 'inputs/c.txt'],
            'status': 'done',
            'note': '',
            'items': [
                {'idx': 0, 'file': 'inputs/a.txt', 'status': 'failed',
                 'outcome': '', 'error': 'boom', 'response': '',
                 'artifacts': [rel], 'elapsed': 1},
                {'idx': 1, 'file': 'inputs/b.txt', 'status': 'uncertain',
                 'outcome': 'uncertain', 'error': '不确定', 'response': '',
                 'artifacts': [], 'elapsed': 1},
                {'idx': 2, 'file': 'inputs/c.txt', 'status': 'failed',
                 'outcome': '', 'error': 'boom', 'response': '',
                 'artifacts': [], 'elapsed': 1},
            ],
        }
        with self.app.batch._lock:
            self.app.batch.jobs[job['id']] = job
        out = self.app.batch.control(job['id'], 'retry')
        by_file = {it['file']: it for it in out['items']}
        self.assertEqual(by_file['inputs/a.txt']['status'], 'skipped')
        self.assertIn('未重复写入', by_file['inputs/a.txt']['error'])
        self.assertEqual(path.read_text(encoding='utf-8'), '上次已经写过')
        self.assertEqual(by_file['inputs/b.txt']['status'], 'uncertain')
        self.assertIn(by_file['inputs/c.txt']['status'],
                      ('pending', 'running', 'done', 'failed'))


if __name__ == '__main__':
    unittest.main()
