# -*- coding: utf-8 -*-
"""后端回归测试（标准库 unittest，无第三方依赖）。

覆盖的是“改错了就是事故或返工”的部分：
- 路径边界（写白名单 / 符号链接 / 会话 id）
- 审批档位矩阵（危险命令永不自动放行）
- 会话库（CRUD、检索、旧 JSON 迁移、不再丢第 401 条）
- 上下文压缩（保留路径/数字）与提示词顺序（静态前缀 + 动态上下文）
- 工具结果契约（结构化 error_type，不靠文案猜）
- 模型槽位优先级

跑法：
    python -m unittest discover -s tests -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.core import Agent, _key_lines, compact_history, _wrap_tool_result  # noqa: E402
from agent.memory import Memory  # noqa: E402
from agent.tools import ToolRegistry, ToolResult  # noqa: E402
from agent.workspace import SessionCtx, Workspace  # noqa: E402
from server.runs import ModelGate, RunRegistry  # noqa: E402
from server.sessions import SessionManager  # noqa: E402


class FakeLLM:
    """只记录收到的 messages，不做网络请求。"""

    def __init__(self, reply='好', tool_calls=None):
        self.calls = []
        self.reply = reply
        self.tool_calls = tool_calls or []

    def chat(self, messages, tools=None, on_delta=None, on_reason=None,
             temperature=0.7):
        self.calls.append(list(messages))
        if on_delta and self.reply:
            on_delta(self.reply)
        return {"content": self.reply, "reasoning": "", "usage": {},
                "tool_calls": list(self.tool_calls)}

    def abort(self):
        pass


def make_agent(tmp: Path, reply='好', tool_calls=None):
    ws = Workspace(str(tmp / 'ws'))
    mem = Memory(str(tmp / 'ws' / '.agent' / 'memory.db'))
    llm = FakeLLM(reply, tool_calls)
    ag = Agent(ws, llm, mem, context_window=32768, max_turns=2)
    return ag, ws, mem, llm


class TestPathBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = Workspace(str(self.tmp / 'ws'))
        self.ws.set_session(SessionCtx('t1', '测试', '2026-01-01'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_outside_whitelist_rejected(self):
        self.ws.begin_turn('t1')
        for rel in ('inputs/x.txt', '.agent/x.txt', '../escape.txt', '/tmp/abs.txt'):
            with self.assertRaises(PermissionError, msg=rel):
                self.ws.resolve_write(rel)

    def test_write_whitelist_allows_outputs_and_notes(self):
        real, rel = self.ws.resolve_write('notes/a.md')
        self.assertTrue(str(real).startswith(str(self.ws.root)))
        self.assertTrue(rel.startswith('notes/'))
        r2, rel2 = self.ws.resolve_write('outputs/a.md')
        self.assertTrue(rel2.startswith('outputs/'))

    def test_symlink_rejected(self):
        target = self.tmp / 'secret.txt'
        target.write_text('secret', encoding='utf-8')
        link = self.ws.root / 'notes' / 'link.txt'
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError) as e:
            self.skipTest(f'当前环境无法创建符号链接: {e}')
        with self.assertRaises(PermissionError):
            self.ws.resolve_read('notes/link.txt')


class TestApprovalMatrix(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = Workspace(str(self.tmp / 'ws'))
        self.mem = Memory(str(self.tmp / 'ws' / '.agent' / 'memory.db'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reg(self, profile, auto=None):
        return ToolRegistry(self.ws, self.mem, confirm_callback=lambda c: True,
                            profile=profile, auto_approve=auto)

    def test_profiles(self):
        ro = self._reg('readonly')
        self.assertFalse(ro.write_auto_approved('write_file', 'outputs/a.md'))
        self.assertFalse(ro.cmd_auto_approved('dir'))
        sg = self._reg('suggest')
        self.assertTrue(sg.write_auto_approved('write_file', 'outputs/a.md'))
        self.assertTrue(sg.write_auto_approved('write_file', 'notes/a.md'))
        self.assertFalse(sg.write_auto_approved('delete_file', 'outputs/a.md'))
        self.assertTrue(sg.cmd_auto_approved('dir'))
        ae = self._reg('auto_edit')
        self.assertTrue(ae.write_auto_approved('delete_file', 'outputs/a.md'))
        self.assertTrue(ae.cmd_auto_approved('dir'))      # 只读命令仍放行
        self.assertFalse(ae.cmd_auto_approved('python build.py'))  # 其它命令逐条确认

    def test_dangerous_never_auto(self):
        for profile in ('suggest', 'auto_edit', 'trusted'):
            reg = self._reg(profile, {'run_other_cmds': True})
            self.assertFalse(reg.cmd_auto_approved('rm -rf /tmp/x'), profile)
            self.assertFalse(reg.cmd_auto_approved('git push origin main'), profile)
        trusted = self._reg('trusted')
        self.assertTrue(trusted.cmd_auto_approved('dir'))

    def test_shell_meta_not_whitelisted(self):
        reg = self._reg('suggest')
        self.assertFalse(reg.cmd_auto_approved('echo hi > outputs/x.txt'))
        self.assertFalse(reg.cmd_auto_approved('dir && del a'))


class TestToolContract(unittest.TestCase):
    def test_error_type_from_toolresult(self):
        text = _wrap_tool_result('❌ 炸了', 'write_file', {},
                                 meta={'ok': False, 'code': 'permission',
                                       'hint': '换个目录'})
        d = json.loads(text)
        self.assertFalse(d['success'])
        self.assertEqual(d['error_type'], 'permission')
        self.assertEqual(d['hint'], '换个目录')

    def test_success_does_not_invent_hint(self):
        d = json.loads(_wrap_tool_result('已写入 outputs/a.md', 'write_file', {},
                                         meta={'ok': True}))
        self.assertTrue(d['success'])
        self.assertNotIn('hint', d)

    def test_toolresult_infers_ok_from_prefix(self):
        self.assertFalse(ToolResult('❌ 失败').ok)
        self.assertTrue(ToolResult('✅ 成功').ok)
        self.assertFalse(ToolResult.fail('不存在的路径').ok)
        self.assertEqual(ToolResult.fail('x', code='timeout').code, 'timeout')


class TestCompaction(unittest.TestCase):
    def test_key_lines_keeps_paths_and_numbers(self):
        picked = _key_lines('先寒暄一句。已生成 outputs/2026/周报.md，共 12 条记录。'
                            '另外随便聊聊天气。', 3)
        self.assertTrue(any('outputs/2026/周报.md' in x for x in picked))
        self.assertTrue(any('12' in x for x in picked))

    def test_compact_keeps_recent_and_summarises_early(self):
        hist = [{'role': 'user', 'content': '把表整理成 outputs/报表.xlsx'},
                {'role': 'assistant', 'content': '已生成 outputs/报表.xlsx，共 30 行'}]
        hist += [{'role': 'user', 'content': f'第{i}轮'} for i in range(9)]
        prefix, recent, cut = compact_history(hist, keep_recent=4)
        self.assertEqual(recent, hist[cut:])
        body = '\n'.join(m['content'] for m in prefix)
        self.assertIn('outputs/报表.xlsx', body)
        self.assertNotIn('第8轮', body)


class TestPromptOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_system_is_static_and_context_is_separate(self):
        ag, ws, mem, llm = make_agent(self.tmp)
        mem.save_fact('用户负责每周五发周报，路径 notes/周报.md')
        (ws.root / 'AGENTS.md').write_text('金额保留两位小数', encoding='utf-8')
        ag.run([], '帮我写周报')
        msgs = llm.calls[0]
        self.assertEqual(msgs[0]['role'], 'system')
        system = msgs[0]['content']
        self.assertNotIn('周报.md', system)          # 记忆不得进 system
        self.assertNotIn('两位小数', system)          # 项目指令也不进
        # 历史之后、用户消息之前那条 user 是动态上下文
        roles = [m['role'] for m in msgs]
        self.assertEqual(roles[-1], 'user')
        self.assertEqual(msgs[-1]['content'], '帮我写周报')
        ctx = msgs[-2]
        self.assertEqual(ctx['role'], 'user')
        self.assertIn('周报.md', ctx['content'])
        self.assertIn('两位小数', ctx['content'])

    def test_history_keeps_recent_tool_results(self):
        ag, ws, mem, llm = make_agent(self.tmp)
        hist = [{'role': 'user', 'content': '更早的问题'},
                {'role': 'assistant', 'content': '更早的回答',
                 'tool_steps': [{'tool': 'read_file', 'ok': True, 'result': 'old'}]}]
        hist += [{'role': 'user', 'content': 'x'}, {'role': 'assistant', 'content': 'y'}] * 5
        hist += [{'role': 'user', 'content': '读一下表'},
                 {'role': 'assistant', 'content': '读完了',
                  'tool_steps': [{'tool': 'read_excel', 'ok': True,
                                  'result': 'A1: 姓名 B1: 金额'}]}]
        out = ag._history_messages(hist)
        joined = '\n'.join(m['content'] for m in out)
        self.assertIn('A1: 姓名', joined)   # 最近一轮的读取结果还在
        # 更早的轮次只留摘要
        self.assertIn('上一轮调用过', joined)


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sm = SessionManager(str(self.tmp / 'agent'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_crud_and_search(self):
        s = self.sm.create('周报 会话')
        self.sm.add_message(s.id, 'user', '帮我整理周报')
        self.sm.add_message(s.id, 'assistant', '已生成 outputs/周报.md',
                            extra={'tool_steps': [{'tool': 'write_file', 'ok': False}]})
        self.sm.register_artifacts(s.id, ['outputs/周报.md'])
        meta = [x for x in self.sm.list_all() if x['id'] == s.id][0]
        self.assertEqual(meta['msg_count'], 2)
        self.assertEqual(meta['artifact_count'], 1)
        self.assertEqual(meta['fail_count'], 1)
        self.assertEqual(meta['preview'], '已生成 outputs/周报.md')
        hits = self.sm.search('周报')
        self.assertTrue(hits and hits[0]['sid'] == s.id)

    def test_no_message_dropped_after_400(self):
        s = self.sm.create('长会话')
        for i in range(420):
            self.sm.add_message(s.id, 'user', f'消息{i}')
        loaded = self.sm.load(s.id)
        self.assertEqual(len(loaded.messages), 420)
        self.assertEqual(loaded.messages[0]['content'], '消息0')

    def test_truncate_pin_fork(self):
        s = self.sm.create('a')
        self.sm.add_message(s.id, 'user', 'u1')
        self.sm.add_message(s.id, 'assistant', 'a1')
        self.sm.set_pinned(s.id, True)
        self.assertTrue([x for x in self.sm.list_all() if x['id'] == s.id][0]['pinned'])
        f = self.sm.fork(s.id, 0)
        self.assertEqual(len(self.sm.load(f.id).messages), 1)
        self.assertEqual(self.sm.truncate(s.id, 1), 1)
        self.assertEqual(len(self.sm.load(s.id).messages), 1)

    def test_json_migration(self):
        tmp2 = Path(tempfile.mkdtemp())
        try:
            sid = 'abcdef123456'
            d = tmp2 / 'sessions' / sid
            d.mkdir(parents=True)
            (d / 'meta.json').write_text(json.dumps(
                {'title': '旧会话', 'created_at': '2026-01-01T00:00:00',
                 'pinned': True}), encoding='utf-8')
            (d / 'messages.json').write_text(json.dumps([
                {'role': 'user', 'content': '旧消息 内容', 'ts': 1.0},
                {'role': 'assistant', 'content': '旧回复', 'ts': 2.0,
                 'tool_steps': [{'tool': 'read_file', 'ok': True, 'result': 'x'}]},
            ], ensure_ascii=False), encoding='utf-8')
            (d / 'artifacts.json').write_text(json.dumps(['outputs/old.md']),
                                              encoding='utf-8')
            sm2 = SessionManager(str(tmp2))
            s2 = sm2.load(sid)
            self.assertEqual(s2.title, '旧会话')
            self.assertEqual(len(s2.messages), 2)
            self.assertTrue(s2.pinned)
            self.assertEqual(s2.artifacts, ['outputs/old.md'])
            self.assertTrue((d / 'messages.json.migrated').exists())
            self.assertTrue(sm2.search('旧消息'))
            # 再实例化一次不能重复导入
            sm3 = SessionManager(str(tmp2))
            self.assertEqual(len(sm3.load(sid).messages), 2)
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_bad_sid_rejected(self):
        for bad in ('../../etc', 'abc', '', 'x' * 12):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.sm.load(bad)


class TestRuns(unittest.TestCase):
    def test_gate_priority_for_waiters(self):
        g = ModelGate(1)
        self.assertTrue(g.acquire('batch', timeout=2))
        order = []

        def worker(kind):
            g.acquire(kind, timeout=5)
            order.append(kind)
            g.release()

        import threading
        t1 = threading.Thread(target=worker, args=('memory',))
        t1.start()
        time.sleep(0.05)
        t2 = threading.Thread(target=worker, args=('chat',))
        t2.start()
        time.sleep(0.05)
        g.release()
        t1.join(2)
        t2.join(2)
        self.assertEqual(order, ['chat', 'memory'])

    def test_registry_snapshot(self):
        r = RunRegistry()
        run = r.create('s1', 'me')
        run.add('delta', text='你')
        run.add('delta', text='好')
        snap = run.snapshot(1)
        self.assertEqual(snap['text_len'], 2)
        self.assertEqual(len(snap['events']), 1)
        self.assertTrue(r.running_for('s1'))
        r.finish(run, 'done')
        self.assertFalse(r.running_for('s1'))


if __name__ == '__main__':
    unittest.main()
