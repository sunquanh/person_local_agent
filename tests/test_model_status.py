""" /api/model/status：首屏模型连通提示的轻量探活接口。

前端 09-boot.js 启动时 fetch 它；reachable === false 时亮 #modelCard
警告卡片。之前 httpd.py 里根本没有这个路由，每次启动都是 404
（前端 .catch 吞掉，只丢了警告功能）。本文件覆盖：未配地址、
端口不通、模型服务正常响应、端口非法（:0/越界/非数字）、非法 scheme、
https 闭端口、https 自签名证书拒；并确认响应里绝不出现 api_key。
"""
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.test_backend import FakeLLM  # noqa: E402


def _cfg(ws, base_url, api_key=''):
    return {
        'model': {'base_url': base_url, 'api_key': api_key,
                  'name': 'qwen', 'context_window': 8192, 'thinking': 'hide',
                  'max_tokens': 0},
        'workspace': {'root': str(ws)},
        'server': {'host': '127.0.0.1', 'port': 9, 'auto_exit': False},
        'limits': {'model_slots': 2, 'chat_timeout_s': 20, 'llm_timeout_s': 5,
                   'max_upload_mb': 5},
        'memory': {'extract': False},
        'rag': {'enabled': False},
        'security': {'allow_code_exec': True, 'profile': 'trusted'},
        'prompt': {'role': '', 'rules': []},
    }


class _StubModel(BaseHTTPRequestHandler):
    """只认 GET /v1/models 的假模型服务。"""

    def do_GET(self):
        if self.path == '/v1/models':
            body = json.dumps({'data': [{'id': 'qwen'}]}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class TestModelStatus(unittest.TestCase):
    def _start_server(self, ws, base_url, api_key=''):
        from server.httpd import AgentHTTPServer, AgentServer, Handler
        app = AgentServer(_cfg(ws, base_url, api_key))
        app.llm = FakeLLM('好')
        httpd = AgentHTTPServer(('127.0.0.1', 0), Handler)
        httpd.agent_server = app
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        # addCleanup 按 LIFO 执行，倒序添加使执行顺序为
        # shutdown → server_close → join（正序加会让 join 先等满 3 秒）
        self.addCleanup(thread.join, 3)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(app.batch.close)
        host, port = httpd.server_address
        return app, 'http://%s:%d' % (host, port)

    def _ws(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ws = tmp / 'ws'
        (ws / 'inputs').mkdir(parents=True)
        return ws

    def _get(self, base):
        with urllib.request.urlopen(base + '/api/model/status', timeout=10) as resp:
            self.assertEqual(resp.status, 200, '接口本身不应 404')
            raw = resp.read().decode('utf-8')
        return raw, json.loads(raw)

    def test_no_base_url(self):
        _, base = self._start_server(self._ws(), '')
        raw, d = self._get(base)
        self.assertFalse(d['reachable'])
        self.assertEqual(d['error'], '地址未配置')
        self.assertEqual(d['model'], 'qwen')

    def test_closed_port_unreachable_and_no_key_leak(self):
        _, base = self._start_server(self._ws(), 'http://127.0.0.1:9/v1',
                                     api_key='SECRET-KEY-123')
        raw, d = self._get(base)
        self.assertFalse(d['reachable'])
        self.assertIn('error', d)
        self.assertEqual(d['base_url'], 'http://127.0.0.1:9/v1')
        self.assertNotIn('SECRET-KEY-123', raw, '响应里绝不能出现 api_key')
        self.assertNotIn('api_key', raw)

    def test_reachable_stub_model(self):
        stub = ThreadingHTTPServer(('127.0.0.1', 0), _StubModel)
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        host, port = stub.server_address
        _, base = self._start_server(self._ws(),
                                     'http://%s:%d/v1' % (host, port))
        raw, d = self._get(base)
        self.assertTrue(d['reachable'])
        self.assertEqual(d['model'], 'qwen')

    def test_port_zero_rejected(self):
        _, base = self._start_server(self._ws(), 'http://127.0.0.1:0/v1')
        _, d = self._get(base)
        self.assertFalse(d['reachable'])
        self.assertIn('端口无效', d['error'])

    def test_port_out_of_range_rejected(self):
        _, base = self._start_server(self._ws(), 'http://127.0.0.1:70000/v1')
        _, d = self._get(base)
        self.assertFalse(d['reachable'])
        self.assertIn('端口无效', d['error'])

    def test_non_numeric_port_rejected(self):
        _, base = self._start_server(self._ws(), 'http://127.0.0.1:abc/v1')
        _, d = self._get(base)
        self.assertFalse(d['reachable'])
        self.assertIn('端口无效', d['error'])

    def test_invalid_scheme_rejected(self):
        _, base = self._start_server(self._ws(), 'ftp://127.0.0.1:8000/v1')
        _, d = self._get(base)
        self.assertFalse(d['reachable'])
        self.assertIn('base_url 无效', d['error'])

    def test_https_closed_port(self):
        _, base = self._start_server(self._ws(), 'https://127.0.0.1:9/v1')
        _, d = self._get(base)
        self.assertFalse(d['reachable'])
        self.assertIn('ConnectionRefusedError', d['error'])

    @unittest.skipUnless(shutil.which('openssl'), 'openssl 不可用')
    def test_https_self_signed_stub_rejected(self):
        # 探活走 HTTPSConnection 默认校验（与聊天 LLMClient 同口径），
        # 自签名证书的模型服务会被判不通；本项只证明 https 分支确实
        # 走了 TLS 握手、且失败原因就是证书校验。
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        key, cert = str(tmp / 'key.pem'), str(tmp / 'cert.pem')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                        '-keyout', key, '-out', cert, '-days', '1',
                        '-nodes', '-subj', '/CN=127.0.0.1'],
                       check=True, capture_output=True, timeout=30)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        stub = ThreadingHTTPServer(('127.0.0.1', 0), _StubModel)
        stub.socket = ctx.wrap_socket(stub.socket, server_side=True)
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        host, port = stub.server_address
        _, base = self._start_server(self._ws(),
                                     'https://%s:%d/v1' % (host, port))
        _, d = self._get(base)
        self.assertFalse(d['reachable'])
        self.assertIn('CERTIFICATE_VERIFY_FAILED', d['error'])


if __name__ == '__main__':
    unittest.main()
