# -*- coding: utf-8 -*-
"""首轮可靠性：已实测缺口。

- 工具结果要有 ok / code（基线 test_toolresult_infers_ok_from_prefix 依赖）
- 批量作业必须能拿到模型槽位，不能因 AgentServer 缺少 gate 而整批失败
- 重试只重置失败项；已取消的项保持取消
- 创建作业时排除作业自己的输出目录和 .agent
"""
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.tools import ToolResult  # noqa: E402
from agent.workspace import sanitize_slug  # noqa: E402
from server.httpd import AgentServer  # noqa: E402
from tests.test_backend import FakeLLM  # noqa: E402


def _config(root: Path) -> dict:
    return {
        "model": {"base_url": "http://127.0.0.1:9/v1", "api_key": "",
                  "name": "fake", "context_window": 8192, "thinking": "collapse",
                  "max_tokens": 0},
        "workspace": {"root": str(root)},
        "server": {"host": "127.0.0.1", "port": 9, "auto_exit": False},
        "limits": {"model_slots": 1, "chat_timeout_s": 30, "llm_timeout_s": 5,
                   "max_upload_mb": 5},
        "memory": {"extract": False},
        "rag": {"enabled": False},
        "security": {"allow_code_exec": False, "profile": "suggest"},
        "prompt": {"role": "", "rules": []},
    }


class TestToolResultOk(unittest.TestCase):
    def test_prefix_and_fail_helper(self):
        self.assertFalse(ToolResult('❌ 失败').ok)
        self.assertTrue(ToolResult('✅ 成功').ok)
        self.assertTrue(ToolResult('普通输出').ok)
        failed = ToolResult.fail('不存在的路径', code='timeout')
        self.assertFalse(failed.ok)
        self.assertEqual(failed.code, 'timeout')
        self.assertTrue(failed.text.startswith('❌'))


class TestBatchGateAndRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = AgentServer(_config(self.tmp / 'ws'))
        self.app.llm = FakeLLM('已处理')

    def tearDown(self):
        self.app.batch._stop = True
        self.app.batch._wake.set()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_server_has_model_gate(self):
        self.assertTrue(hasattr(self.app, 'gate'))
        self.assertGreaterEqual(self.app.gate.slots, 1)

    def test_batch_item_does_not_fail_for_missing_gate(self):
        src = self.app.ws.root / 'inputs'
        src.mkdir(parents=True, exist_ok=True)
        f = src / 'a.txt'
        f.write_text('示例甲\n', encoding='utf-8')
        job = self.app.batch.create('摘录', '读这个文件并写一句话结论',
                                    ['inputs/a.txt'])
        deadline = time.time() + 8
        status = ''
        error = ''
        while time.time() < deadline:
            cur = self.app.batch.get(job['id'])
            item = cur['items'][0]
            status = item.get('status') or ''
            error = item.get('error') or ''
            if status in ('done', 'failed', 'canceled', 'skipped', 'uncertain'):
                break
            time.sleep(0.05)
        self.assertNotIn('gate', error)
        self.assertNotIn('AttributeError', error)
        self.assertEqual(status, 'done', error)

    def test_retry_keeps_canceled_and_reruns_failed_only(self):
        job = {
            'id': 'job-retry-1',
            'name': '重试范围',
            'template': 'x',
            'files': ['inputs/a.txt', 'inputs/b.txt'],
            'status': 'done',
            'note': '',
            'items': [
                {'idx': 0, 'file': 'inputs/a.txt', 'status': 'failed',
                 'error': 'boom', 'response': '', 'artifacts': [], 'elapsed': 1},
                {'idx': 1, 'file': 'inputs/b.txt', 'status': 'canceled',
                 'error': '已停止', 'response': '', 'artifacts': [], 'elapsed': 1},
            ],
        }
        with self.app.batch._lock:
            self.app.batch.jobs[job['id']] = job
        out = self.app.batch.control(job['id'], 'retry')
        by_file = {it['file']: it['status'] for it in out['items']}
        self.assertIn(by_file['inputs/a.txt'], ('pending', 'running', 'done', 'failed'))
        self.assertNotEqual(by_file['inputs/a.txt'], 'canceled')
        self.assertEqual(by_file['inputs/b.txt'], 'canceled')

    def test_create_excludes_own_output_and_agent_dir(self):
        root = self.app.ws.root
        (root / 'inputs').mkdir(parents=True, exist_ok=True)
        (root / 'inputs' / 'a.txt').write_text('甲', encoding='utf-8')
        name = '合同摘录'
        slug = sanitize_slug(name, 'job')
        day = time.strftime('%Y-%m-%d')
        out_rel = f'outputs/{day}/{slug}/已有.md'
        out = root / out_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('旧输出', encoding='utf-8')
        agent_rel = '.agent/jobs/note.txt'
        agent_file = root / agent_rel
        agent_file.parent.mkdir(parents=True, exist_ok=True)
        agent_file.write_text('ledger', encoding='utf-8')
        job = self.app.batch.create(
            name, '摘录',
            ['inputs/a.txt', out_rel, agent_rel])
        self.assertEqual(job['files'], ['inputs/a.txt'])
        self.assertIn(out_rel, job.get('excluded') or [])
        self.assertTrue(str(job.get('output_dir') or '').startswith(f'outputs/{day}/{slug}'))
        self.assertTrue(job.get('overwrite_rule'))
