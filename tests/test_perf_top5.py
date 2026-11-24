# -*- coding: utf-8 -*-
"""Top 5 性能优化复现/回归测试（2026-11-24 评审批）。

1. add_message 增量维护统计（已在先前批次实现，这里是回归 guard：
   300 条消息后单次 add 必须快、fail_count 必须与全量重算一致）。
2. /api/health 不得调 rag.stats()（15 秒一次的全树遍历）。
3. RAG refresh 无变化时不重建 df 表。
4. 启动模型探测超时为 1 秒（原 3 秒）。
5. _handle_chat 一轮只 load 一次会话（记忆提取复用已加载的 session）。
"""
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.rag import Rag as RAGIndex  # noqa: E402
from server.sessions import SessionManager  # noqa: E402


class TestAddMessageIncremental(unittest.TestCase):
    def test_stats_match_full_recalc_and_single_add_is_fast(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        sm = SessionManager(str(tmp / 'ws'))
        s = sm.create('长会话')
        fails = 0
        for i in range(300):
            steps = [{'ok': i % 7 == 0, 'name': 't'}] if i % 3 == 0 else []
            fails += sum(1 for st in steps if not st['ok'])
            sm.add_message(s.id, 'assistant', '回复 %d' % i,
                           extra={'tool_steps': steps} if steps else None)
        t0 = time.time()
        sm.add_message(s.id, 'assistant', '再一条',
                       extra={'tool_steps': [{'ok': False, 'name': 't'}]})
        self.assertLess(time.time() - t0, 1.0)
        row = sm.list_all()[0]
        self.assertEqual(row['msg_count'], 301)
        self.assertEqual(row['fail_count'], fails + 1)
        # 全量重算对照：_refresh_stats 应与增量维护值一致
        with sm._db() as c:
            sm._refresh_stats(c, s.id)
        row2 = sm.list_all()[0]
        self.assertEqual(row2['fail_count'], fails + 1)
        self.assertEqual(row2['msg_count'], 301)


class TestRagRefreshSkipsDf(unittest.TestCase):
    def test_second_refresh_without_changes_does_not_rebuild_df(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = tmp / 'ws'
        (ws / 'inputs').mkdir(parents=True)
        (ws / 'inputs' / 'a.txt').write_text('hello world ' * 200, encoding='utf-8')
        (ws / 'inputs' / 'b.txt').write_text('another document ' * 200, encoding='utf-8')
        rag = RAGIndex(str(ws))
        rag.refresh()
        df_before = rag.db.execute('SELECT token, n FROM df ORDER BY token').fetchall()
        calls = []

        orig = rag._rebuild_df

        def counting():
            calls.append(1)
            return orig()

        rag._rebuild_df = counting
        stats = rag.refresh()
        self.assertEqual(stats['updated'], 0)
        self.assertEqual(stats['new'], 0)
        self.assertEqual(stats['removed'], 0)
        self.assertEqual(calls, [], '无变化时 refresh 不应重建 df')
        df_after = rag.db.execute('SELECT token, n FROM df ORDER BY token').fetchall()
        self.assertEqual(df_after, df_before)
        self.assertEqual(rag.search('hello world', 2),
                         rag.search('hello world', 2))

    def test_refresh_after_change_rebuilds_df(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = tmp / 'ws'
        (ws / 'inputs').mkdir(parents=True)
        (ws / 'inputs' / 'a.txt').write_text('hello world ' * 200, encoding='utf-8')
        rag = RAGIndex(str(ws))
        rag.refresh()
        (ws / 'inputs' / 'a.txt').write_text('totally different words ' * 200,
                                             encoding='utf-8')
        calls = []
        orig = rag._rebuild_df

        def counting():
            calls.append(1)
            return orig()

        rag._rebuild_df = counting
        rag.refresh()
        self.assertEqual(calls, [1])
        toks = {r[0] for r in rag.db.execute('SELECT token FROM df')}
        self.assertIn('totally', toks)


class TestStartupProbeTimeout(unittest.TestCase):
    def test_check_env_probes_with_one_second_timeout(self):
        import app as appmod
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        seen = {}

        def fake_connect(addr, timeout=None):
            seen['timeout'] = timeout
            raise socket.error('blackhole')

        orig = socket.create_connection
        socket.create_connection = fake_connect
        try:
            cfg = {'workspace': {'root': str(tmp / 'ws')},
                   'model': {'base_url': 'http://10.255.255.1:12345/v1'}}
            self.assertTrue(appmod.check_env(cfg))
        finally:
            socket.create_connection = orig
        self.assertEqual(seen.get('timeout'), 1)


class TestHealthAndChatLoad(unittest.TestCase):
    def _start_server(self, ws, memory_extract=False):
        from server.httpd import AgentHTTPServer, AgentServer, Handler
        from tests.test_backend import FakeLLM
        cfg = {
            'model': {'base_url': 'http://127.0.0.1:9/v1', 'api_key': '',
                      'name': 'fake', 'context_window': 8192, 'thinking': 'hide',
                      'max_tokens': 0},
            'workspace': {'root': str(ws)},
            'server': {'host': '127.0.0.1', 'port': 9, 'auto_exit': False},
            'limits': {'model_slots': 2, 'chat_timeout_s': 20, 'llm_timeout_s': 5,
                       'max_upload_mb': 5},
            'memory': {'extract': memory_extract},
            'rag': {'enabled': False},
            'security': {'allow_code_exec': True, 'profile': 'trusted'},
            'prompt': {'role': '', 'rules': []},
        }
        app = AgentServer(cfg)
        app.llm = FakeLLM('好')
        httpd = AgentHTTPServer(('127.0.0.1', 0), Handler)
        httpd.agent_server = app
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(app.batch.close)
        host, port = httpd.server_address
        return app, 'http://%s:%d' % (host, port)

    def test_health_does_not_call_rag_stats(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = tmp / 'ws'
        (ws / 'inputs').mkdir(parents=True)
        for i in range(50):
            (ws / 'inputs' / ('f%03d.txt' % i)).write_text('x ' * 500, encoding='utf-8')
        app, base = self._start_server(ws)
        calls = []
        orig = app.rag.stats

        def counting():
            calls.append(1)
            return orig()

        app.rag.stats = counting
        with urllib.request.urlopen(base + '/api/health', timeout=10) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        self.assertIn('ok', body)
        self.assertEqual(calls, [], 'health 不应触发 RAG 全树遍历')

    def test_chat_turn_loads_session_once(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        app, base = self._start_server(tmp / 'ws', memory_extract=True)
        s = app.sessions.create('复用测试')
        for i in range(6):
            app.sessions.add_message(s.id, 'user' if i % 2 == 0 else 'assistant',
                                     '消息 %d' % i)
        calls = []
        orig_load = app.sessions.load

        def counting(sid):
            calls.append(sid)
            return orig_load(sid)

        app.sessions.load = counting
        data = json.dumps({'message': '再问一句', 'session_id': s.id,
                           'client_req_id': 'req-load-01'}).encode('utf-8')
        req = urllib.request.Request(base + '/api/chat', data=data, method='POST')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode('utf-8')
        self.assertIn('result', text)
        # 记忆提取在后台线程跑，等它完成再数
        deadline = time.time() + 5
        while time.time() < deadline and len(calls) < 1:
            time.sleep(0.05)
        time.sleep(0.5)
        self.assertEqual(len(calls), 1, '一轮只应 load 一次会话，实际 %d 次' % len(calls))
        loaded = orig_load(s.id)
        self.assertEqual(len(loaded.messages), 8)


if __name__ == '__main__':
    unittest.main()
