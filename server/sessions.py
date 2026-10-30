# -*- coding: utf-8 -*-
"""会话管理器。

每个会话一个目录 .agent/sessions/<id>/：
- messages.json   消息历史（含每轮的 tool_steps / artifacts，供回放）
- artifacts.json  该会话的全部产出物清单（跨轮累加，产物面板直接读它）
- work_log.md     该会话的工作日志（用户请求/工具步骤/回复摘要）

产出物跟踪 = 会话登记制：工具写文件 -> 登记进 artifacts.json，前端面板读清单。
"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from agent.workspace import SessionCtx

_LOCK = threading.Lock()


def _slug_title(title: str) -> str:
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(title or '')).strip()
    return t or '新会话'


# 会话 id 必须是 create() 生成的 12 位 hex：一切经外部传入的 sid 都要先过
# 白名单，否则 '../..' 会拼出工作台根目录，delete() 的 rmtree 就能删掉
# 任意目录（路径穿越）。所有取会话目录的入口都经 _sdir 统一校验。
_SID_RE = re.compile(r'^[0-9a-f]{12}$')


def _check_sid(sid) -> str:
    s = str(sid or '')
    if not _SID_RE.match(s):
        raise ValueError(f'非法会话 id: {s!r}')
    return s


# 落盘瘦身：工具结果与思考过程只保留前 N 字符
# （messages.json 会被频繁整体读写，20 条 12KB 的工具结果就能撑到几 MB）
_TOOL_RESULT_KEEP = 600
_THINK_KEEP = 20000


def _slim_extra(extra: dict) -> dict:
    """把要落盘的消息附加字段瘦身：工具结果/参数/思考截断，避免 JSON 无限膨胀。"""
    out = dict(extra)
    steps = out.get('tool_steps')
    if isinstance(steps, list):
        slim = []
        for s in steps[:60]:
            if not isinstance(s, dict):
                continue
            d = dict(s)
            r = str(d.get('result') or '')
            if len(r) > _TOOL_RESULT_KEEP:
                d['result'] = r[:_TOOL_RESULT_KEEP] + f"…(已截断，原 {len(r)} 字符)"
            a = d.get('args')
            if isinstance(a, dict):
                d['args'] = {k: (v if isinstance(v, (int, float, bool)) or v is None
                                 else str(v)[:200])
                             for k, v in a.items()}
            slim.append(d)
        out['tool_steps'] = slim
    t = out.get('thinking')
    if isinstance(t, str) and len(t) > _THINK_KEEP:
        out['thinking'] = t[:_THINK_KEEP] + '\n…(思考过长，已截断)'
    return out


class Session:
    def __init__(self, sid, title, created_at, messages=None, artifacts=None,
                 pinned=False, compact=None):
        self.id = sid
        self.title = title
        self.created_at = created_at
        self.messages = messages if messages is not None else []
        self.artifacts = artifacts if artifacts is not None else []
        self.pinned = bool(pinned)
        # 手动压缩记录 {summary, upto, ts, ...}：只影响“发给模型的历史”，
        # 完整对话仍按原样展示（用户全程可回看，不丢消息）
        self.compact = compact or None

    def to_meta(self):
        # 失败工具数：扫描全部消息里的 tool_steps，仅统计显式 ok=False 的
        fails = 0
        last_ts = 0.0
        for m in self.messages:
            for s in (m.get('tool_steps') or []):
                if s.get('ok') is False:
                    fails += 1
            ts = m.get('ts')
            if isinstance(ts, (int, float)):
                last_ts = max(last_ts, ts)
        # 无消息时回退到创建时间（epoch 秒），供“最后活跃”排序/展示
        if not last_ts and self.created_at:
            try:
                last_ts = datetime.fromisoformat(self.created_at).timestamp()
            except Exception:
                last_ts = 0.0
        # 最后一条消息预览（会话列表展示用）
        preview = ''
        if self.messages:
            preview = (self.messages[-1].get('content') or '').strip().replace('\n', ' ')[:60]
        return {"id": self.id, "title": self.title, "created_at": self.created_at,
                "msg_count": len(self.messages),
                "artifact_count": len(self.artifacts),
                "fail_count": fails,
                "last_ts": last_ts,
                "pinned": self.pinned,
                "preview": preview}


class SessionManager:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        (self.data_dir / 'sessions').mkdir(parents=True, exist_ok=True)

    def _sdir(self, sid) -> Path:
        # _check_sid 在这里统一拦：delete/load/_save 等所有入口都经 _sdir。
        return self.data_dir / 'sessions' / _check_sid(sid)

    def _sfile(self, sid, name) -> Path:
        return self._sdir(sid) / name

    # ── CRUD ─────────────────────────────────────────────────────
    def create(self, title: str = "新会话") -> Session:
        sid = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec='seconds')
        s = Session(sid, _slug_title(title), now)
        self._sdir(sid).mkdir(parents=True, exist_ok=True)
        self._save(s)
        return s

    def load(self, sid: str) -> Session:
        _check_sid(sid)
        if not self._sdir(sid).is_dir():
            # 不存在的 sid 直接抛错，不再返回“幽灵空会话”——否则调用方
            # _save 后会在磁盘上凭空建出会话目录，聊天接口的 400 分支也永不触发。
            raise FileNotFoundError(f'会话不存在: {sid}')
        s = Session(sid, '新会话', '')
        s.messages = self._read_json(sid, 'messages.json') or []
        s.artifacts = self._read_json(sid, 'artifacts.json') or []
        meta = self._read_json(sid, 'meta.json') or {}
        s.title = meta.get('title', s.title)
        s.created_at = meta.get('created_at', '')
        s.pinned = bool(meta.get('pinned', False))
        s.compact = self._read_json(sid, 'compact.json') or None
        return s

    def list_all(self) -> list:
        """会话列表（只读 meta.json，不解析 messages.json）。

        旧实现每次都把每个会话的全部消息读进来，只为算消息数/失败数/预览；
        几百条消息 × 十几个会话后，每次刷新都要几百毫秒。现在这些统计在 _save
        时增量写进 meta.json，列表接口只读 meta。
        """
        out = []
        base = self.data_dir / 'sessions'
        if not base.is_dir():
            return out
        for d in sorted(os.listdir(base), reverse=True):
            if not _SID_RE.match(d):
                continue  # 会话目录外的杂项（非 create() 生成的 id）不参与列表
            sdir = self._sdir(d)
            if not sdir.is_dir():
                continue
            try:
                meta = json.loads((sdir / 'meta.json').read_text(encoding='utf-8'))
            except Exception:
                continue
            if 'msg_count' in meta:      # 新版：直接读缓存统计
                out.append({
                    "id": d, "title": meta.get('title', '新会话'),
                    "created_at": meta.get('created_at', ''),
                    "msg_count": meta.get('msg_count', 0),
                    "artifact_count": meta.get('artifact_count', 0),
                    "fail_count": meta.get('fail_count', 0),
                    "last_ts": meta.get('last_ts', 0),
                    "pinned": bool(meta.get('pinned', False)),
                    "preview": meta.get('preview', ''),
                })
                continue
            # 旧数据兼容：没有缓存统计时回退到全量解析
            try:
                s = Session(d, meta.get('title', '新会话'), meta.get('created_at', ''))
                s.messages = self._read_json(d, 'messages.json') or []
                s.artifacts = self._read_json(d, 'artifacts.json') or []
                s.pinned = bool(meta.get('pinned', False))
                out.append(s.to_meta())
            except Exception:
                continue
        return out

    def rename(self, sid: str, title: str):
        s = self.load(sid)
        s.title = _slug_title(title)
        self._save(s)

    def set_pinned(self, sid: str, pinned: bool):
        s = self.load(sid)
        s.pinned = bool(pinned)
        self._save(s)
        return s.pinned

    def fork(self, sid: str, index: int) -> "Session":
        """从第 index 条消息（含）分叉出新会话，保留 messages[:index+1]。

        用于「从这里分支」：原会话不动，新会话接着这条继续，
        产物清单一并复制（文件是共享的，只是换个上下文继续聊）。
        """
        s = self.load(sid)
        idx = max(0, int(index))
        if idx >= len(s.messages):
            raise ValueError('分叉点超出消息范围')
        keep = list(s.messages[:idx + 1])
        if not keep:
            raise ValueError('分叉点没有可保留的消息')
        ns = self.create(s.title + ' · 分支')
        ns.messages = keep
        ns.artifacts = list(s.artifacts)
        self._save(ns)
        return ns

    def truncate(self, sid: str, index: int) -> int:
        """删除第 index 条及其之后的消息，返回删除条数（不可恢复，调用方需二次确认）。"""
        s = self.load(sid)
        idx = max(0, int(index))
        removed = len(s.messages) - idx
        if removed <= 0:
            return 0
        s.messages = s.messages[:idx]
        # 删消息会让压缩点下标失效，直接作废（下次可重新压）
        s.compact = None
        self._save(s)
        return removed

    def iter_all_messages(self):
        """遍历所有会话的所有消息（用于附件引用检查）。"""
        sdir = self.data_dir / 'sessions'
        if not sdir.is_dir():
            return
        for d in sorted(os.listdir(sdir), reverse=True):
            sd = sdir / d
            if not sd.is_dir():
                continue
            try:
                s = self.load(d)
            except Exception:
                continue
            for m in s.messages:
                yield m

    def delete(self, sid: str):
        import shutil
        # sid 合法性由 _sdir 内的 _check_sid 保证：'../..' 之类到不了 rmtree。
        shutil.rmtree(self._sdir(sid), ignore_errors=True)

    # ── 会话操作 ─────────────────────────────────────────────────
    def add_message(self, sid: str, role: str, content: str, extra: dict = None):
        s = self.load(sid)
        m = {"role": role, "content": content, "ts": time.time()}
        if extra:
            m.update(_slim_extra(extra))
        s.messages.append(m)
        if len(s.messages) > 400:
            dropped = len(s.messages) - 400
            s.messages = s.messages[-400:]
            # 手动压缩点随消息前移同步左移，否则压缩后会漏发/错位
            cp = s.compact or {}
            if cp:
                cp['upto'] = max(0, int(cp.get('upto') or 0) - dropped)
        self._save(s)

    def set_compact(self, sid: str, summary: str, upto: int, stats: dict = None) -> dict:
        """记录一次手动压缩：upto 之前的消息不再发给模型（原始消息全保留）。"""
        s = self.load(sid)
        rec = {"summary": str(summary or ''), "upto": max(0, int(upto or 0)),
               "ts": datetime.now().isoformat(timespec='seconds')}
        if stats:
            rec.update({k: stats[k] for k in
                        ('before_msgs', 'after_msgs', 'before_chars',
                         'after_chars', 'est_tokens', 'saved_pct') if k in stats})
        s.compact = rec
        self._save(s)
        return rec

    def clear_compact(self, sid: str) -> bool:
        """撤销手动压缩，恢复完整历史。"""
        s = self.load(sid)
        had = bool(s.compact)
        s.compact = None
        self._save(s)
        return had

    def mark_turn_undone(self, sid: str, turn: str) -> bool:
        """标记某一轮已撤销，历史里不再显示可重复撤销的按钮。"""
        s = self.load(sid)
        changed = False
        for m in s.messages:
            snap = m.get('snapshot')
            if isinstance(snap, dict) and str(snap.get('turn')) == str(turn):
                snap['undone'] = True
                changed = True
        if changed:
            self._save(s)
        return changed

    def register_artifacts(self, sid: str, rels: list):
        """登记本轮产物（去重 + 持久化）。产物面板读取此清单。"""
        if not rels:
            return
        s = self.load(sid)
        for r in rels:
            if r not in s.artifacts:
                s.artifacts.append(r)
        self._save(s)

    def session_ctx(self, s: Session) -> SessionCtx:
        """构造当前会话上下文（供 workspace 路由 outputs 子目录）。"""
        return SessionCtx(s.id, s.title, datetime.now().strftime('%Y-%m-%d'))

    def write_work_log(self, sid: str, user_input: str, tool_steps: list, response: str):
        """追加本轮工作日志。

        双落盘：
        - 会话目录 work_log.md（会话级详情）
        - notes/工作日志/<日期>.md（工作台可见、按日期累积，参考老版 agent 格式）
        """
        try:
            now = datetime.now()
            ts = now.strftime('%H:%M:%S')
            # 公共正文（用户 / 工具步骤 / 回复摘要）
            body = [f"**用户**: {str(user_input)[:500]}\n"]
            if tool_steps:
                body.append(f"**工具调用**: {len(tool_steps)} 步\n")
                for s in tool_steps[:12]:
                    mark = '✅' if s.get('ok') else '❌'
                    body.append(f"- {mark} {s['tool']}: {str(s.get('args'))[:80]}\n")
                if len(tool_steps) > 12:
                    body.append(f"- ... 共 {len(tool_steps)} 步\n")
                body.append("\n")
            body.append(f"**回复摘要**: {str(response or '')[:800]}\n")
            with _LOCK:
                # ① 会话目录（原有，会话级详情）
                p = self._sfile(sid, 'work_log.md')
                if not p.exists():
                    p.write_text(f"# 工作日志 {now.strftime('%Y-%m-%d')}\n",
                                 encoding='utf-8')
                with open(p, 'a', encoding='utf-8') as f:
                    f.writelines([f"\n## {ts}\n\n"] + body)
                # ② 工作台可见：notes/工作日志/<日期>.md（按日期累积，参考老版 agent）
                try:
                    ws_root = self.data_dir.parent  # data_dir = <工作台>/.agent
                    d = ws_root / 'notes' / '工作日志'
                    d.mkdir(parents=True, exist_ok=True)
                    pd = d / f"{now.strftime('%Y-%m-%d')}.md"
                    if not pd.exists():
                        pd.write_text(f"# 工作日志 {now.strftime('%Y-%m-%d')}\n",
                                      encoding='utf-8')
                    # 会话标题：查不到就用 id
                    try:
                        title = self.load(sid).title
                    except Exception:
                        title = sid
                    daily = [f"\n### {ts} · {title}\n\n"] + body + ["\n---\n"]
                    with open(pd, 'a', encoding='utf-8') as f:
                        f.writelines(daily)
                except Exception:
                    pass  # 工作台不可写时静默降级，不影响主链路
        except Exception:
            pass

    # ── 会话搜索（全文 LIKE，个人规模足够）──────────────────────
    def search(self, q: str, limit: int = 20) -> list:
        q = (q or '').strip()
        if not q:
            return []
        out = []
        sdir = self.data_dir / 'sessions'
        if not sdir.is_dir():
            return out
        for d in sorted(os.listdir(sdir), reverse=True):
            sdir2 = sdir / d
            if not sdir2.is_dir():
                continue
            try:
                meta = json.loads((sdir2 / 'meta.json').read_text(encoding='utf-8'))
                msgs = json.loads((sdir2 / 'messages.json').read_text(encoding='utf-8'))
            except Exception:
                continue
            for m in msgs:
                c = (m.get('content') or '')
                i = c.lower().find(q.lower())
                if i < 0:
                    continue
                s = max(0, i - 60)
                snip = c[s:i + len(q) + 120].replace('\n', ' ')
                if s > 0:
                    snip = '…' + snip
                out.append({"sid": d, "title": meta.get('title', '新会话'),
                            "role": m.get('role'), "ts": m.get('ts'),
                            "snippet": snip.strip()})
                if len(out) >= limit:
                    return out
        return out

    # ── 会话导出（Markdown）────────────────────────────────────
    def export_markdown(self, sid: str) -> str:
        s = self.load(sid)
        lines = [f"# {s.title}", f"\n> 创建时间：{s.created_at}  ·  消息数：{len(s.messages)}\n"]
        for m in s.messages:
            role = m.get('role')
            c = m.get('content') or ''
            if role == 'user':
                lines.append(f"\n## 🧑 用户\n\n{c}")
            elif role == 'assistant':
                tk = (m.get('thinking') or '').strip()
                if tk:
                    lines.append(f"\n## 🤖 助手（思考过程）\n\n<details>\n\n{tk}\n\n</details>")
                lines.append(f"\n## 🤖 助手\n\n{c}")
                for s2 in (m.get('tool_steps') or []):
                    mark = '✅' if s2.get('ok') else '❌'
                    lines.append(f"- {mark} `{s2.get('tool')}` {str(s2.get('args'))[:120]}")
        if s.artifacts:
            lines.append(f"\n## 📦 产出物\n")
            for a in s.artifacts:
                lines.append(f"- {a}")
        return "\n".join(lines)

    # ── 内部 ─────────────────────────────────────────────────────
    def _read_json(self, sid, name):
        p = self._sfile(sid, name)
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return None

    @staticmethod
    def _atomic_write(p: Path, data: str):
        """原子写入：先写同目录临时文件再 os.replace，断电/崩溃不会留下半截 JSON。"""
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(data)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save(self, s: Session):
        with _LOCK:
            sdir = self._sdir(s.id)
            sdir.mkdir(parents=True, exist_ok=True)
            cp = sdir / 'compact.json'
            if s.compact:
                self._atomic_write(cp, json.dumps(s.compact, ensure_ascii=False))
            elif cp.exists():
                try:
                    cp.unlink()
                except OSError:
                    pass
            m = s.to_meta()
            self._atomic_write(sdir / 'meta.json',
                               json.dumps({"title": s.title, "created_at": s.created_at,
                                           "pinned": s.pinned,
                                           # 列表页需要的统计：随保存增量写入，避免 list_all 全量解析
                                           "msg_count": m["msg_count"],
                                           "artifact_count": m["artifact_count"],
                                           "fail_count": m["fail_count"],
                                           "last_ts": m["last_ts"],
                                           "preview": m["preview"]},
                                          ensure_ascii=False))
            self._atomic_write(sdir / 'messages.json',
                               json.dumps(s.messages, ensure_ascii=False))
            self._atomic_write(sdir / 'artifacts.json',
                               json.dumps(s.artifacts, ensure_ascii=False))
