# -*- coding: utf-8 -*-
"""SSE 事件契约：后端唯一事实源。

前端 `web/app.js` 的 `handleFrame()` 必须覆盖这里列出的所有事件。
`verify/test_ui_contract.py` 会解析前端 switch，缺一个就失败——避免再次出现
"后端已经发了、前端没人接"的静默断线（ask / tool_start / todo 就是这样丢了）。
"""

TURN_EVENTS = {
    "queued",       # 排队中：position
    "think",        # 思考增量：text
    "delta",        # 正文增量：text
    "tool_start",   # 工具开始执行：name, args
    "tool",         # 工具执行完成：name, args, ok, result, artifacts
    "todo",         # 任务清单变化：items [{text,status}]
    "ask",          # 向用户提问：question, options
    "confirm",      # 危险命令确认：command
    "warn",         # 本轮偏慢提醒：message, extendable（可「再等 5 分钟」放宽看门狗）
    "result",       # 本轮完成：response/stopped/artifacts/usage/context/todos/snapshot
    "error",        # 本轮失败：message
}


def validate(kind: str) -> str:
    if kind not in TURN_EVENTS:
        raise ValueError(f"未登记的 SSE 事件类型: {kind}")
    return kind
