# -*- coding: utf-8 -*-
"""模型槽位与运行登记。

两个问题一起解决：

1. **模型槽位**：离线单卡只有一张卡/一个推理槽，但旧代码里交互对话、批量作业、
   后台记忆提取各自为政——批量刻意绕过 _busy，记忆提取每轮结束就抢槽。
   `ModelGate` 用一个带优先级的信号量把它们排进同一条队：交互 > 批量 > 记忆。
2. **运行登记**：`RunRegistry` 记录每个会话当前/最近的运行，供
   `/api/run` 与侧栏状态使用（刷新页面也能看到"这个会话还在跑"）。
"""
import threading
import time
import uuid


class ModelGate:
    """带优先级的模型槽位（默认 1 槽，对应单卡部署）。"""

    PRIORITY = {'chat': 0, 'batch': 1, 'memory': 2}

    def __init__(self, slots: int = 1):
        self.slots = max(1, int(slots or 1))
        self._cond = threading.Condition()
        self._active = 0
        self._waiters = []      # [(priority, seq, token)]
        self._seq = 0
        self._acquired_total = 0

    def acquire(self, kind: str = 'chat', timeout=None, stop=None) -> bool:
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        token = object()
        with self._cond:
            self._seq += 1
            me = (self.PRIORITY.get(kind, 1), self._seq, token)
            self._waiters.append(me)
            try:
                while True:
                    self._waiters.sort(key=lambda x: (x[0], x[1]))
                    head = self._waiters[0]
                    if self._active < self.slots and head[2] is token:
                        self._waiters.pop(0)
                        self._active += 1
                        self._acquired_total += 1
                        return True
                    if stop is not None:
                        try:
                            if stop():
                                return False
                        except Exception:
                            pass
                    remain = None if deadline is None else deadline - time.monotonic()
                    if remain is not None and remain <= 0:
                        return False
                    self._cond.wait(0.2 if remain is None else min(0.2, max(0.01, remain)))
            finally:
                for i, w in enumerate(self._waiters):
                    if w[2] is token:
                        self._waiters.pop(i)
                        break

    def release(self):
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def stats(self) -> dict:
        with self._cond:
            return {'slots': self.slots, 'active': self._active,
                    'waiting': len(self._waiters),
                    'acquired_total': self._acquired_total}


class Run:
    """一次会话运行（一轮对话）。events 供断线后回放/状态展示。"""

    MAX_EVENTS = 600

    def __init__(self, sid: str, owner: str = '', kind: str = 'chat'):
        self.id = uuid.uuid4().hex[:10]
        self.sid = sid
        self.owner = owner
        self.kind = kind
        self.status = 'running'      # running / done / error / stopped
        self.started_at = time.time()
        self.finished_at = 0.0
        self.events = []
        self.text_len = 0
        self._lock = threading.Lock()

    def add(self, kind: str, **data):
        ev = {"type": kind, **data}
        with self._lock:
            if len(self.events) >= self.MAX_EVENTS:
                # 只保留尾部：回放关心的是"现在进行到哪"，不是完整历史
                del self.events[:self.MAX_EVENTS // 3]
            self.events.append(ev)
            if kind == 'delta':
                self.text_len += len(str(data.get('text') or ''))
        return ev

    def snapshot(self, after: int = 0) -> dict:
        with self._lock:
            evs = self.events[after:]
            n = len(self.events)
        return {"id": self.id, "sid": self.sid, "status": self.status,
                "kind": self.kind, "started_at": self.started_at,
                "finished_at": self.finished_at, "events": evs,
                "cursor": n, "text_len": self.text_len}

    def finish(self, status: str = 'done'):
        self.status = status
        self.finished_at = time.time()


class RunRegistry:
    """按会话保留最近一次运行（含正在运行的那个）。"""

    def __init__(self, keep: int = 20):
        self._lock = threading.Lock()
        self._runs = {}         # sid -> Run（最近一次）
        self._running = {}      # run_id -> Run
        self._order = []
        self.keep = max(1, int(keep))

    def create(self, sid: str, owner: str = '', kind: str = 'chat') -> Run:
        run = Run(sid, owner, kind)
        with self._lock:
            self._runs[sid] = run
            self._running[run.id] = run
            self._order.append(run.id)
            # 只保留最近若干条运行记录
            while len(self._order) > self.keep:
                old = self._order.pop(0)
                self._running.pop(old, None)
        return run

    def finish(self, run: Run, status: str = 'done'):
        run.finish(status)
        with self._lock:
            self._running.pop(run.id, None)

    def for_session(self, sid: str):
        with self._lock:
            return self._runs.get(sid)

    def running_for(self, sid: str):
        run = self.for_session(sid)
        return run if run and run.status == 'running' else None

    def running(self) -> list:
        with self._lock:
            return [r for r in self._running.values() if r.status == 'running']

    def summary(self) -> list:
        with self._lock:
            runs = list(self._running.values())
        return [{"id": r.id, "sid": r.sid, "status": r.status,
                 "kind": r.kind, "started_at": r.started_at,
                 "text_len": r.text_len} for r in runs]
