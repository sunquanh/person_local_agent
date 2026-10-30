# -*- coding: utf-8 -*-
"""Agent 主循环（Plan→Act→Observe→Reflect）。

核心能力：
1. 结构化工具结果：success/error/hint 三元组，模型可感知失败原因
2. 退出条件检测：final_answer / 连续失败 / 最大轮次 / 无工具调用
3. 分级上下文压缩：近期保留原文，中期提取工具摘要，早期只保留关键事实
4. 并行只读工具：同一轮多个 read_file/grep 等并发执行（延迟从 N 倍降到 1 倍）
5. Todo 清单：长任务有可见进度（替代旧版只回显文字的 plan）
6. ask_user：需求含糊时向用户提问，而不是瞎猜
7. 本轮快照：所有写入可一键撤销（workspace.begin_turn/end_turn）
8. 子 Agent 委托：复杂任务可分解为独立子任务（子 Agent 禁止再委托，防递归）
"""
import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import logsetup
from .tools import ASK_FORBIDDEN, CODE_TOOLS, READ_ONLY_TOOLS, WRITE_TOOLS, ToolRegistry

log = logsetup.get('core')

# 工具结果回传给模型的长度上限
TOOL_RESULT_MAX = 12000
# 连续失败上限：达到后自动停止（防止无限循环）
MAX_CONSECUTIVE_FAILURES = 3
# 分级压缩阈值
COMPRESS_TIER_RECENT = 10    # 最近N轮保留原文
COMPRESS_TIER_MID = 30       # 中间轮提取工具摘要


def _truncate_tool_result(text) -> str:
    s = str(text or '')
    if len(s) <= TOOL_RESULT_MAX:
        return s
    return (s[:TOOL_RESULT_MAX] +
            f"\n\n…(工具输出过长：共 {len(s)} 字符，仅保留前 {TOOL_RESULT_MAX} 字符。"
            f"如需完整内容，请用 read_file 分段读取或指定 offset 参数继续。)")


def _wrap_tool_result(result_text: str, tool_name: str, args: dict) -> str:
    """将工具原始结果包装为结构化格式，包含 success/error/hint。

    模型通过这个格式可以：
    - 清晰知道操作是否成功
    - 理解失败的具体原因
    - 获得修复建议（hint）
    """
    text = str(result_text or '')
    is_error = text.startswith(('❌', '⏱️', '⛔'))

    # 提取 exit code（run_command 格式：$ cmd\n(exit N)\n...）
    exit_code = None
    if tool_name == 'run_command':
        import re
        m = re.search(r'\(exit (\d+)\)', text)
        if m:
            exit_code = int(m.group(1))

    # 构建结构化结果
    structured = {
        'success': not is_error,
        'output': text,
    }

    if is_error:
        # 分析错误类型并给出 hint
        hint = ''
        if '超时' in text:
            hint = '命令执行超时，可能有死循环或进程卡住。建议：1)检查命令是否正确 2)增加timeout 3)用更简单的命令测试'
        elif '找不到' in text or 'not found' in text.lower():
            hint = '文件或路径不存在。建议：先用 list_dir 确认目录结构，再用正确的路径'
        elif '拒绝' in text:
            hint = '用户拒绝执行。请尝试其他方式完成任务'
        elif exit_code is not None and exit_code != 0:
            hint = f'命令返回非零退出码 {exit_code}，请分析错误输出并修复'
        elif '语法错误' in text or 'SyntaxError' in text:
            hint = 'Python 语法错误，请检查代码并修复'
        elif '模块' in text and '找不到' in text:
            hint = '缺少 Python 模块，可能是未安装的依赖'

        if hint:
            structured['hint'] = hint
        structured['error_type'] = 'timeout' if '超时' in text else \
                                   'not_found' if '找不到' in text else \
                                   'rejected' if '拒绝' in text else \
                                   'runtime' if exit_code and exit_code != 0 else 'unknown'
    else:
        if tool_name == 'run_command' and exit_code is not None and exit_code != 0:
            structured['success'] = False
            structured['error_type'] = 'non_zero_exit'
            structured['hint'] = f'命令返回退出码 {exit_code}，请分析输出并修复'

    return json.dumps(structured, ensure_ascii=False)


def _as_text(user_input) -> str:
    """user_input 可能是纯文本，也可能是多模态 parts 列表。"""
    if isinstance(user_input, str):
        return user_input
    if isinstance(user_input, list):
        return ' '.join(p.get('text', '') for p in user_input
                        if isinstance(p, dict) and p.get('type') == 'text')
    return str(user_input or '')


