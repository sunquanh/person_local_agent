# -*- coding: utf-8 -*-
"""工作区存储的进程重启与 HTTP 集成。不用函数调用冒充换进程。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.httpd import AgentHTTPServer, AgentServer, Handler  # noqa: E402
from server.storepath import prepare_launch  # noqa: E402

WORKER = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.chdir(sys.argv[2])
from server.sessions import SessionManager
from server.storepath import load_ui, prepare_launch, save_ui
mode, prog, ws = sys.argv[3], sys.argv[4], sys.argv[5]
launch = prepare_launch(prog, workspace_arg=ws)
if mode == "write":
    sm = SessionManager(os.path.join(launch.workspace_root, ".agent"))
    s = sm.create("集成甲")
    sm.add_message(s.id, "user", "第一条")
    save_ui(launch.workspace_root, {"theme": "dark", "last_session": s.id})
    cfg_path = os.path.join(launch.workspace_root, ".agent", "config.json")
    cfg = json.loads(open(cfg_path, encoding="utf-8").read())
    cfg.setdefault("model", {})["name"] = "saved-model"
    cfg.setdefault("model", {})["api_key"] = ""
    open(cfg_path, "w", encoding="utf-8").write(json.dumps(cfg))
    print(json.dumps({"id": s.id, "root": launch.workspace_root}))
else:
    sm = SessionManager(os.path.join(launch.workspace_root, ".agent"))
    ui = load_ui(launch.workspace_root)
    print(json.dumps({
        "root": launch.workspace_root,
        "titles": [r["title"] for r in sm.list_all()],
        "theme": ui.get("theme"),
        "last": ui.get("last_session"),
        "model": (launch.config.get("model") or {}).get("name"),
        "cfg": launch.config_path,
    }, ensure_ascii=False))
'''


def _run(mode, prog, ws, cwd):
    proc = subprocess.run(
        [sys.executable, '-c', WORKER, str(ROOT), cwd, mode, prog, ws],
        cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestRestartAndIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.prog = self.tmp / '程序'
        self.prog.mkdir()
        self.cwd_a = self.tmp / '启动甲'
        self.cwd_b = self.tmp / '启动乙'
        self.cwd_a.mkdir()
        self.cwd_b.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_then_new_process_reads_same_history(self):
        ws = str(self.tmp / '工作 区')
        written = _run('write', str(self.prog), ws, str(self.cwd_a))
        read = _run('read', str(self.prog), ws, str(self.cwd_b))
        self.assertEqual(read['root'], written['root'])
        self.assertIn('集成甲', read['titles'])
        self.assertEqual(read['theme'], 'dark')
        self.assertEqual(read['last'], written['id'])
        self.assertEqual(read['model'], 'saved-model')
        self.assertTrue(read['cfg'].endswith(os.path.join('.agent', 'config.json')))

    def test_two_workspaces_keep_separate_history(self):
        a = str(self.tmp / '区A')
        b = str(self.tmp / '区B')
        _run('write', str(self.prog), a, str(self.cwd_a))
        # 第二进程把指针改到 B，并写入另一套偏好
        proc = subprocess.run(
            [sys.executable, '-c', WORKER, str(ROOT), str(self.cwd_b),
             'write', str(self.prog), b],
            cwd=str(self.cwd_b), capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 把 B 的主题改成 light，标题会是另一次 create「集成甲」
        # 再读 A：仍是 dark，且只有 A 的会话
        read_a = _run('read', str(self.prog), a, str(self.cwd_a))
        read_b = _run('read', str(self.prog), b, str(self.cwd_b))
        self.assertNotEqual(read_a['root'], read_b['root'])
        self.assertEqual(read_a['theme'], 'dark')
        self.assertEqual(read_b['theme'], 'dark')
        self.assertEqual(read_a['titles'], ['集成甲'])
        self.assertEqual(read_b['titles'], ['集成甲'])
        self.assertNotEqual(read_a['last'], read_b['last'])


class TestHttpUi(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.prog = self.tmp / '程序'
        self.prog.mkdir()
        self.ws = self.tmp / '界面区'
        launch = prepare_launch(str(self.prog), workspace_arg=str(self.ws))
        self.launch = launch
        self.app = AgentServer(launch.config, config_path=launch.config_path,
                                program_dir=str(self.prog))
        self.httpd = AgentHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.agent_server = self.app
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_address[1]

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

    def _req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8')
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {'error': raw}
            return e.code, payload

    def test_ui_post_survives_restart_and_bad_body_is_rejected(self):
        code, saved = self._req('POST', '/api/ui', {'theme': 'dark', 'last_session': 's-http'})
        self.assertEqual(code, 200)
        self.assertEqual(saved['theme'], 'dark')
        code, bad = self._req('POST', '/api/ui', ['not-an-object'])
        self.assertEqual(code, 400)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.app.batch.close()
        read = _run('read', str(self.prog), str(self.ws), str(self.tmp))
        self.assertEqual(read['theme'], 'dark')
        self.assertEqual(read['last'], 's-http')


class TestUiCacheKey(unittest.TestCase):
    def test_keys_are_scoped_and_failure_is_visible(self):
        proc = subprocess.run(
            ['node', '-e', """
const m = require(%s);
const a = m.uiCacheKey('/tmp/工作区 A', 'theme');
const b = m.uiCacheKey('/tmp/工作区 B', 'theme');
if (a === b) process.exit(2);
if (a === 'theme' || b === 'theme') process.exit(3);
if (!m.uiSaveMessage(false).includes('未写入')) process.exit(4);
if (m.uiSaveMessage(true) !== '') process.exit(5);
console.log(a);
console.log(b);
""" % json.dumps(str(ROOT / 'web' / 'js' / 'ui-cache.js'))],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        lines = proc.stdout.strip().splitlines()
        self.assertNotEqual(lines[0], lines[1])
        self.assertTrue(lines[0].startswith('wb:'))
        self.assertNotEqual(lines[0], 'theme')
        self.assertIn('%', lines[0])
