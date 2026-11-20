# -*- coding: utf-8 -*-
"""模型槽位与运行登记。

两个问题一起解决：

1. **模型槽位**：离线单卡只有一张卡/一个推理槽，但旧代码里交互对话、批量作业、
   后台记忆提取各自为政——批量刻意绕过 _busy，记忆提取每轮结束就抢槽。
   `ModelGate` 用一个带优先级的信号量把它们排进同一条队：交互 > 批量 > 记忆。
2. **运行登记**：`RunRegistry` 记录每个会话当前/最近的运行，供
   `/api/run` 与侧栏状态使用（刷新页面也能看到"这个会话还在跑"）。
"""
import json
import threading
import time
import uuid
from pathlib import Path

_ACTIVE = {'queued', 'running', 'stopping', 'awaiting_confirm'}


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
        self.client_req_id = ''
        self.action = ''
        self.updated_at = self.started_at
        self._lock = threading.Lock()

    def touch(self, status=None, action=None):
        if status:
            self.status = status
        if action is not None:
            self.action = action
        self.updated_at = time.time()

    def to_disk(self) -> dict:
        with self._lock:
            events = list(self.events[-200:])
            text_len = self.text_len
        return {
            'id': self.id, 'sid': self.sid, 'owner': self.owner, 'kind': self.kind,
            'status': self.status, 'started_at': self.started_at,
            'finished_at': self.finished_at, 'client_req_id': self.client_req_id,
            'action': self.action, 'updated_at': self.updated_at,
            'text_len': text_len, 'events': events,
        }

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
                "cursor": n, "text_len": self.text_len,
                "client_req_id": self.client_req_id, "action": self.action,
                "updated_at": self.updated_at}

    def finish(self, status: str = 'done'):
        self.status = status
        self.finished_at = time.time()


class RunRegistry:
    """按会话保留最近一次运行（含正在运行的那个）。"""

    def __init__(self, keep: int = 20, root: str = None):
        self._lock = threading.Lock()
        self._runs = {}         # sid -> Run（最近一次）
        self._running = {}      # run_id -> Run
        self._by_req = {}       # client_req_id -> Run
        self._order = []
        self.keep = max(1, int(keep))
        self.root = Path(root) if root else None
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)
            self.load()

    def load(self):
        """读盘。进程已经不在了，仍标着 running 的记录改成 interrupted。

        同一会话有多份历史文件时必须留最新一条：glob 顺序任意，
        直接覆盖会让旧的 done 盖掉本次 interrupted。
        """
        if not self.root or not self.root.is_dir():
            return
        loaded = []
        for path in self.root.glob('*.json'):
            if path.name.endswith('.tmp'):
                continue
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            run = Run(str(data.get('sid') or ''), str(data.get('owner') or ''),
                      str(data.get('kind') or 'chat'))
            run.id = str(data.get('id') or run.id)
            run.status = str(data.get('status') or 'running')
            run.started_at = float(data.get('started_at') or run.started_at)
            run.finished_at = float(data.get('finished_at') or 0)
            run.client_req_id = str(data.get('client_req_id') or '')
            run.action = str(data.get('action') or '')
            run.updated_at = float(data.get('updated_at') or run.started_at)
            run.text_len = int(data.get('text_len') or 0)
            run.events = list(data.get('events') or [])
            loaded.append((run, path))
        loaded.sort(key=lambda rp: (rp[0].started_at, rp[0].updated_at))
        # 只保留最近 keep 条，更旧的文件清掉，避免重启后越积越多
        if len(loaded) > self.keep:
            for _run, path in loaded[:-self.keep]:
                try:
                    path.unlink()
                except OSError:
                    pass
            loaded = loaded[-self.keep:]
        changed = []
        for run, _path in loaded:
            if run.status in _ACTIVE:
                run.status = 'interrupted'
                run.action = '进程已退出，任务没有可靠的结束结果'
                run.finished_at = time.time()
                changed.append(run)
            self._remember(run)
            if run.id not in self._order:
                self._order.append(run.id)
        for run in changed:
            self.save(run)

    def _remember(self, run: Run):
        old = self._runs.get(run.sid)
        if old is None or (run.started_at, run.updated_at) >= (
                old.started_at, old.updated_at):
            self._runs[run.sid] = run
        if run.client_req_id:
            self._by_req[run.client_req_id] = run
        if run.status == 'running':
            self._running[run.id] = run

    def save(self, run: Run):
        if not self.root:
            return
        path = self.root / f'{run.id}.json'
        tmp = self.root / f'{run.id}.json.tmp'
        tmp.write_text(json.dumps(run.to_disk(), ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)

    def get_or_create(self, sid: str, owner: str = '', kind: str = 'chat',
                      client_req_id: str = ''):
        """同一 client_req_id 只登记一次。返回 (run, created)。"""
        with self._lock:
            if client_req_id and client_req_id in self._by_req:
                return self._by_req[client_req_id], False
            run = Run(sid, owner, kind)
            run.client_req_id = client_req_id or run.id
            self._runs[sid] = run
            self._running[run.id] = run
            self._by_req[run.client_req_id] = run
            self._order.append(run.id)
            while len(self._order) > self.keep:
                old = self._order.pop(0)
                self._running.pop(old, None)
            self.save(run)
            return run, True

    def by_id(self, run_id: str):
        with self._lock:
            for run in self._runs.values():
                if run.id == run_id:
                    return run
            return self._running.get(run_id)

    def for_request(self, client_req_id: str):
        with self._lock:
            return self._by_req.get(client_req_id)

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
        self.save(run)

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