def estimate_tokens(text) -> int:
    """粗略 token 估算（中文按 0.75、其它按 0.28 token/字符）。

    只用于压缩触发与 UI 预算提示；真实用量以服务端返回的 usage.prompt_tokens
    为准（每次拿到后都会覆盖估算值）。
    """
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text or '')
    if not text:
        return 0
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return int(cjk * 0.75 + (len(text) - cjk) * 0.28) + 1


def estimate_messages(messages) -> int:
    total = 0
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        c = m.get('content')
        if isinstance(c, list):
            total += sum(estimate_tokens(p.get('text', '')) for p in c
                         if isinstance(p, dict))
        else:
            total += estimate_tokens(c)
        for tc in (m.get('tool_calls') or []):
            total += estimate_tokens(json.dumps(tc, ensure_ascii=False))
    return total


# ── 分级上下文压缩：自动压缩与手动「立即压缩」共用同一套摘要规则 ──────
def _summarize_early(early: list) -> str:
    """30 条以前的轮次：只保留用户意图与助手结论（各截前 100 / 150 字）。"""
    facts = []
    for m in early or []:
        role = m.get('role', '')
        content = m.get('content') or ''
        if role == 'user' and content.strip():
            facts.append(content[:100])
        elif role == 'assistant' and not m.get('tool_calls') and content.strip():
            facts.append(content[:150])
    if not facts:
        return ''
    return "【早期对话要点】\n" + "\n".join(f"- {f}" for f in facts[-8:])


def _summarize_mid(mid: list) -> str:
    """中间轮次：只留工具结果摘要（成功/失败 + 输出前 200 字）。"""
    lines = []
    for m in mid or []:
        role = m.get('role', '')
        content = m.get('content') or ''
        if role == 'tool' and content.strip():
            try:
                parsed = json.loads(content)
                success = parsed.get('success', True)
                output = str(parsed.get('output', ''))[:200]
                lines.append(f"[工具{'✓' if success else '✗'}] {output}")
            except (json.JSONDecodeError, TypeError):
                lines.append(f"[工具] {content[:200]}")
        elif role == 'user' and content.strip():
            lines.append(f"[用户] {content[:100]}")
    if not lines:
        return ''
    return "【中间操作摘要】\n" + "\n".join(lines[-12:])


def compact_history(history: list, keep_recent: int = COMPRESS_TIER_RECENT):
    """把历史压成 (摘要消息列表, 最近 N 条原文, 压缩点下标)。

    - 最近 ``keep_recent`` 条完全保留（不丢细节）
    - 中间段只留工具结果摘要
    - 更早的只留用户意图与关键结论

    手动压缩（POST /api/compact）用它生成「摘要 + 压缩点」并落盘，
    自动压缩把它当真：两边摘要口径一致，不会出现“手动压完还超窗”。
    """
    history = list(history or [])
    keep_recent = max(1, int(keep_recent or COMPRESS_TIER_RECENT))
    if len(history) <= keep_recent:
        return [], history, 0
    cut = len(history) - keep_recent
    boundary = max(0, len(history) - COMPRESS_TIER_MID)
    msgs = []
    early_txt = _summarize_early(history[:boundary])
    if early_txt:
        msgs.append({"role": "user", "content": early_txt})
    mid_txt = _summarize_mid(history[boundary:cut])
    if mid_txt:
        msgs.append({"role": "user", "content": mid_txt})
    return msgs, history[cut:], cut


