# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 客户端（离线内网模型）。

- 流式：SSE 逐行读取，delta 内容**边到边回调**（真打字机）
  * 关键：用 resp.readline() 逐行消费，而不是 resp.read() 整包读完——
    后者会让所有 delta 在响应结束时一次性到达（表现为"卡半天，然后刷一屏"）
  * 非 SSE（服务器忽略 stream 直接返回 JSON）自动降级整包解析
  * SSE 里混入 JSON 整包帧时也能兜底
- 工具调用：流式分片按 index 聚合为完整 tool_calls
- 思考过程处理（Qwen3 等思考型模型）：
  * reasoning_content 字段（API 标准思考）-> on_reason 回调
  * 混在 content 里的 <think>...</think> / <|start_thinking|>...</|end_thinking|>
    （vLLM 等内网部署常见，标签可能被流式切片拆开、甚至只有 <think> 没有闭合）
    -> ThinkSplitter 状态机跨 chunk 拆分，思考走 on_reason，正文走 on_delta
  * 无标签正文内思考（模型把"用户希望…让我先分析…"直接写进正文）-> 保守启发式识别
  * thinking 模式: collapse=思考进折叠块(默认) / hide=彻底隐藏(并请求服务端关闭思考) / keep=原样保留
- 停止：abort() 关闭所有在途连接，任何阶段（建连/等响应头/流式中）都能立即中断
- 线程安全：每次 chat() 独立状态，可同时被前台对话、后台记忆提取、子 Agent 调用
- 重试：连接类错误与 5xx 自动重试（默认 1 次），4xx / 用户中止不重试
"""
import http.client as _http
import json
import random
import re
import socket
import threading
import time
import urllib.parse as _urlparse

from . import logsetup

log = logsetup.get('llm')

DEFAULT_TIMEOUT = 300  # 单次读超时(秒)：内网大模型首 token 常要 20-60s，120 会误杀长 prefill

# ── 思考标签识别（复用老项目 agent_fixed 的成熟方案）──────────────
# <think> / <thinking> 标签 + Qwen3 特殊思考 token，可能被流式切片拆到多个 chunk，
# 由 ThinkSplitter 跨 chunk 缓冲后统一匹配。
_THINK_START_RE = re.compile(r'<think(?:ing)?[^>]*>|<\|start_thinking\|>', re.IGNORECASE)
_THINK_END_RE = re.compile(r'</think(?:ing)?>|<\|end_thinking\|>', re.IGNORECASE)
# 缓冲尾部保留窗口：大于最长完整标签（"<|start_thinking|>" = 17 字符），防止半截标签被误推
_THINK_HOLD = 18
# 未闭合思考块内的"思考→正文"转折（Qwen3 常漏写 </think>，正文通常另起一段）：
# 匹配点之前是思考，匹配点起（含整段开头）是正文——匹配点不吞正文本身
_ANSWER_TRANSITION_RE = re.compile(
    r'(?:\n\n|\r\n\r\n)\s*(?:'
    r'好的[，,]?\s*(?:这是|下面|以下是|最终|直接|我)|'
    r'(?:最终|直接)(?:答案|给出|回答)|综上(?:所述)?|'
    r'根据以上分析|以下是我|我(?:现在|来)直接|'
    r'#{1,3}\s*(?:回答|答案|结果|总结|Answer|Result)|'
    r'-{3,}|'
    r'Here\'?s (?:the |my )?|The (?:answer|result|solution)|'
    r'In (?:summary|conclusion)|Based on (?:the |my )?analysis'
    r')',
    re.MULTILINE | re.IGNORECASE)


class ThinkSplitter:
    """把 content 流中的思考块拆出来：思考 -> on_reason，正文 -> on_content。

    - 完整 <think>...</think> / <|start_thinking|>...</|end_thinking|> -> reasoning
    - 标签被流式切片拆到多个 chunk -> 跨 chunk 缓冲后统一匹配，不误判不漏判
    - 只有 <think> 没有 </think>（Qwen3 常漏闭合）-> 在思考块内找"思考→正文"转折
      （另起一段且像答案开头），找到则后面归正文、前面归思考；找不到则全部归思考
    - 思考内容在块解析完成后一次性推送（思考块默认折叠，无需逐字打字机）
    """

    MAX_THINK_BUF = 16 * 1024

    def __init__(self, on_content=None, on_reason=None):
        self.buf = ''
        self.in_think = False
        self.on_content = on_content or (lambda t: None)
        self.on_reason = on_reason or (lambda t: None)

    def push(self, text):
        if text:
            self.buf += text
            self._drain(False)

    def finish(self):
        """流结束：把剩余缓冲按当前状态输出（未闭合思考块 -> reasoning）。"""
        self._drain(True)
        if self.buf:
            if self.in_think:
                self.on_reason(self.buf)
            else:
                self.on_content(self.buf)
            self.buf = ''

    def _drain(self, flush):
        while True:
            if self.in_think:
                m = _THINK_END_RE.search(self.buf)
                if m:
                    head, rest = self.buf[:m.start()], self.buf[m.end():]
                    if head:
                        self.on_reason(head)
                    self.buf = rest
                    self.in_think = False
                    continue
                # 未闭合思考块：找"思考→正文"转折（模型漏写 </think>，正文常另起一段）
                if len(self.buf) >= 20:
                    tm = _ANSWER_TRANSITION_RE.search(self.buf)
                    if tm:
                        head, rest = self.buf[:tm.start()], self.buf[tm.start():]
                        if head:
                            self.on_reason(head)
                        self.buf = rest.lstrip('\r\n \t')
                        self.in_think = False
                        continue
                # 未闭合且未找到转折：整块缓冲（限量防内存无限），等后续分片补齐
                if len(self.buf) > self.MAX_THINK_BUF:
                    self.on_reason(self.buf[:-self.MAX_THINK_BUF])
                    self.buf = self.buf[-self.MAX_THINK_BUF:]
                return
            else:
                m = _THINK_START_RE.search(self.buf)
                if m:
                    before, rest = self.buf[:m.start()], self.buf[m.end():]
                    if before:
                        self.on_content(before)
                    self.buf = rest
                    self.in_think = True
                    continue
                if flush:
                    self.on_content(self.buf)
                    self.buf = ''
                    return
                # 未找到开始标签：保留尾部可能的半截开始标签
                if len(self.buf) > _THINK_HOLD:
                    self.on_content(self.buf[:-_THINK_HOLD])
                    self.buf = self.buf[-_THINK_HOLD:]
                return


# ── 无标签正文内思考识别（保守启发式，复用老项目 agent_fixed 的成熟方案）──
# 某些内网部署的模型（尤其 Qwen3 系）不用 <think> 标签，而是把思考过程直接
# 写在 content 里（如"好的，用户的问题是…/Let me think about…"开头）。
# 识别策略保守：仅当开头 ~200 字符命中"思考前缀" 且 中后部出现"思考→正文"
# 转折标记时才裁剪——两者缺一即视为正常回复，避免误伤。
_UNMARKED_THINK_PREFIXES = [
    # 英文
    r"here'?s (?:my |a |the )?(?:thinking process|reasoning process)",
    r"step[- ]by[- ]step (?:analysis|reasoning|thinking)",
    r"(?:internal |hidden )?thinking(?:\s+process)?\s*[:：]",
    r"(?:chain[- ]of[- ])?reasoning(?:\s+process)?\s*[:：]",
    r"(?:detailed |step[- ]by[- ]step )?analysis\s*[:：]",
    r"let'?s break (?:this|it) down step by step",
    r"(?:let|allow) me (?:think|analyze|reason|consider|look at|review)",
    r"(?:first|okay|ok|alright|so),? let me (?:think|analyze|consider|check)",
    r"to (?:answer|solve|fix|handle) this,? (?:i |we |let )",
    r"the user (?:wants|asks|needs|is asking)",
    r"so,? (?:the|this) user (?:wants|asked)",
    r"now,? (?:i|we|let me) (?:need|should|can|will)",
    # 中文
    r"^(?:我的)?思考过程[：:]",
    r"^思路分析[：:]",
    r"^逐步分析[：:]",
    r"^分析过程[：:]",
    r"^(?:好的|嗯|恩|好|OK|可以)[，,。]?\s*(?:让我|我来|我先|我们)(?:先|来)?(?:想想|思考|分析|看看|检查|回顾|梳理|理解)",
    r"^(?:让我|我来|我先|我们)(?:先|来)?(?:想想|思考|分析|看看|检查|回顾|梳理|理解)",
    r"^(?:首先|先)[，,]?(?:我|我们)?(?:需要|来看|分析|检查|确认)",
    # 用户诉求开头（Qwen3 离线部署常见：直接把"用户希望/想要/提到…"写进正文）
    r"^用户(?:的)?(?:问题是|想|需要|要求|希望|想要|想让我|让我|需要我|要求我|提到|说|问|给(?:了|我)|期望|目标是)",
    r"^(?:好的|嗯|恩|好|OK|可以)[，,。]?\s*用户(?:的)?(?:问题是|想|需要|要求|希望|想要|想让我|让我|提到|说|问)",
    r"^(?:这个|当前|本次|这是一(?:个|份)|这是关于)(?:任务|需求|问题|请求|要求|文件|数据)",
    r"^(?:我看到|我注意到|我发现|我需要|我得先|我来先|先看(?:下|一下)|先来|先确认)",
    r"^(?:用户说|用户提|用户问|用户发来|用户上传了|用户给了)",
    r"^我的(?:思路|想法|计划|分析|考虑)(?:是|：|:)",
    r"^(?:这个任务|该任务|本次任务|当前任务)(?:是|要求|需要)",
    r"^需求(?:是|为|：|:)",
]

_UNMARKED_THINK_TRANSITIONS = [
    # 英文转折
    r"(?:here'?s (?:the |my )?(?:answer|response|reply|result|summary|solution))\s*[:：]",
    r"(?:based on (?:the |my )?analysis)\s*[,，]",
    r"(?:in summary|to summarize|in conclusion|in short|all in all)\s*[,：:]",
    r"(?:the (?:answer|solution|fix|result|conclusion) is)\s*[:：]",
    r"(?:to (?:fix|solve|resolve|answer) (?:this|it|the (?:problem|issue)))\s*[,：:]",
    r"(?:okay|ok|alright|so),? now\s*[,，:]",
    r"^anyway\s*[,，]",
    r"^(?:so|now),? here'?s",
    r"^---+",
    r"^##\s+(?:Answer|Response|Solution|回答|总结|结果)",
    # 中文转折
    r"^根据以上分析\s*[,，]",
    r"^(?:综上|综上所述|总的来说|总的说来|总而言之)\s*[,，：:]",
    r"^(?:最终答案|答案是|我的回答是|我的建议是|我的方案是)\s*[:：]",
    r"^(?:好的|好|OK)[，,。]?\s*(?:我来?|我(?:现在|来)直接|以下|这是|现在|下面)",
    r"^(?:现在|接下来|最后)[，,]?\s*(?:我(?:们)?(?:来|直接|给出|总结|看)|回答|给出|总结|看)",
    r"^直接(?:给出|回答|上|说)",
    r"^所以[,，:：]?",
    r"^因此[,，:：]?",
    r"^那么[,，:：]?",
    r"^(?:总结|小结)(?:一下|如下|来说)?[,，:：]?",
    r"^(?:我认为|我建议|我觉得|我推荐|我倾向于)[,，:：]",
    r"^(?:下面是|以下(?:是|为)|具体(?:做法|方案|来说)|详细(?:方案|步骤|做法)|步骤(?:如下)?|做法(?:如下)?|回答如下|结果如下)",
    r"^可以(?:这样|这么做|按以下|参考以下)",
    r"^先说结论",
    r"^那我们(?:来|现在)?(?:回答|看看|整理|总结)",
    r"^接下来我",
    r"^综上，我",
    r"^所以(?:我|我们)",
]

_UNMARKED_MIN_LEN = 400      # 短内容不可能是完整思考块
_UNMARKED_HEAD = 200        # 前缀只查开头 200 字符
_UNMARKED_SEARCH = 500      # 转折标记从 min(500, len/4) 之后搜索


def split_unmarked_thinking(content: str) -> tuple:
    """检测无标签正文内思考，返回 (clean_content, stripped_thinking)。

    保守策略：开头命中思考前缀 + 中后部命中转折标记才裁剪；转折点之后是正文。
    未命中 -> 原样返回，stripped 为空。绝不误伤正常回复。
    """
    if not content or len(content) < _UNMARKED_MIN_LEN:
        return content, ''
    head = content[:_UNMARKED_HEAD].lower()
    if not any(re.search(pr, head, re.IGNORECASE | re.MULTILINE)
               for pr in _UNMARKED_THINK_PREFIXES):
        return content, ''
    search_start = min(_UNMARKED_SEARCH, len(content) // 4)
    for trans_re in _UNMARKED_THINK_TRANSITIONS:
        m = re.search(trans_re, content[search_start:], re.MULTILINE | re.IGNORECASE)
        if m:
            split_pos = search_start + m.start()
            marker = m.group(0)
            rest = content[split_pos + len(marker):].lstrip(' \t\n\r，,：:。')
            if not rest.strip():
                continue
            thinking = content[:split_pos + len(marker)].strip()
            return rest.strip(), thinking
    # 未命中转折：不裁剪（保守），但超长时截断保底防刷屏
    if len(content) > 8000:
        return content[:4000] + "\n\n...(内容过长，已截断)", ''
    return content, ''


def strip_thinking_blocks(content: str) -> str:
    """从文本中剥离所有残留思考块标签（保底清理，正常拆分后内容里不应有）。"""
    c = content or ""
    c = re.sub(r'<think(?:ing)?[^>]*>.*?</think(?:ing)?>', '', c, flags=re.DOTALL | re.IGNORECASE)
    c = re.sub(r'<think(?:ing)?\s*/>', '', c, flags=re.IGNORECASE)
    c = re.sub(r'<\|start_thinking\|>[\s\S]*?<\|end_thinking\|>', '', c, flags=re.IGNORECASE)
    c = re.sub(r'\|start_thinking\|[\s\S]*?\|end_thinking\|', '', c, flags=re.IGNORECASE)
    return c.strip()


class LLMError(Exception):
    """LLM 调用错误（含可读的中文信息，便于前端展示）。

    retry_after: 服务端 Retry-After 头（秒），重试退避时优先使用。
    """
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def _iter_lines(resp, initial: bytes = b''):
    """按行消费响应体：读到一行就 yield，不等整包读完（真流式的关键）。"""
    buf = initial or b''
    while True:
        nl = buf.find(b'\n')
        if nl >= 0:
            yield buf[:nl]
            buf = buf[nl + 1:]
            continue
        chunk = resp.readline()
        if not chunk:
            if buf.strip():
                yield buf
            return
        buf += chunk


class LLMClient:
    def __init__(self, base_url: str, api_key: str = "", model: str = "qwen",
                 timeout: int = DEFAULT_TIMEOUT, thinking: str = "collapse",
                 max_tokens: int = 0, retries: int = 2):
        self.base_url = (base_url or "").rstrip('/')
        self.api_key = api_key or ""
        self.model = model or "qwen"
        self.timeout = timeout
        self.thinking = (thinking or "collapse").strip().lower()
        if self.thinking not in ('collapse', 'hide', 'keep'):
            self.thinking = 'collapse'
        # 输出上限：0 = 用服务端默认（部分内网部署默认 max_tokens 很小，
        # 会导致长回答/SQL 被截断，可在设置里调大）
        self.max_tokens = max(0, int(max_tokens or 0))
        self.retries = max(0, int(retries or 0))
        # 并发安全：每次 chat 独立状态，_live 里登记在途调用（abort 用）
        self._lock = threading.Lock()
        self._live = {}

    # ── 停止 ──────────────────────────────────────────────────────
    def abort(self):
        """强制终止所有在途请求（含后台记忆提取/子 Agent）。

        关闭底层 http.client 连接（而不仅是响应对象），因此即使在
        等待模型首个响应头（getresponse 阻塞阶段）也能立即中断。
        """
        with self._lock:
            states = list(self._live.values())
        for st in states:
            st['aborted'] = True
            conn = st.get('conn')
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ── 核心调用 ──────────────────────────────────────────────────
    def chat(self, messages, tools=None, on_delta=None, on_reason=None,
             temperature=0.7):
        """发送 chat 请求（可被 abort() 随时中断，连接类错误自动重试）。

        返回: {"content": str, "reasoning": str, "tool_calls": [...], "usage": {...}}
        或抛 LLMError（含面向用户的中文信息）。
        on_delta(text)   收到增量正文时回调（可为 None）。
        on_reason(text)  收到思考内容增量时回调。

        重试策略（B9）：指数退避 + 抖动，优先尊重服务端 Retry-After；
        **一旦已经流式输出过内容就不再重试**（重放会导致正文重复）。
        """
        emitted = {'text': False}

        def _d(t):
            emitted['text'] = True
            if on_delta:
                on_delta(t)

        def _r(t):
            emitted['text'] = True
            if on_reason:
                on_reason(t)

        last = None
        for attempt in range(self.retries + 1):
            try:
                return self._chat_once(messages, tools, _d, _r, temperature)
            except LLMError as e:
                last = e
                msg = str(e)
                # 读超时不重试：服务端第一个请求还在算，重发只会让同一张卡压力翻倍
                if ('中止' in msg or 'HTTP 4' in msg or '超时' in msg or emitted['text']
                        or attempt >= self.retries):
                    raise
                delay = (float(e.retry_after) if e.retry_after
                         else min(8.0, 0.6 * (2 ** attempt))) + random.uniform(0, 0.3)
                delay = max(0.1, min(delay, 30.0))
                log.warning('模型请求失败，%.1fs 后重试（%d/%d）: %s',
                            delay, attempt + 1, self.retries, msg)
                time.sleep(delay)
        raise last

    def _chat_once(self, messages, tools, on_delta, on_reason, temperature):
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        # thinking=hide: 请求服务端关闭思考（Qwen3 系 vLLM 顶层字段被忽略，
        # 必须 chat_template_kwargs 双参数才能真正关闭——老项目实测结论）
        if self.thinking == 'hide':
            body["enable_thinking"] = False
            body["chat_template_kwargs"] = {"enable_thinking": False}

        url = f"{self.base_url}/chat/completions"
        u = _urlparse.urlparse(url)
        host = u.hostname or '127.0.0.1'
        port = u.port or (443 if u.scheme == 'https' else 80)
        conn_cls = _http.HTTPSConnection if u.scheme == 'https' else _http.HTTPConnection
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req_path = u.path or '/chat/completions'
        if u.query:
            req_path += '?' + u.query
        conn = conn_cls(host, port, timeout=self.timeout)
        state = {'conn': conn, 'aborted': False}
        token = id(state)
        with self._lock:
            self._live[token] = state
        want_reason = self.thinking != 'hide'
        box = {}

        def _io():
            try:
                box['r'] = self._chat_io(conn, req_path, headers, body,
                                         on_delta, on_reason, want_reason, state)
            except Exception as e:
                box['e'] = e
            finally:
                with self._lock:
                    self._live.pop(token, None)
                try:
                    conn.close()
                except Exception:
                    pass

        t = threading.Thread(target=_io, daemon=True)
        t.start()
        # 主线程轮询停止标志：abort() 后立即返回，不等模型响应/超时
        while t.is_alive():
            if state['aborted']:
                # 尽力唤醒阻塞中的 recv（POSIX 有效；Windows 部分情况无效，
                # 但主线程不再等待，_io 守护线程会自然超时退出）
                try:
                    sock = conn.sock
                    if sock is not None:
                        sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                t.join(0.3)
                raise LLMError("请求被中止")
            t.join(0.2)
        if 'e' in box:
            raise box['e']
        return box['r']

    def _chat_io(self, conn, req_path, headers, body,
                 on_delta, on_reason, want_reason, state):
        """实际网络请求 + 解析（在守护线程中运行，可被 abort 中断）。"""
        content_parts = []
        reasoning_parts = []
        tool_acc = {}  # index -> {id, name, arguments}
        usage = {}

        def _push_content(t):
            content_parts.append(t)
            if on_delta:
                on_delta(t)

        def _push_reason(t):
            reasoning_parts.append(t)
            if want_reason and on_reason:
                on_reason(t)

        def _parse_message(msg):
            """解析整包 JSON message（非 SSE / 混 JSON 路径共用）。"""
            nonlocal usage
            content = msg.get('content') or ''
            splitter = ThinkSplitter(on_content=_push_content, on_reason=_push_reason)
            splitter.push(content)
            splitter.finish()
            reasoning = msg.get('reasoning_content') or ''
            if reasoning:
                reasoning_parts.append(reasoning)
                if want_reason and on_reason:
                    on_reason(reasoning)
            for tc in (msg.get('tool_calls') or []):
                fn = tc.get('function', {})
                tool_acc[tc.get('index', 0)] = {
                    'id': tc.get('id', ''),
                    'name': fn.get('name', ''),
                    'arguments': fn.get('arguments', '') or '',
                }
            if msg.get('usage'):
                usage = msg.get('usage') or {}

        def _consume_sse(resp, initial=b''):
            """逐行消费 SSE（initial=预读的字节，可能含半行）。"""
            nonlocal usage
            splitter = ThinkSplitter(on_content=_push_content, on_reason=_push_reason)
            for raw in _iter_lines(resp, initial):
                if state['aborted']:
                    break
                if not raw:
                    continue
                line = raw.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                if not line.startswith('data:'):
                    # 容忍空行/注释；遇到 JSON 整包则降级解析
                    if line.startswith('{'):
                        try:
                            data = json.loads(line)
                            _parse_message(data.get('choices', [{}])[0].get('message', {}))
                            usage = data.get('usage') or usage
                            break
                        except Exception:
                            continue
                    continue
                payload = line[5:].strip()
                if payload == '[DONE]':
                    break
                try:
                    data = json.loads(payload)
                except Exception:
                    continue
                choices = data.get('choices') or []
                if not choices:
                    if data.get('usage'):
                        usage = data.get('usage') or {}
                    continue
                delta = choices[0].get('delta') or {}
                # 思考增量（Qwen3 等模型的 reasoning_content，与 content 独立）
                r = delta.get('reasoning_content')
                if r:
                    reasoning_parts.append(r)
                    if want_reason and on_reason:
                        on_reason(r)
                # 内容增量（可能含 <think> 标签 -> 由 splitter 拆分）
                d = delta.get('content')
                if d:
                    splitter.push(d)
                # 工具调用增量（分片聚合）
                for c in (delta.get('tool_calls') or []):
                    idx = c.get('index', 0)
                    acc = tool_acc.setdefault(idx, {'id': '', 'name': '', 'arguments': ''})
                    if c.get('id'):
                        acc['id'] = c['id']
                    fn = c.get('function') or {}
                    if fn.get('name'):
                        acc['name'] = fn['name']
                    if fn.get('arguments'):
                        acc['arguments'] += fn['arguments']
            splitter.finish()  # 未闭合思考块 -> 归 reasoning

        try:
            conn.request('POST', req_path,
                         body=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                         headers=headers)
            resp = conn.getresponse()
            if resp.status >= 400:
                # http.client 不会抛 HTTPError，4xx/5xx 直接在这里读取
                try:
                    detail = resp.read(2000).decode('utf-8', errors='replace')
                except Exception:
                    detail = ''
                ra = None
                try:
                    ra = float(resp.headers.get('Retry-After') or 0) or None
                except (TypeError, ValueError):
                    ra = None
                raise LLMError(f"模型接口 HTTP {resp.status}: {detail[:300] or resp.reason}",
                               retry_after=ra)

            ctype = (resp.headers.get("Content-Type") or "").lower()
            if 'event-stream' in ctype:
                _consume_sse(resp)
            else:
                # 服务器可能忽略 stream 直接返回 JSON：先预读一小段判断
                first = resp.read(2048)
                if b'data:' in first:
                    _consume_sse(resp, initial=first)
                else:
                    raw = first + resp.read()
                    try:
                        data = json.loads(raw.decode('utf-8', errors='replace'))
                    except Exception as e:
                        raise LLMError(f"模型返回了无法解析的内容: {e}")
                    msg = data.get('choices', [{}])[0].get('message', {})
                    _parse_message(msg)
                    usage = data.get('usage') or usage
            return self._assemble(content_parts, reasoning_parts, tool_acc, usage,
                                  keep_reason=want_reason)

        except socket.timeout:
            raise LLMError("模型响应超时（模型可能已挂起）")
        except _http.RemoteDisconnected:
            if state['aborted']:
                raise LLMError("请求被中止")
            raise LLMError("模型连接被关闭（模型可能重启或断连）")
        except LLMError:
            raise
        except Exception as e:
            if state['aborted']:
                raise LLMError("请求被中止")
            if isinstance(e, (ConnectionError, BrokenPipeError, socket.error, OSError)):
                raise LLMError(f"无法连接模型服务: {e}")
            raise LLMError(f"请求模型失败: {type(e).__name__}: {e}")

    @staticmethod
    def _assemble(content_parts, reasoning_parts, tool_acc, usage, keep_reason=True):
        content = ''.join(content_parts).strip()
        reasoning = ''.join(reasoning_parts).strip()
        # 保底：清掉可能残留的思考标签
        content = strip_thinking_blocks(content)
        # 无标签正文内思考：保守启发式，剥离部分归入 reasoning（不丢）
        clean, stripped = split_unmarked_thinking(content)
        if stripped:
            content = clean
            if keep_reason:
                reasoning = (stripped + "\n\n" + reasoning).strip() if reasoning else stripped
        if not keep_reason:
            reasoning = ''
        tool_calls = []
        for idx in sorted(tool_acc):
            a = tool_acc[idx]
            if a.get('name'):
                tool_calls.append({
                    "id": a.get('id') or f"call_{idx}",
                    "name": a['name'],
                    "arguments": a.get('arguments') or '',
                })
        return {
            "content": content,
            "reasoning": reasoning,
            "tool_calls": tool_calls,
            "usage": usage or {},
        }
