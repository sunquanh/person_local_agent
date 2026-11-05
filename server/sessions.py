# -*- coding: utf-8 -*-
"""会话管理器（SQLite 版）。

每个会话对外仍是一个目录 `.agent/sessions/<id>/`（work_log.md 仍在里面），
但消息与产物改存 `.agent/sessions.db`：

- sessions   会话元数据 + 列表页要用的增量统计（不再每次全量解析消息）
- messages   一行一条消息（extra 里放 tool_steps / artifacts / snapshot…）
- artifacts  会话产出物清单
- msg_fts    FTS5 全文索引（中文按二元组切词，与 rag.py 同一套分词）

为什么换掉 JSON：
- 旧实现每次追加消息都要 load 整个会话 + 全量重写 messages.json，O(n²)；
- 超过 400 条会静默丢掉最早的对话（用户无从得知）；
- 全文检索是"读所有会话的所有消息再子串扫描"。

旧数据无痛迁移：首次启动时把 sessions/<id>/messages.json 导入库，
随后把该文件改名为 messages.json.migrated（保留备查，不重复导入）。
"""
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from agent.rag import tokenize
from agent.workspace import SessionCtx

_LOCK = threading.RLock()  # 读-改-写序列全程持锁，防两请求交错丢更新

_TOOL_RESULT_KEEP = 600
_THINK_KEEP = 20000


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


def _slim_extra(extra: dict) -> dict:
    """把要落盘的消息附加字段瘦身：工具结果/参数/思考截断，避免无限膨胀。"""
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


def _fts_body(text) -> str:
    """FTS5 索引文本：中文二元组 + 英文词，空格分隔（与查询侧同一分词）。"""
    return ' '.join(tokenize(str(text or '')))


_MD_NOISE = re.compile(r'```.*?```|`([^`]*)`|\*\*([^*]*)\*\*|^\s{0,3}[#>-]+\s*|^\s*[-*+]\s+',
                       re.S | re.M)


def preview_text(content, limit: int = 60) -> str:
    """列表页的一行摘要：去掉 markdown 符号，别让侧栏出现 `**粗体**` 和反引号。"""
    txt = _MD_NOISE.sub(lambda m: m.group(1) or m.group(2) or ' ', str(content or ''))
    txt = re.sub(r'\s+', ' ', txt).strip()
    return txt[:limit]


