# -*- coding: utf-8 -*-
"""离线 mock 模型服务（OpenAI 兼容），用于端到端测试。

不连真模型也能验证：SSE 流式、思考块拆分、工具调用聚合、写文件产物、
断流/停止、错误路径。用法：

    python tests/mock_llm.py --port 8898
    # config.json 的 model.base_url 填 http://127.0.0.1:8898/v1

脚本化行为（按用户消息里的关键字）：
- 含“写文件” -> 第一轮返回 write_file 工具调用，第二轮返回带路径的结论
- 含“失败演示” -> 返回一个不存在的工具（验证连续失败熔断路径）
- 其它 -> 流式输出「思考块 + markdown + 代码块」，用于看流式渲染
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = """好的，这是给你的结论。

## 要点
1. 第一条：**先读表再汇总**，避免口径不一致。
2. 第二条：金额统一保留两位小数。
3. 第三条：产出物保存到 `outputs/` 下，路径写进回复。

```python
def total(rows):
    return round(sum(r["amount"] for r in rows), 2)
```

| 项目 | 金额 |
|---|---|
| A | 12.30 |
| B | 4.50 |

需要我把它整理成 Word 吗？
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        sys.stderr.write('[mock] ' + (fmt % args) + '\n')

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip('/').endswith('/models'):
            return self._json({"object": "list", "data": [{"id": "mock-qwen",
                                                            "object": "model"}]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b'{}'
        try:
            req = json.loads(raw.decode('utf-8'))
        except Exception:
            return self._json({"error": "bad json"}, 400)
        if not self.path.rstrip('/').endswith('/chat/completions'):
            return self._json({"error": "not found"}, 404)

        msgs = req.get('messages') or []
        user_text = ''
        for m in reversed(msgs):
            if m.get('role') == 'user':
                c = m.get('content')
                if isinstance(c, list):
                    c = ' '.join(str(p.get('text') or '') for p in c
                                 if isinstance(p, dict))
                user_text = str(c or '')
                break
        has_tool_result = any(m.get('role') == 'tool' for m in msgs)

        if '写文件' in user_text and not has_tool_result:
            self._sse_tool_call('write_file',
                                {"path": "outputs/演示/结论.md",
                                 "content": "# 结论\n\n- 第一条\n- 第二条\n"})
            return
        if '失败演示' in user_text and not has_tool_result:
            self._sse_tool_call('no_such_tool', {"x": 1})
            return
        if has_tool_result:
            self._sse_text('已经把结论写入 `outputs/演示/结论.md`。', think='')
            return
        if '长文' in user_text:
            # 慢速长输出：用于观察流式渲染/中途停止
            self._sse_text(ANSWER * 4, think='', delay=0.05, chunk=16)
            return
        self._sse_text(ANSWER,
                       think='用户想要一个结论，先把口径说清楚，再给表格和示例代码。')

    # ── SSE 输出 ─────────────────────────────────────────────────
    def _head(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()

    def _frame(self, delta, finish=None):
        obj = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
               "created": int(time.time()), "model": "mock-qwen",
               "choices": [{"index": 0, "delta": delta,
                            "finish_reason": finish}]}
        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")
                         .encode('utf-8'))
        self.wfile.flush()

    def _done(self, prompt_tokens=800, completion_tokens=200):
        usage = {"prompt_tokens": prompt_tokens,
                 "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
        obj = {"id": "chatcmpl-mock", "choices": [],
               "usage": usage, "model": "mock-qwen"}
        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")
                         .encode('utf-8'))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _sse_text(self, text, think='', delay=0.012, chunk=24):
        self._head()
        if think:
            # 故意拆成多片、且标签跨 chunk，验证 ThinkSplitter 的跨片处理
            for chunk in ('<thi', 'nk>', think[:18], think[18:], '</thi', 'nk>\n\n'):
                self._frame({"content": chunk})
                time.sleep(0.01)
        # 按 24 字切开发流式帧（模拟真实模型的分片）
        for i in range(0, len(text), chunk):
            self._frame({"content": text[i:i + chunk]})
            time.sleep(delay)
        self._frame({}, "stop")
        self._done()

    def _sse_tool_call(self, name, args):
        self._head()
        self._frame({"role": "assistant", "content": ""})
        self._frame({"tool_calls": [{"index": 0, "id": "call_mock_1",
                                     "type": "function",
                                     "function": {"name": name, "arguments": ""}}]})
        payload = json.dumps(args, ensure_ascii=False)
        for i in range(0, len(payload), 16):
            self._frame({"tool_calls": [{"index": 0,
                                         "function": {"arguments": payload[i:i + 16]}}]})
            time.sleep(0.005)
        self._frame({}, "tool_calls")
        self._done(prompt_tokens=900, completion_tokens=40)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8898)
    ap.add_argument('--host', default='127.0.0.1')
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f'mock LLM on http://{a.host}:{a.port}/v1', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