class Agent:
    def __init__(self, ws, llm, memory, confirm_callback=None,
                 rag=None, rag_top_k: int = 4, compress_threshold: int = 0,
                 allow_code_exec: bool = True,
                 role_text: str = '', rules=None,
                 ctx=None, allow_delegate: bool = True, vision: bool = False,
                 context_window: int = 0, max_turns: int = 25,
                 profile: str = 'suggest', auto_approve = None, session_allow = None):
        self.ws = ws
        self.llm = llm
        self.memory = memory
        self.rag = rag or None
        self.rag_top_k = rag_top_k
        self.compress_threshold = compress_threshold
        # 自定义角色提示词 + 铁律
        self.role_text = (role_text or '').strip()
        self.rules = [str(r).strip() for r in (rules or []) if str(r).strip()]
        # 会话上下文（每请求一个 Agent，线程局部注入 workspace，避免串会话）
        self.ctx = ctx
        self.max_turns = max(1, int(max_turns or 25))
        # 上下文预算（0 = 不启用，只按字符阈值压缩）
        self.context_window = max(0, int(context_window or 0))
        self._usage = {}
        self._todos = []
        self._rag_hits = []
        self._compacted = False
        self._ctx_stats = {}
        self.registry = ToolRegistry(ws, memory, confirm_callback,
                                     allow_code_exec=allow_code_exec, llm=llm,
                                     hooks=self, allow_delegate=allow_delegate,
                                     vision=vision, profile=profile,
                                     auto_approve=auto_approve,
                                     session_allow=session_allow)
        self._stop = threading.Event()
        self._read_only = False
        # GUI 回调
        self.on_delta = None      # (text)
        self.on_tool = None       # (name, args, ok, result_text, artifacts, ms)
        self.on_tool_start = None  # (name, args) —— 工具开始执行（前端显示"正在…"）
        self.on_think = None      # (text) 状态提示
        self.on_todo = None       # (items) 任务清单变化
        self.on_ask = None        # (question, options) -> 用户回答（阻塞）
        self._is_sub_agent = False  # 标记是否为子Agent

    # ── 停止 ──────────────────────────────────────────────────────
    def request_stop(self):
        self._stop.set()
        self.llm.abort()

    def reset_stop(self):
        self._stop.clear()

    def update_todos(self, items):
        """任务清单唯一入口：记忆 + 通知前端（todo / plan 工具都走这里）。"""
        self._todos = [i for i in (items or []) if isinstance(i, dict) and i.get('text')]
        if self.on_todo:
            try:
                self.on_todo(self._todos)
            except Exception:
                pass

    def stop_requested(self):
        return self._stop.is_set()

    # ── 退出条件检测 ──────────────────────────────────────────────
    def _check_exit_conditions(self, messages, consecutive_failures):
        """检测是否应该结束Agent循环。

        退出条件（任一满足即停）：
        1. 最近一条assistant消息调用了 final_answer 工具
        2. 连续失败次数达到上限
        """
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            return True, 'consecutive_failures'

        if not messages:
            return False, ''
        for msg in reversed(messages[-6:]):
            if msg.get('role') == 'assistant' and msg.get('tool_calls'):
                for tc in msg['tool_calls']:
                    # 兼容两种格式：嵌套 {function: {name: ...}} 和扁平 {name: ...}
                    func_name = (tc.get('function', {}).get('name') if isinstance(tc.get('function'), dict)
                                 else None) or tc.get('name', '')
                    if func_name == 'final_answer':
                        return True, 'final_answer'
            if msg.get('role') == 'assistant':
                break

        return False, ''

    # ── 主循环 ────────────────────────────────────────────────────
    def run(self, history: list, user_input, mode: str = 'craft',
            max_turns: int = None) -> dict:
        """执行一轮对话。user_input 可为 str 或多模态 parts 列表。"""
        if self.ctx is not None:
            self.ws.set_session(self.ctx)
        snap = None
        try:
            if self.ctx is not None:
                try:
                    self.ws.begin_turn(self.ctx.id)
                except Exception:
                    pass
            result = self._run_loop(history, user_input, mode,
                                    int(max_turns or self.max_turns))
        finally:
            try:
                snap = self.ws.end_turn()
            except Exception:
                snap = None
            if self.ctx is not None:
                self.ws.clear_session()
        if snap:
            result['snapshot'] = snap
        return result

    def _run_loop(self, history, user_input, mode, max_turns):
        # 只读 = 用户选了只读模式，或审批档位设为 readonly
        self._read_only = (mode == 'ask' or self.registry.profile == 'readonly')
        if self._read_only:
            tools = [t.schema() for t in self.registry.tools.values()
                     if t.name not in ASK_FORBIDDEN]
        else:
            tools = self.registry.schemas()

        self._usage = {}
        self._compacted = False
        self._rag_hits = []
        self._ctx_stats = {}
        system = self._build_system(_as_text(user_input))
        history = self._compress_history(history, reserve=estimate_tokens(system) + 1500)
        messages = [{"role": "system", "content": system}]
        messages += self._history_messages(history)
        messages.append({"role": "user", "content": user_input})
        self._refresh_context(messages)

        tool_steps = []
        artifacts = []
        consecutive_failures = 0

        for turn in range(max_turns):
            if self.stop_requested():
                return self._finish(tool_steps, artifacts, stopped=True)

            if self.on_think:
                self.on_think(f"思考中…（第 {turn + 1} 轮）")

            # 思考内容实时推送
            reasoning_buf = []

            def _on_reason(t):
                reasoning_buf.append(t)
                if self.on_think:
                    self.on_think(t)

            r = self.llm.chat(messages, tools=tools, on_delta=self.on_delta,
                              on_reason=_on_reason)

            if r.get('usage'):
                self._usage = r['usage']
                self._refresh_context(messages, actual=self._usage.get('prompt_tokens'))

            if r.get('reasoning') and not reasoning_buf and self.on_think:
                self.on_think(r['reasoning'])

            if self.stop_requested():
                return self._finish(tool_steps, artifacts, stopped=True)

            calls = r.get('tool_calls') or []

            # ── 无工具调用：正常回复结束 ──
            if not calls:
                return self._finish(tool_steps, artifacts, stopped=False,
                                    response=r.get('content') or '',
                                    reasoning=r.get('reasoning') or '')

            # 记录 assistant 的 tool_calls
            messages.append({
                "role": "assistant",
                "content": r.get('content') or None,
                "tool_calls": [{
                    "id": c["id"], "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                } for c in calls],
            })

            # ── 执行工具调用（只读类可并行）──
            results = self._exec_batch(calls, tool_steps, artifacts)
            for c in calls:
                if self.stop_requested():
                    return self._finish(tool_steps, artifacts, stopped=True)
                name, res = c['name'], results.get(c['id'])
                ok = res.get('ok', False)
                if ok:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                structured = _wrap_tool_result(res.get('text', ''), name, res.get('args') or {})
                messages.append({
                    "role": "tool",
                    "tool_call_id": c['id'],
                    "content": _truncate_tool_result(structured),
                })
                # 多模态：read_image 命中时把图片作为用户消息追加，让模型真正"看到"
                img = res.get('image')
                if img and not self._read_only:
                    part = self._image_message(img)
                    if part:
                        messages.append(part)

            # ── 退出条件检测 ──
            should_exit, reason = self._check_exit_conditions(
                messages, consecutive_failures)
            if should_exit:
                if reason == 'final_answer':
                    for msg in reversed(messages[-8:]):
                        if msg.get('role') == 'assistant' and msg.get('tool_calls'):
                            for tc in msg['tool_calls']:
                                func_name = (tc.get('function', {}).get('name')
                                             if isinstance(tc.get('function'), dict)
                                             else None) or tc.get('name', '')
                                func_args = (tc.get('function', {}).get('arguments', '{}')
                                             if isinstance(tc.get('function'), dict)
                                             else tc.get('arguments', '{}'))
                                if func_name == 'final_answer':
                                    try:
                                        fa_args = json.loads(func_args)
                                    except Exception:
                                        fa_args = {}
                                    return self._finish(
                                        tool_steps, artifacts, stopped=False,
                                        response=fa_args.get('answer', ''),
                                        reasoning=r.get('reasoning') or '')
                elif reason == 'consecutive_failures':
                    return self._finish(
                        tool_steps, artifacts, stopped=False,
                        response=f"⚠️ 连续 {MAX_CONSECUTIVE_FAILURES} 次工具调用失败，任务中止。"
                                 f"请检查错误信息并重试。")
                break

        # 轮次耗尽
        return self._finish(tool_steps, artifacts, stopped=False,
                            response="⏹️ 已到达最大轮次，任务可能未完成，请重试或拆分任务。")

    # ── 工具批执行（只读并行）────────────────────────────────────
    def _exec_batch(self, calls, tool_steps, artifacts) -> dict:
        """执行一批工具调用：只读类并行，其余串行；结果按调用顺序统一登记。

        _exec_one 只做执行、不碰共享状态（tool_steps/回调），因此可安全并发；
        登记与回调统一回到主线程按顺序做，保证前端看到的顺序与模型发起的一致。
        """
        out = {}
        names = [c['name'] for c in calls]
        parallel = (len(calls) > 1
                    and all(n in READ_ONLY_TOOLS for n in names)
                    and not self._read_only)
        if parallel:
            with ThreadPoolExecutor(max_workers=min(4, len(calls))) as ex:
                futs = {c['id']: ex.submit(self._exec_one, c) for c in calls}
                for c in calls:
                    try:
                        out[c['id']] = futs[c['id']].result()
                    except Exception as e:
                        out[c['id']] = self._failed(c, f"❌ {e}")
        else:
            for c in calls:
                out[c['id']] = self._exec_one(c)

        for c in calls:
            r = out.get(c['id']) or self._failed(c, "❌ 工具执行异常")
            tool_steps.append(r['step'])
            for a in r['artifacts']:
                if a not in artifacts:
                    artifacts.append(a)
            if self.on_tool:
                try:
                    self.on_tool(c['name'], r['args'], r['ok'], r['text'],
                                 r['artifacts'], r.get('ms'))
                except Exception:
                    pass
        return out

    @staticmethod
    def _failed(c, text):
        return {'ok': False, 'text': text, 'artifacts': [], 'args': {}, 'image': None,
                'ms': 0,
                'step': {"tool": c['name'], "args": {}, "result": text,
                         "ok": False, "artifacts": [], "ms": 0}}

    def _exec_one(self, c) -> dict:
        """执行单个工具调用（可能运行在线程池里，需自己设置线程局部 ctx）。"""
        if self.ctx is not None:
            self.ws.set_session(self.ctx)
        name, args_txt = c['name'], c.get('arguments') or ''
        try:
            args = json.loads(args_txt) if args_txt.strip() else {}
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}

        if self.on_tool_start:
            try:
                self.on_tool_start(name, args)
            except Exception:
                pass

        tool = self.registry.get(name)
        image = None
        ms = 0
        if tool is None:
            result_text = f"❌ 未知工具: {name}"
            ok, arts = False, []
        elif self._read_only and name in ASK_FORBIDDEN:
            result_text = "⛔ 当前为只读模式，禁止执行该操作"
            ok, arts = False, []
        elif not self._approve_tool(name, args):
            result_text = "⛔ 用户拒绝执行该操作"
            ok, arts = False, []
        else:
            # 单步耗时（U：工具时间线显示每步用了多久；不含用户确认的等待时间）
            t0 = time.monotonic()
            res = tool.execute(args)
            ms = int((time.monotonic() - t0) * 1000)
            result_text = res.text
            arts = res.artifacts
            image = getattr(res, 'image', None)
            ok = not result_text.startswith(("❌", "⛔", "⏱️"))

        return {'ok': ok, 'text': result_text, 'artifacts': arts,
                'args': args, 'image': image, 'ms': ms,
                'step': {"tool": name, "args": args, "result": result_text,
                         "ok": ok, "artifacts": arts, "ms": ms}}

    def _approve_tool(self, name: str, args: dict) -> bool:
        """审批档位门禁（B4）：写/删/移与命令按档位自动放行，否则弹确认框。

        安全取向：审批逻辑自身出错时拒绝（fail-closed），绝不静默放行。
        """
        reg = self.registry
        try:
            if name.startswith('mcp__'):
                # MCP 工具（B5）：安全边界从内置工具扩大到任意子进程，默认逐条审批
                if (self._read_only or reg.profile == 'readonly'
                        or reg.confirm_callback is None):
                    return False
                brief = ', '.join(f'{k}={str(v)[:60]}'
                                  for k, v in list((args or {}).items())[:6])
                return bool(reg.confirm_callback(f'MCP 工具 {name}({brief})'))
            if name in CODE_TOOLS:
                cmd = str((args or {}).get('command') or (args or {}).get('code') or '')
                if name == 'execute_python':
                    # 代码执行不走 shell 命令的白名单/记忆通道：_is_dangerous
                    # 是 shell 正则，对代码文本没有拦截意义；除 trusted 档
                    # （用户已选“全部自动”）外一律人工确认。
                    if reg.profile == 'trusted':
                        return True
                    if reg.confirm_callback is None:
                        return False
                    return bool(reg.confirm_callback(f"执行代码：{cmd[:400]}"))
                if reg.cmd_auto_approved(cmd):
                    return True
                if reg.confirm_callback is None:
                    return False
                label = (f"执行代码：{cmd[:400]}" if name == 'execute_python'
                         else f"执行命令：{cmd[:400]}")
                if not reg.confirm_callback(label):
                    return False
                reg.remember_cmd(cmd)
                reg.pre_approved.add(cmd)
                return True
            if name in WRITE_TOOLS:
                path = str((args or {}).get('path') or (args or {}).get('to') or '')
                if reg.write_auto_approved(name, path):
                    return True
                if reg.confirm_callback is None:
                    return False
                verb = {'delete_file': '删除', 'move_file': '移动/覆盖',
                        'mkdir': '创建目录'}.get(name, '写入')
                return bool(reg.confirm_callback(f'{verb}文件：{path}'))
        except Exception as e:
            try:
                from . import logsetup
                logsetup.get('core').error('审批逻辑异常（已拒绝）: %s', e)
            except Exception:
                pass
            return False
        return True

    def _image_message(self, path: str):
        """把图片编码成 OpenAI 兼容的 image_url 消息（多模态模型）。"""
        try:
            import os
            size = os.path.getsize(path)
            if size > 8 * 1024 * 1024:
                return None
            with open(path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode('ascii')
            ext = str(path).rsplit('.', 1)[-1].lower()
            mime = 'image/png' if ext == 'png' else 'image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}'
            return {"role": "user", "content": [
                {"type": "text", "text": f"（这是图片 {os.path.basename(path)} 的内容）"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}
        except Exception:
            return None

    def _refresh_context(self, messages, actual=None):
        """更新本轮上下文占用（真实 prompt_tokens 优先）。"""
        est = estimate_messages(messages)
        used = int(actual or est)
        limit = self.context_window
        self._ctx_stats = {
            "used": used,
            "estimated": est,
            "limit": limit,
            "ratio": round(used / limit, 3) if limit else 0,
            "compacted": bool(self._compacted),
        }

    def _finish(self, tool_steps, artifacts, stopped, response="", reasoning=""):
        return {
            "response": response,
            "reasoning": reasoning,
            "tool_steps": tool_steps,
            "artifacts": artifacts,
            "stopped": stopped,
            "usage": self._usage or {},
            "todos": list(self._todos or []),
            "context": dict(self._ctx_stats or {}),
            "citations": list(self._rag_hits or []),
        }

    # ── 历史消息（补上轮工具摘要，模型才知道自己做过什么）────────────
    def _history_messages(self, history: list) -> list:
        out = []
        for m in (history or []):
            role = m.get('role')
            if role not in ('user', 'assistant'):
                continue
            content = m.get('content') or ''
            steps = m.get('tool_steps') or []
            if role == 'assistant' and steps:
                brief = '；'.join(
                    f"{s.get('tool')}{'✓' if s.get('ok') else '✗'}"
                    for s in steps[:8])
                content = (content + f"\n\n[上一轮调用过：{brief}]").strip()
            out.append({"role": role, "content": content})
        return out

    # ── 分级上下文压缩 ──────────────────────────────────────────
    def _compress_history(self, history: list, reserve: int = 0) -> list:
        """分级压缩：近期保留原文，中期提取工具摘要，早期只保留关键事实。

        - 近 10 条完全保留（不丢失任何细节）
        - 10-30 条：只保留工具调用摘要（成功/失败+关键输出）
        - 30 条以前：只保留用户意图和关键事实
        - 不再需要额外的 LLM 调用来压缩（省延迟+成本）
        """
        threshold = self.compress_threshold
        budget = max(0, int(self.context_window * 0.7) - int(reserve or 0)) \
            if self.context_window else 0
        if (threshold <= 0 and not budget) or not history:
            return history
        total = sum(len(m.get('content') or '') for m in history)
        est = estimate_messages(history)
        if (threshold <= 0 or total <= threshold) and (not budget or est <= budget):
            return history
        self._compacted = True
        # 接近上限时压缩得更狠：近期原文从 10 条降到 6 条
        recent_n = 6 if (budget and est > budget * 1.15) else COMPRESS_TIER_RECENT
        prefix, recent, _ = compact_history(history, recent_n)
        return prefix + recent

    # ── 项目指令（AGENTS.md，对标 Codex / Cline 的约定）────────────
    def _project_instructions(self) -> str:
        """读取工作台里的项目指令：AGENTS.md -> notes/AGENTS.md -> .agent/rules.md。

        这是"不同项目不同规矩"的落点，避免所有规则都挤进全局铁律。
        单文件上限 4000 字符，防止把上下文打爆。
        """
        parts = []
        for rel in ('AGENTS.md', 'notes/AGENTS.md', '.agent/rules.md'):
            try:
                p = self.ws.root / rel
                if not p.is_file():
                    continue
                # 项目指令直接进 system prompt：文件本身或路径中任何一段是
                # 符号链接时跳过（否则链接可指向工作台外任意文件，敏感内容
                # 进上下文、恶意指令进系统提示词），与其它路径校验口径一致。
                self.ws._reject_symlinks(p)
                txt = p.read_text(encoding='utf-8', errors='replace').strip()
            except (OSError, PermissionError) as e:
                log.warning('项目指令 %s 未读入（%s）', rel, e)
                continue
            if not txt:
                continue
            if len(txt) > 4000:
                txt = txt[:4000] + '\n…(项目指令过长，已截断)'
            parts.append(f"【{rel}】\n{txt}")
        if not parts:
            return ''
        return "\n\n【项目指令】（用户放在工作台里的规则，优先级高于通用做事方式）\n" + "\n\n".join(parts)

    # ── system prompt ─────────────────────────────────────────────
    def _build_system(self, user_input: str) -> str:
        mem_ctx = self.memory.build_context(user_input)
        rag_ctx = ''
        if self.rag and user_input.strip():
            try:
                hits = self.rag.search(user_input, self.rag_top_k)
                if hits:
                    lines = [f"- 【{h['path']}】{h['snippet']}" for h in hits]
                    rag_ctx = ("\n\n【本地资料库（来自 inputs/notes 里你的文档，可据此回答并注明来源）】\n"
                               + "\n".join(lines))
                    # 引用信息随结果回传前端，答案下方显示来源（用户能核对，不是黑盒）
                    self._rag_hits = [{"rel": h.get('path', ''),
                                       "snippet": str(h.get('snippet') or '')[:180],
                                       "score": round(float(h.get('score') or 0), 3)}
                                      for h in hits[:6]]
            except Exception as e:
                log.warning('RAG 检索失败: %s', e)
                rag_ctx = ''
        project_txt = self._project_instructions()
        mode_txt = ("\n【当前模式】只读：只读、分析、回答问题；禁止写文件、执行命令、删除文件。"
                    if self._read_only else "")
        role_line = (self.role_text if self.role_text else
                     "你是部署在用户个人工作台里的 AI 助手，帮助用户处理办公文档、代码开发和日常资料整理。")
        rules_txt = ''
        if self.rules:
            numbered = "\n".join(f"{i}. {r}" for i, r in enumerate(self.rules, 1))
            rules_txt = ("\n\n【铁律】（用户设定的必须遵守的规则，任何时候都不得违反）\n"
                         + numbered)
        return f"""{role_line}

【个人工作台】工作台是用户日常工作的空间（根目录 {self.ws.root}）：
- inputs/  —— 用户放进去的输入素材（网页上传也落这里）
- outputs/ —— 你的产出物目录。你写 outputs/xxx 会自动保存到 outputs/<日期>/<会话标题>/xxx，
  工具返回的结果里会给出真实保存路径，回复用户时请引用真实路径
- notes/   —— 长期知识库，你可以新建/编辑笔记
- .agent/  —— 系统内部状态，不要读写
【写入白名单】只能写 outputs/ 或 notes/；写 inputs/、.agent/、系统目录、绝对路径都会被拒绝。
【读取】可以读工作台内任意文件，也可以读电脑其他目录（只读）；敏感文件（.env/.key/凭据）会被拦截。
【产出物】每次生成/修改的文件都要登记（工具会自动登记），并在回复里告诉用户文件在哪。

【做事方式】
1. 简单问答（一句话能答的）直接回答，不要调用任何工具。
2. 复杂任务先调用 todo 列出步骤清单，然后逐步执行；每完成一步就更新 todo 状态（done/doing）。
3. 工具返回的是 JSON：success=false 表示失败，请看 hint 分析原因后修复再重试，不要原样重复同一调用。
4. 同一轮需要读多个文件/搜索多处时，可以一次发起多个只读工具调用（会并行执行，更快）。
5. 需要用户确认关键信息（目标格式、范围、取舍）时用 ask_user 提问，不要靠猜。
6. 所有步骤完成后直接回复结论；也可调用 final_answer 收尾。回复里说明产物路径。
7. 连续失败时不要盲目重试，先分析原因或换一种做法。
{rag_ctx}
{mem_ctx}
{project_txt}
{rules_txt}
{mode_txt}

【风格】回复用中文，简洁、结构化。办公任务优先用专业文档工具（word/excel/ppt）。
需要展示内部推理时用 <think>...</think> 包裹（标签内不写最终答案），系统会自动折叠展示。"""