class Session:
    def __init__(self, sid, title, created_at, messages=None, artifacts=None,
                 pinned=False, compact=None):
        self.id = sid
        self.title = title
        self.created_at = created_at
        self.messages = messages if messages is not None else []
        self.artifacts = artifacts if artifacts is not None else []
        self.pinned = bool(pinned)
        # 手动压缩记录 {summary, upto, ts, ...}：只影响"发给模型的历史"，
        # 完整对话仍按原样展示（用户全程可回看，不丢消息）
        self.compact = compact or None

    def to_meta(self):
        fails = 0
        last_ts = 0.0
        for m in self.messages:
            for s in (m.get('tool_steps') or []):
                if s.get('ok') is False:
                    fails += 1
            ts = m.get('ts')
            if isinstance(ts, (int, float)):
                last_ts = max(last_ts, ts)
        if not last_ts and self.created_at:
            try:
                last_ts = datetime.fromisoformat(self.created_at).timestamp()
            except Exception:
                last_ts = 0.0
        preview = ''
        if self.messages:
            preview = preview_text(self.messages[-1].get('content'))
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
        self.db_path = self.data_dir / 'sessions.db'
        self._init_db()
        self._migrate_json()

    # ── 存储底座 ─────────────────────────────────────────────────
    @contextmanager
    def _db(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute("""CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT DEFAULT '',
                pinned INTEGER DEFAULT 0,
                compact TEXT DEFAULT '',
                msg_count INTEGER DEFAULT 0,
                last_ts REAL DEFAULT 0,
                preview TEXT DEFAULT '',
                fail_count INTEGER DEFAULT 0)""")
            c.execute("""CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sid TEXT NOT NULL,
                ord INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT DEFAULT '',
                extra TEXT DEFAULT '',
                ts REAL DEFAULT 0,
                created_at REAL DEFAULT 0)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_msg_sid ON messages(sid, ord)")
            c.execute("""CREATE TABLE IF NOT EXISTS artifacts(
                sid TEXT NOT NULL, rel TEXT NOT NULL,
                PRIMARY KEY(sid, rel))""")
            try:
                c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING
                    fts5(sid UNINDEXED, msg_id UNINDEXED, body, tokenize='unicode61')""")
            except sqlite3.OperationalError:
                # 极老的 SQLite（无 FTS5）：检索回退到 LIKE，其它功能不受影响
                pass

    def _has_fts(self) -> bool:
        try:
            with self._db() as c:
                c.execute("SELECT count(*) FROM msg_fts").fetchone()
            return True
        except Exception:
            return False

    # ── 旧 JSON 数据迁移 ─────────────────────────────────────────
    def _migrate_json(self):
        base = self.data_dir / 'sessions'
        if not base.is_dir():
            return
        for d in sorted(os.listdir(base)):
            if not _SID_RE.match(d):
                continue
            sdir = base / d
            src = sdir / 'messages.json'
            if not src.is_file():
                continue
            try:
                with self._db() as c:
                    row = c.execute('SELECT 1 FROM sessions WHERE id=?', (d,)).fetchone()
                if row:
                    src.rename(sdir / 'messages.json.migrated')
                    continue
                msgs = json.loads(src.read_text(encoding='utf-8')) or []
                meta = {}
                try:
                    meta = json.loads((sdir / 'meta.json').read_text(encoding='utf-8')) or {}
                except Exception:
                    pass
                artifacts = []
                try:
                    artifacts = json.loads((sdir / 'artifacts.json')
                                           .read_text(encoding='utf-8')) or []
                except Exception:
                    pass
                compact = None
                try:
                    compact = json.loads((sdir / 'compact.json')
                                         .read_text(encoding='utf-8')) or None
                except Exception:
                    pass
                s = Session(d, meta.get('title', '新会话'),
                            meta.get('created_at', ''), messages=msgs,
                            artifacts=artifacts,
                            pinned=bool(meta.get('pinned', False)),
                            compact=compact)
                self._insert_session(s)
                src.rename(sdir / 'messages.json.migrated')
            except Exception:
                # 单个会话迁移失败不阻塞启动：下次启动会再试
                continue

    # ── 内部写入 ─────────────────────────────────────────────────
    def _insert_session(self, s: Session):
        with _LOCK, self._db() as c:
            c.execute("INSERT OR REPLACE INTO sessions(id, title, created_at, pinned, compact) "
                      "VALUES(?,?,?,?,?)",
                      (s.id, s.title, s.created_at, 1 if s.pinned else 0,
                       json.dumps(s.compact, ensure_ascii=False) if s.compact else ''))
            for i, m in enumerate(s.messages):
                self._insert_message(c, s.id, i, m)
            for rel in s.artifacts:
                c.execute("INSERT OR IGNORE INTO artifacts(sid, rel) VALUES(?,?)",
                          (s.id, rel))
            self._refresh_stats(c, s.id)

    def _insert_message(self, c, sid, ordv, m):
        extra = {k: v for k, v in m.items()
                 if k not in ('role', 'content', 'ts')}
        ts = float(m.get('ts') or time.time())
        cur = c.execute("INSERT INTO messages(sid, ord, role, content, extra, ts, created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (sid, ordv, m.get('role') or 'user', m.get('content') or '',
                         json.dumps(extra, ensure_ascii=False) if extra else '',
                         ts, time.time()))
        body = _fts_body(m.get('content') or '')
        try:
            c.execute("INSERT INTO msg_fts(sid, msg_id, body) VALUES(?,?,?)",
                      (sid, cur.lastrowid, body))
        except sqlite3.OperationalError:
            pass
        return cur.lastrowid

    def _refresh_stats(self, c, sid):
        row = c.execute("SELECT COUNT(*) n, MAX(ts) last FROM messages WHERE sid=?",
                        (sid,)).fetchone()
        n, last = (row['n'], row['last']) if row else (0, 0)
        prev = ''
        if n:
            r2 = c.execute("SELECT content FROM messages WHERE sid=? ORDER BY ord DESC LIMIT 1",
                           (sid,)).fetchone()
            prev = (r2['content'] or '').strip().replace('\n', ' ')[:60] if r2 else ''
        fails = 0
        for r in c.execute("SELECT extra FROM messages WHERE sid=? AND role='assistant'",
                           (sid,)):
            try:
                ex = json.loads(r['extra'] or '{}')
            except Exception:
                continue
            for st in (ex.get('tool_steps') or []):
                if isinstance(st, dict) and st.get('ok') is False:
                    fails += 1
        c.execute("UPDATE sessions SET msg_count=?, last_ts=?, preview=?, fail_count=? "
                  "WHERE id=?", (n, float(last or 0), prev, fails, sid))

    # ── 路径 ─────────────────────────────────────────────────────
    def _sdir(self, sid) -> Path:
        return self.data_dir / 'sessions' / _check_sid(sid)

    def _exists(self, sid) -> bool:
        with self._db() as c:
            return c.execute('SELECT 1 FROM sessions WHERE id=?',
                             (sid,)).fetchone() is not None

    # ── CRUD ─────────────────────────────────────────────────────
    def create(self, title: str = "新会话") -> Session:
        sid = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec='seconds')
        s = Session(sid, _slug_title(title), now)
        self._sdir(sid).mkdir(parents=True, exist_ok=True)
        with _LOCK:
            self._insert_session(s)
        return s

    def load(self, sid: str) -> Session:
        sid = _check_sid(sid)
        with self._db() as c:
            meta = c.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
            if meta is None:
                raise FileNotFoundError(f'会话不存在: {sid}')
            rows = c.execute('SELECT * FROM messages WHERE sid=? ORDER BY ord, id',
                             (sid,)).fetchall()
            arts = [r['rel'] for r in
                    c.execute('SELECT rel FROM artifacts WHERE sid=? ORDER BY rowid', (sid,))]
        s = Session(sid, meta['title'], meta['created_at'], pinned=bool(meta['pinned']))
        s.artifacts = arts
        if meta['compact']:
            try:
                s.compact = json.loads(meta['compact'])
            except Exception:
                s.compact = None
        msgs = []
        for r in rows:
            m = {"role": r['role'], "content": r['content'], "ts": r['ts']}
            if r['extra']:
                try:
                    m.update(json.loads(r['extra']))
                except Exception:
                    pass
            msgs.append(m)
        s.messages = msgs
        return s

    def list_all(self) -> list:
        """会话列表：只读 sessions 表的增量统计，不再解析每条消息。"""
        out = []
        with self._db() as c:
            rows = c.execute("""SELECT s.*, (SELECT COUNT(*) FROM artifacts a
                                WHERE a.sid=s.id) AS artifact_count
                                FROM sessions s
                                ORDER BY s.last_ts DESC, s.created_at DESC""").fetchall()
        for r in rows:
            out.append({
                "id": r['id'], "title": r['title'], "created_at": r['created_at'],
                "msg_count": r['msg_count'], "artifact_count": r['artifact_count'],
                "fail_count": r['fail_count'], "last_ts": r['last_ts'],
                "pinned": bool(r['pinned']), "preview": r['preview'],
            })
        return out

    def rename(self, sid: str, title: str):
        sid = _check_sid(sid)
        with _LOCK, self._db() as c:
            c.execute('UPDATE sessions SET title=? WHERE id=?', (_slug_title(title), sid))

    def set_pinned(self, sid: str, pinned: bool):
        sid = _check_sid(sid)
        with _LOCK, self._db() as c:
            c.execute('UPDATE sessions SET pinned=? WHERE id=?',
                      (1 if pinned else 0, sid))
        return bool(pinned)

    def fork(self, sid: str, index: int) -> "Session":
        """从第 index 条消息（含）分叉出新会话，保留 messages[:index+1]。"""
        with _LOCK:
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
            self._insert_session(ns)
            return ns

    def truncate(self, sid: str, index: int) -> int:
        """删除第 index 条及其之后的消息，返回删除条数（不可恢复）。"""
        sid = _check_sid(sid)
        with _LOCK:
            with self._db() as c:
                total = c.execute('SELECT COUNT(*) n FROM messages WHERE sid=?',
                                  (sid,)).fetchone()['n']
            idx = max(0, int(index))
            removed = max(0, total - idx)
            if not removed:
                return 0
            with self._db() as c:
                ids = [r['id'] for r in c.execute(
                    'SELECT id FROM messages WHERE sid=? ORDER BY ord, id', (sid,))]
                doomed = ids[idx:]
                if doomed:
                    q = ','.join('?' * len(doomed))
                    c.execute(f'DELETE FROM messages WHERE id IN ({q})', doomed)
                    try:
                        c.execute(f'DELETE FROM msg_fts WHERE msg_id IN ({q})', doomed)
                    except sqlite3.OperationalError:
                        pass
                # 删消息会让压缩点下标失效，直接作废（下次可重新压）
                c.execute("UPDATE sessions SET compact='' WHERE id=?", (sid,))
                self._refresh_stats(c, sid)
            return removed

    def iter_all_messages(self):
        with self._db() as c:
            rows = c.execute('SELECT sid, role, content, extra, ts FROM messages '
                             'ORDER BY sid DESC, ord').fetchall()
        for r in rows:
            m = {"role": r['role'], "content": r['content'], "ts": r['ts']}
            if r['extra']:
                try:
                    m.update(json.loads(r['extra']))
                except Exception:
                    pass
            yield m

    def delete(self, sid: str):
        sid = _check_sid(sid)
        with _LOCK:
            with self._db() as c:
                c.execute('DELETE FROM messages WHERE sid=?', (sid,))
                try:
                    c.execute('DELETE FROM msg_fts WHERE sid=?', (sid,))
                except sqlite3.OperationalError:
                    pass
                c.execute('DELETE FROM artifacts WHERE sid=?', (sid,))
                c.execute('DELETE FROM sessions WHERE id=?', (sid,))
            shutil.rmtree(self._sdir(sid), ignore_errors=True)

    # ── 会话操作 ─────────────────────────────────────────────────
    def add_message(self, sid: str, role: str, content: str, extra: dict = None):
        sid = _check_sid(sid)
        m = {"role": role, "content": content, "ts": time.time()}
        if extra:
            m.update(_slim_extra(extra))
        with _LOCK, self._db() as c:
            nxt = (c.execute('SELECT COALESCE(MAX(ord), -1) m FROM messages WHERE sid=?',
                             (sid,)).fetchone()['m'] + 1)
            self._insert_message(c, sid, nxt, m)
            # 增量维护列表页统计：不重算全部消息（旧版就是每次全量扫描）
            fails = sum(1 for st in (m.get('tool_steps') or [])
                        if isinstance(st, dict) and st.get('ok') is False)
            preview = preview_text(content)
            c.execute("UPDATE sessions SET msg_count=msg_count+1, last_ts=?, "
                      "preview=?, fail_count=fail_count+? WHERE id=?",
                      (float(m['ts']), preview, fails, sid))

    def set_compact(self, sid: str, summary: str, upto: int, stats: dict = None) -> dict:
        """记录一次手动压缩：upto 之前的消息不再发给模型（原始消息全保留）。"""
        sid = _check_sid(sid)
        rec = {"summary": str(summary or ''), "upto": max(0, int(upto or 0)),
               "ts": datetime.now().isoformat(timespec='seconds')}
        if stats:
            rec.update({k: stats[k] for k in
                        ('before_msgs', 'after_msgs', 'before_chars',
                         'after_chars', 'est_tokens', 'saved_pct') if k in stats})
        with _LOCK, self._db() as c:
            c.execute('UPDATE sessions SET compact=? WHERE id=?',
                      (json.dumps(rec, ensure_ascii=False), sid))
        return rec

    def clear_compact(self, sid: str) -> bool:
        sid = _check_sid(sid)
        with _LOCK, self._db() as c:
            row = c.execute('SELECT compact FROM sessions WHERE id=?', (sid,)).fetchone()
            had = bool(row and row['compact'])
            c.execute("UPDATE sessions SET compact='' WHERE id=?", (sid,))
        return had

    def mark_turn_undone(self, sid: str, turn: str) -> bool:
        sid = _check_sid(sid)
        changed = False
        with _LOCK, self._db() as c:
            rows = c.execute("SELECT id, extra FROM messages WHERE sid=? AND extra LIKE '%snapshot%'",
                             (sid,)).fetchall()
            for r in rows:
                try:
                    ex = json.loads(r['extra'] or '{}')
                except Exception:
                    continue
                snap = ex.get('snapshot')
                if isinstance(snap, dict) and str(snap.get('turn')) == str(turn):
                    snap['undone'] = True
                    ex['snapshot'] = snap
                    c.execute('UPDATE messages SET extra=? WHERE id=?',
                              (json.dumps(ex, ensure_ascii=False), r['id']))
                    changed = True
        return changed

    def register_artifacts(self, sid: str, rels: list):
        if not rels:
            return
        sid = _check_sid(sid)
        with _LOCK, self._db() as c:
            for r in rels:
                c.execute('INSERT OR IGNORE INTO artifacts(sid, rel) VALUES(?,?)',
                          (sid, r))

    def remove_attachment(self, sid: str, ts: float, path: str) -> str:
        """从 ts 附近的用户消息里删掉一个附件引用。"""
        sid = _check_sid(sid)
        with _LOCK, self._db() as c:
            rows = c.execute("SELECT id, ts, extra FROM messages WHERE sid=? AND role='user'",
                             (sid,)).fetchall()
            cands = [r for r in rows if abs(float(r['ts'] or 0) - float(ts or 0)) <= 5]
            if not cands:
                return 'no_message'
            row = min(cands, key=lambda x: abs(float(x['ts'] or 0) - float(ts or 0)))
            try:
                ex = json.loads(row['extra'] or '{}')
            except Exception:
                ex = {}
            atts = ex.get('attachments') or []
            kept = [a for a in atts
                    if (a if isinstance(a, str) else (a or {}).get('path', '')) != path]
            if len(kept) == len(atts):
                return 'no_attachment'
            ex['attachments'] = kept
            c.execute('UPDATE messages SET extra=? WHERE id=?',
                      (json.dumps(ex, ensure_ascii=False) if ex else '', row['id']))
        return 'removed'

    def pop_trailing_assistant(self, sid: str) -> int:
        sid = _check_sid(sid)
        n = 0
        with _LOCK, self._db() as c:
            while True:
                r = c.execute('SELECT id, role FROM messages WHERE sid=? ORDER BY ord DESC, id DESC '
                              'LIMIT 1', (sid,)).fetchone()
                if not r or r['role'] != 'assistant':
                    break
                c.execute('DELETE FROM messages WHERE id=?', (r['id'],))
                try:
                    c.execute('DELETE FROM msg_fts WHERE msg_id=?', (r['id'],))
                except sqlite3.OperationalError:
                    pass
                n += 1
            if n:
                self._refresh_stats(c, sid)
        return n

    def session_ctx(self, s: Session) -> SessionCtx:
        return SessionCtx(s.id, s.title, datetime.now().strftime('%Y-%m-%d'))

    def write_work_log(self, sid: str, user_input: str, tool_steps: list, response: str):
        """追加本轮工作日志（会话目录 + notes/工作日志/<日期>.md）。"""
        try:
            now = datetime.now()
            ts = now.strftime('%H:%M:%S')
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
                p = self._sdir(sid) / 'work_log.md'
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_text(f"# 工作日志 {now.strftime('%Y-%m-%d')}\n",
                                 encoding='utf-8')
                with open(p, 'a', encoding='utf-8') as f:
                    f.writelines([f"\n## {ts}\n\n"] + body)
                try:
                    ws_root = self.data_dir.parent  # data_dir = <工作台>/.agent
                    d = ws_root / 'notes' / '工作日志'
                    d.mkdir(parents=True, exist_ok=True)
                    pd = d / f"{now.strftime('%Y-%m-%d')}.md"
                    if not pd.exists():
                        pd.write_text(f"# 工作日志 {now.strftime('%Y-%m-%d')}\n",
                                      encoding='utf-8')
                    try:
                        title = self.load(sid).title
                    except Exception:
                        title = sid
                    daily = [f"\n### {ts} · {title}\n\n"] + body + ["\n---\n"]
                    with open(pd, 'a', encoding='utf-8') as f:
                        f.writelines(daily)
                except Exception:
                    pass
        except Exception:
            pass

    # ── 会话搜索（FTS5，中文按二元组匹配）──────────────────────
    def search(self, q: str, limit: int = 20) -> list:
        q = (q or '').strip()
        if not q:
            return []
        terms = tokenize(q)
        out = []
        if terms and self._has_fts():
            match = ' OR '.join('"%s"' % t.replace('"', '') for t in terms[:12])
            try:
                with self._db() as c:
                    rows = c.execute("""SELECT m.sid, m.role, m.content, m.ts, s.title
                                        FROM msg_fts f
                                        JOIN messages m ON m.id = f.msg_id
                                        JOIN sessions s ON s.id = m.sid
                                        WHERE msg_fts MATCH ?
                                        ORDER BY rank LIMIT ?""",
                                     (match, max(limit, 1))).fetchall()
                for r in rows:
                    out.append(self._hit(r['sid'], r['title'], r['role'],
                                         r['content'] or '', r['ts'], q))
                return out[:limit]
            except Exception:
                out = []
        # 兜底：FTS 不可用 / 查询词被分词吃空 -> 子串扫描（个人规模可接受）
        needle = q.lower()
        with self._db() as c:
            rows = c.execute("""SELECT m.sid, m.role, m.content, m.ts, s.title
                                FROM messages m JOIN sessions s ON s.id=m.sid
                                WHERE lower(m.content) LIKE ? ORDER BY m.ts DESC LIMIT ?""",
                             ('%' + needle + '%', limit)).fetchall()
        for r in rows:
            out.append(self._hit(r['sid'], r['title'], r['role'],
                                 r['content'] or '', r['ts'], q))
        return out[:limit]

    @staticmethod
    def _hit(sid, title, role, content, ts, q):
        i = content.lower().find(q.lower())
        if i < 0:
            i = 0
        start = max(0, i - 60)
        snip = content[start:i + len(q) + 120].replace('\n', ' ')
        if start > 0:
            snip = '…' + snip
        return {"sid": sid, "title": title, "role": role, "ts": ts,
                "snippet": snip.strip()}

    # ── 会话导出（Markdown）─────────────────────────────────────
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
            lines.append("\n## 📦 产出物\n")
            for a in s.artifacts:
                lines.append(f"- {a}")
        return "\n".join(lines)
