# -*- coding: utf-8 -*-
"""批量作业（B8）：把"一个文件夹"变成一等公民。

办公场景的真实需求不是"聊一句做一份"，而是"把这个目录下 50 份合同的关键
条款抽到一张 Excel"。批量作业把这种重复任务变成有台账的一等公民：

    .agent/jobs/<job_id>/
    ├── job.json      # 模板提示词、输入文件列表、状态（真源）
    └── items.jsonl   # 每项: file / status / output / error / elapsed（只追加台账）

执行模型：
- 单 worker 串行（本地模型吞吐有限），每项一个独立 Agent + 独立上下文，互不污染。
- 与前台聊天**并发**运行：workspace 的会话上下文是线程局部的，LLMClient 线程安全，
  因此不抢 _busy 守卫（否则长批次会让交互请求排队超时）。
- 批量作业不弹确认框：写入按 auto_edit 档位自动放行，命令一律拒绝（fail-closed）。
- 支持暂停 / 继续 / 取消 / 重试失败项；重启后未跑完的作业置为暂停，不自动烧模型。
"""
import csv
import io
import json
import re
import threading
import time
import uuid
from pathlib import Path

from agent.logsetup import get
from agent.workspace import SessionCtx

log = get('batch')

_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_KEEP_TEXT = 20000        # 单项结果正文保留上限（job.json 体积控制）
_PREVIEW_N = 3            # 「先跑前 3 项预览」的项数


def valid_job_id(job_id: str) -> bool:
    return bool(_ID_RE.match(str(job_id or '')))


