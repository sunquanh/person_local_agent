# -*- coding: utf-8 -*-
"""presence 长连接 + 宽限期 10 秒 + 实例锁心跳接管。

回归：关浏览器后 5 秒服务不退——前端靠 sendBeacon 注销，浏览器整窗关闭时
beacon 经常发不出去，登记表里的残留页要 2 小时心跳超时才清，宽限期根本
没开始计时。A2 修法：每标签页再持一条 presence 长连接（SSE），连接一断
服务端几秒内注销该页。B 修法：实例锁每 10 秒续 mtime，下次启动靠 mtime
超期（60 秒）接管 stale 锁——pid 可能被别的进程重用，只信 pid 会误判。
"""
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from server.httpd import (
    AgentHTTPServer,
    AgentServer,
    Handler,
    _acquire_instance_lock,
)
from tests.test_runs_stop_undo import _config


class TestPresenceCounting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = AgentServer(_config(self.tmp / 'ws'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.app.batch.close)

    def test_presence_open_counts_and_keeps_page(self):
        self.app.page_open('p1')
        self.app.page_presence_open('p1')
        self.assertEqual(self.app._presence.get('p1'), 1)
        self.assertIn('p1', self.app._pages)
        self.app.page_presence_open('p1')
        self.assertEqual(self.app._presence.get('p1'), 2)
        self.app.page_presence_close('p1')
        self.assertEqual(self.app._presence.get('p1'), 1)
        self.assertIn('p1', self.app._pages)
        self.app.page_presence_close('p1')
        self.assertNotIn('p1', self.app._presence)
        self.assertNotIn('p1', self.app._pages)

    def test_presence_close_of_unknown_pid_is_safe(self):
        self.app.page_open('p1')
        self.app.page_presence_close('nope')
        self.assertIn('p1', self.app._pages)

    def test_presence_open_upserts_unregistered_page(self):
        self.app.page_presence_open('p9')
        self.assertIn('p9', self.app._pages)
        self.assertTrue(self.app._pages_seen)


class TestGraceDefault(unittest.TestCase):
    def test_grace_default_is_10s(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        app = AgentServer(_config(tmp / 'ws'))
        self.addCleanup(app.batch.close)
        self.assertEqual(app.auto_exit_grace_s, 10)

    def test_explicit_grace_is_respected(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = _config(tmp / 'ws')
        cfg['server']['auto_exit_grace_s'] = 3
        app = AgentServer(cfg)
        self.addCleanup(app.batch.close)
        self.assertEqual(app.auto_exit_grace_s, 3)


class TestLivePresenceDisconnect(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = _config(self.tmp / 'ws')
        cfg['server']['auto_exit'] = True
        self.app = AgentServer(cfg)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.app.batch.close)
        self.httpd = AgentHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.agent_server = self.app
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.httpd.server_address

    def tearDown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        self.thread.join(timeout=3)

    def test_disconnect_unregisters_page_within_seconds(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/api/page/presence?id=px1')
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn('text/event-stream', resp.getheader('Content-Type'))
        # handler 已跑 page_presence_open：页面登记上
        deadline = time.time() + 5
        while 'px1' not in self.app._pages and time.time() < deadline:
            time.sleep(0.1)
        self.assertIn('px1', self.app._pages)
        # 注意：HTTPResponse 持有一个 dup 出来的 fd，只关 conn 不发 FIN，
        # 必须两个都关，服务端才看得到断开（真浏览器关标签时全关）。
        resp.close()
        conn.close()
        # 连接一断，服务端几秒内注销（不用等 2 小时心跳超时）
        deadline = time.time() + 5
        while 'px1' in self.app._pages and time.time() < deadline:
            time.sleep(0.1)
        self.assertNotIn('px1', self.app._pages)

    def test_presence_without_id_is_400(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request('GET', '/api/page/presence')
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        resp.close()


class TestLockHeartbeatTakeover(unittest.TestCase):
    def _write_lock(self, ws, pid, port, age_s):
        lock_dir = Path(ws) / '.agent'
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / 'server.lock'
        lock_path.write_text(json.dumps(
            {'pid': pid, 'host': '127.0.0.1', 'port': port,
             'started': '2026-11-25 00:00:00'}), encoding='utf-8')
        old = time.time() - age_s
        os.utime(lock_path, (old, old))
        return lock_path

    def test_stale_mtime_takeover_despite_live_pid(self):
        # pid 是活的（就是本进程），端口没人在服务，锁 120 秒没续期：
        # 这是 PID 被重用/持有进程僵死的样子，必须接管，不能报“疑似已有实例”。
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self._write_lock(tmp / 'ws', os.getpid(), 9, age_s=120)
        got = _acquire_instance_lock(str(tmp / 'ws'), '127.0.0.1', 18099)
        self.assertIsNotNone(got)
        payload = json.loads(Path(got).read_text(encoding='utf-8'))
        self.assertEqual(payload['pid'], os.getpid())

    def test_fresh_lock_with_live_pid_is_still_refused(self):
        # 锁是刚建的（活实例在跑）：即使 pid 活着，也绝不能接管。
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self._write_lock(tmp / 'ws', os.getpid(), 9, age_s=0)
        with self.assertRaises(RuntimeError):
            _acquire_instance_lock(str(tmp / 'ws'), '127.0.0.1', 18098)


if __name__ == '__main__':
    unittest.main()