class BatchRunner:
    """批量作业执行器：内存索引 + 磁盘台账（磁盘为真）。"""

    def __init__(self, app):
        self.app = app
        self.root = Path(app.ws.root) / '.agent' / 'jobs'
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error('批量作业目录创建失败: %s', e)
        self.jobs = {}              # id -> job dict（items 也在其中）
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = False
        self._cur = None            # (job_id, item_idx) 正在跑的项
        self._cur_agent = None      # 正在跑的 Agent（取消时 request_stop）
        self._load()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='batch-worker')
        self._thread.start()

    # ── 磁盘读写 ──────────────────────────────────────────────
    def running(self) -> bool:
        """是否有批量项正在执行（用于避免后台记忆提取与批量抢同一个推理槽）。"""
        return self._cur is not None

    def _dir(self, job_id: str) -> Path:
        if not valid_job_id(job_id):
            raise PermissionError(f'非法作业 id: {job_id}')
        return self.root / job_id

    def _load(self):
        if not self.root.is_dir():
            return
        for d in self.root.iterdir():
            if not d.is_dir() or not valid_job_id(d.name):
                continue
            try:
                job = json.loads((d / 'job.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict) or not job.get('id'):
                continue
            # 重启后不自动烧模型：未跑完的置为暂停，等用户点「继续」。
            # 项级 running 同样是 stale 的（当时那项的 worker 随进程死了）：
            # 不复位成 pending 的话，_next 永远跳过它、收尾条件又永远不满，
            # 作业卡在"paused 但永远跑不完"。
            if job.get('status') in ('running', 'pending'):
                job['status'] = 'paused'
                job['note'] = '服务重启，已暂停；点「继续」接着跑'
                for it in job.get('items') or []:
                    if it.get('status') == 'running':
                        it['status'] = 'pending'
                        it['error'] = ''
            self.jobs[job['id']] = job
        if self.jobs:
            log.info('批量作业恢复: %d 个台账', len(self.jobs))

    def _persist(self, job: dict):
        # 持锁写盘：_run_item 曾在锁外调 _persist，与 control() 的写盘
        # 共用同一 job.json.tmp，内容会交错、replace 后剩半截 JSON。
        # RLock：已持锁的调用方重入无碍。
        with self._lock:
            d = self._dir(job['id'])
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / 'job.json.tmp'
            tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1),
                           encoding='utf-8')
            tmp.replace(d / 'job.json')

    def _ledger(self, job: dict, item: dict):
        """items.jsonl：只追加的执行台账（事后排查/统计用）。"""
        try:
            with open(self._dir(job['id']) / 'items.jsonl', 'a',
                      encoding='utf-8') as f:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')
        except OSError:
            pass

    # ── 对外 API ─────────────────────────────────────────────
    def list(self) -> list:
        with self._lock:
            out = [self._summary(j) for j in self.jobs.values()]
        out.sort(key=lambda j: j.get('created_at') or '', reverse=True)
        return out

    @staticmethod
    def _summary(job: dict) -> dict:
        items = job.get('items') or []
        cnt = {s: 0 for s in ('pending', 'running', 'done', 'failed', 'canceled')}
        for it in items:
            cnt[it.get('status') or 'pending'] = cnt.get(
                it.get('status') or 'pending', 0) + 1
        j = {k: v for k, v in job.items() if k != 'items'}
        j['counts'] = cnt
        j['total'] = len(items)
        return j

    def get(self, job_id: str) -> dict:
        if not valid_job_id(job_id):
            raise PermissionError(f'非法作业 id: {job_id}')
        with self._lock:
            job = self.jobs.get(job_id)
        if not job:
            raise FileNotFoundError(f'作业不存在: {job_id}')
        return job

    def create(self, name: str, template: str, files: list,
               preview: bool = False) -> dict:
        name = str(name or '').strip() or f'批量作业 {time.strftime("%m-%d %H:%M")}'
        template = str(template or '').strip()
        if not template:
            raise ValueError('缺少模板提示词（每项要做什么）')
        files = [str(f).replace('\\', '/').strip() for f in (files or [])]
        files = [f for f in files if f]
        if not files:
            raise ValueError('没有选择输入文件')
        if len(files) > 200:
            raise ValueError('单个作业最多 200 个文件')
        # 逐个校验：必须是工作台内可读的真实文件
        for f in files:
            if f.startswith('/') or '..' in f.split('/') or re.match(r'^[A-Za-z]:', f):
                raise PermissionError(f'非法文件路径: {f}')
            try:
                p = self.app.ws.resolve_read(f)
            except PermissionError:
                raise PermissionError(f'无法读取该文件: {f}')
            if not p.is_file():
                raise FileNotFoundError(f'文件不存在: {f}')
        jid = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        job = {
            'id': jid,
            'name': name[:80],
            'template': template,
            'files': files,
            'preview': bool(preview),
            'status': 'running',
            'note': '',
            'created_at': now,
            'done_at': '',
            'items': [{
                'idx': i, 'file': f, 'status': 'pending',
                'elapsed': 0.0, 'error': '', 'response': '', 'artifacts': [],
            } for i, f in enumerate(files)],
        }
        with self._lock:
            self.jobs[jid] = job
            self._persist(job)
        self._wake.set()
        log.info('批量作业创建: %s（%d 项）', name, len(files))
        return job

    def control(self, job_id: str, action: str) -> dict:
        job = self.get(job_id)
        action = str(action or '').strip()
        with self._lock:
            if action == 'pause':
                if job['status'] == 'running':
                    job['status'] = 'paused'
                    job['note'] = ''
            elif action == 'resume':
                if job['status'] in ('paused', 'running'):
                    if any(i['status'] in ('pending', 'running')
                           for i in job['items']):
                        job['status'] = 'running'
                        job['note'] = ''
                        self._wake.set()
            elif action == 'cancel':
                job['status'] = 'canceled'
                job['note'] = '已取消'
                for it in job['items']:
                    if it['status'] in ('pending', 'running'):
                        it['status'] = 'canceled'
                ag = self._cur_agent if self._cur and self._cur[0] == job_id else None
                if ag is not None:
                    try:
                        ag.request_stop()
                    except Exception:
                        pass
            elif action == 'retry':
                n = 0
                for it in job['items']:
                    if it['status'] in ('failed', 'canceled'):
                        it['status'] = 'pending'
                        it['error'] = ''
                        n += 1
                if n:
                    job['status'] = 'running'
                    job['note'] = ''
                    self._wake.set()
            else:
                raise ValueError(f'未知动作: {action}')
            self._persist(job)
        return job

    def to_csv(self, job_id: str) -> str:
        job = self.get(job_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['序号', '输入文件', '状态', '耗时(秒)', '错误', '产物', '结果'])
        for it in job.get('items') or []:
            w.writerow([it['idx'] + 1, it['file'], it.get('status', ''),
                        it.get('elapsed', 0), it.get('error', ''),
                        ' / '.join(it.get('artifacts') or []),
                        (it.get('response') or '').replace('\r', ' ')])
        return buf.getvalue()

    # ── worker ───────────────────────────────────────────────
    def _next(self):
        """挑下一个要跑的 (job, item) 并原子认领；没有就返回 None。

        认领（pending→running + 登记 _cur + 落盘）在同一把锁内完成：
        旧版只在锁内"挑"，_run_item 在锁外改状态，中间 control('cancel')
        能把该项标 canceled 却拦不住它随后开跑（且 _cur_agent 还没挂上，
        request_stop 也递不到）。
        """
        with self._lock:
            for job in self.jobs.values():
                if job.get('status') != 'running':
                    continue
                for it in job['items']:
                    if it['status'] == 'pending':
                        it['status'] = 'running'
                        it['error'] = ''
                        self._cur = (job['id'], it['idx'])
                        self._persist(job)
                        return job, it
                # 没有待办项：收尾
                if not any(i['status'] == 'running' for i in job['items']):
                    failed_n = sum(1 for i in job['items']
                                   if i['status'] == 'failed')
                    job['status'] = 'done'
                    job['done_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    # 全失败也落 done（已无待办、是终态），但备注写清失败数，
                    # 否则界面上"已完成"看起来像全成功。
                    job['note'] = (f'{failed_n} 项失败，见条目错误信息'
                                   if failed_n else (job.get('note') or ''))
                    self._persist(job)
        return None

    def _loop(self):
        while not self._stop:
            picked = None
            try:
                picked = self._next()
            except Exception:
                log.exception('批量作业调度异常')
            if not picked:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            job, item = picked
            try:
                self._run_item(job, item)
            except Exception as e:
                log.exception('批量作业执行异常: %s', item.get('file'))
                with self._lock:
                    item['status'] = 'failed'
                    item['error'] = f'{type(e).__name__}: {e}'
            finally:
                # 收尾落盘失败也不能杀死 worker 线程：否则整个批量停摆且无声。
                # _persist/_ledger 自身的异常在此消化、记日志，循环继续。
                try:
                    with self._lock:
                        self._cur = None
                        self._cur_agent = None
                        # 预览模式：前 3 项跑完先停，等用户看结果再继续
                        if (job.get('preview') and job.get('status') == 'running'
                                and not job.get('previewed')):
                            done_n = sum(1 for i in job['items']
                                         if i['status'] == 'done')
                            rest = sum(1 for i in job['items']
                                       if i['status'] == 'pending')
                            if done_n >= _PREVIEW_N and rest:
                                job['previewed'] = True
                                job['status'] = 'paused'
                                job['note'] = (f'预览完成：前 {done_n} 项已跑完，'
                                               f'点「继续」执行其余 {rest} 项')
                        self._persist(job)
                        self._ledger(job, dict(item))
                except Exception:
                    log.exception('批量作业收尾持久化失败: %s', item.get('file'))
            self._wake.set()

    def _run_item(self, job: dict, item: dict):
        """跑单项：独立 Agent + 独立上下文（互不污染）。

        认领已在 _next 内完成（status 已是 running、_cur 已登记），
        这里不再碰状态；结果写回时持锁，并以 job 的取消状态为准。
        """
        from datetime import date
        ctx = SessionCtx(f"batch-{job['id']}", job['name'],
                         date.today().strftime('%Y-%m-%d'))
        agent = self.app.make_batch_agent(ctx)
        with self._lock:
            self._cur_agent = agent
        agent.reset_stop()
        agent.on_delta = None
        agent.on_tool = None
        agent.on_tool_start = None
        agent.on_todo = None
        agent.on_think = None
        agent.on_ask = None        # 批量不向用户提问（无人应答会卡 180s）

        t0 = time.monotonic()
        total = len(job['items'])
        prompt = (
            f"批量作业「{job['name']}」第 {item['idx'] + 1}/{total} 项。\n\n"
            f"{job['template']}\n\n"
            f"本次只处理这一个文件：{item['file']}\n"
            f"要求：\n"
            f"1. 先读取该文件（用工作台内相对路径）；\n"
            f"2. 按模板要求产出结果，把结果写入 outputs/（文件名包含原文件名）；\n"
            f"3. 最后用一两句话给出结论。"
        )
        try:
            r = agent.run([], prompt, mode='craft')
        finally:
            with self._lock:
                self._cur_agent = None
        item['elapsed'] = round(time.monotonic() - t0, 1)

        # 结果写回持锁：取消可能恰好落在 agent.run 返回之后，control 已把
        # job/item 标 canceled；此时再写 done 会把"已取消"盖回去。
        with self._lock:
            if job.get('status') == 'canceled':
                item['status'] = 'canceled'
                item['error'] = item.get('error') or '已停止'
            elif r.get('stopped'):
                item['status'] = 'canceled'
                item['error'] = '已停止'
            else:
                item['status'] = 'done'
                item['response'] = str(r.get('response') or '')[:_KEEP_TEXT]
                item['artifacts'] = [a.get('rel', '') if isinstance(a, dict) else str(a)
                                     for a in (r.get('artifacts') or [])]
        log.info('批量作业 %s 项 %d/%d: %s -> %s (%.1fs)',
                 job['name'], item['idx'] + 1, total, item['file'],
                 item['status'], item['elapsed'] or 0)
