# -*- coding: utf-8 -*-
"""工作台模型：个人工作空间 + 白名单落盘路由 + 本轮快照（撤销）。

工作台结构（用户日常工作的空间，不是 AI 专属沙盒）::

    <root>/
    ├── inputs/      用户输入素材（网页上传/手动放入，按日期分类）
    ├── outputs/     AI 产出物（按 日期/会话标题 自动分类）
    ├── notes/       长期知识库（AI 可新建/编辑，不分类）
    └── .agent/      AI 内部状态（sessions/ snapshots/ memory.db rag.db logs/）—— 对用户隐藏

落盘白名单:
  - 写 ``outputs/x`` 或裸文件名 ``x``  -> ``outputs/<日期>/<会话>/x``（会话子目录+别名回读）
  - 写 ``notes/x``                     -> ``notes/x``（知识库原地落盘）
  - 写 ``inputs/``、``.agent/``、越界/绝对路径 -> 拒绝（安全，防产出物乱跑）

读取:
  - 工作台内优先（含 outputs 别名回读：AI 写 ``outputs/x`` 实际在会话子目录，
    读 ``outputs/x`` 仍能找到）；工作台外任意目录只读，敏感文件一律拦截。

线程安全:
  会话上下文（SessionCtx）放在 threading.local 里——每个请求一个 Agent 实例、
  在自己的线程里 set_session，多请求并发时不会串会话。

快照/撤销:
  begin_turn() 后，本轮所有经 resolve_write 的写入都会先把原文件备份到
  .agent/snapshots/<sid>/<turn>/，并落一份 journal.json；undo_turn() 可精确回滚
  （新增的文件删除、覆盖的文件还原）。
"""
import difflib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

# 禁止读取/写入的敏感文件模式（防 .env/.pem/.key/凭据 泄露）
SENSITIVE_PATTERNS = [
    r'\.env$', r'\.pem$', r'\.key$', r'id_rsa$', r'id_ed25519$',
    r'\.keystore$', r'\.jks$', r'credentials', r'secret',
    r'\.htpasswd$', r'\.npmrc$', r'\.pypirc$', r'\.netrc$',
    r'\.git[\\/]', r'wallet\.dat$', r'\.kdbx$',
]

# 写入白名单禁止的目录（AI 永远不能写）
WRITE_FORBIDDEN = {'inputs', '.agent', 'sessions', 'work_logs', 'logs'}
# 目录类操作（新建/删除/移动）允许的顶层目录
MANAGED_TOPS = ('outputs', 'notes')

DEFAULT_MAX_UPLOAD_MB = 50


def sanitize_slug(title: str, fallback: str) -> str:
    """把会话标题转成安全目录名（中文保留，去掉路径危险字符）。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(title or '')).strip(' ._')
    return (s[:40] or fallback)


def _decode_text(raw: bytes):
    """按 utf-8 / gb18030 依次尝试解码；都不行返回 None（当二进制处理）。"""
    for enc in ('utf-8', 'gb18030'):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


SIDE_LIMIT = 400        # 并排输出的行数上限（兜底：改动本身就有几百行时才截断）
SIDE_CONTEXT = 3        # 折叠未改动区段时，两端各留几行上下文
SIDE_FOLD_MIN = 8       # 未改动连续达到这么多行才值得折叠（太少折叠反而看不清）
SIDE_MAX_LINES = 20000  # 新旧行数超过这个量级就不再内嵌折叠内容，回退截断（防超大文件）
SIDE_MAX_CHARS = 2_000_000


def _diff_side(old_t: str, new_t: str, limit: int = SIDE_LIMIT,
               context: int = SIDE_CONTEXT) -> dict:
    """逐行对齐的左右两栏数据（并排对比视图用）。

    opcode 级别对齐：相同行、删除行、新增行、替换行分别标记，
    替换行把新旧内容放同一行，宽屏下最容易看出“改了什么”。

    长文件不再一刀切截断：未改动的连续区段折成一行 ``{'t': '≋'}``，
    两端各留 ``context`` 行上下文，行数 ``n`` 与原文 ``l``/``r`` 一并带上，
    前端点一下就地展开（不用再发请求）。行数开销从“文件多长”变成“改动多少”。
    """
    a, b = old_t.splitlines(), new_t.splitlines()
    # 超大文件：不内嵌折叠内容（否则响应体太大），退回旧行为
    huge = (len(a) + len(b) > SIDE_MAX_LINES
            or (len(old_t) + len(new_t)) > SIDE_MAX_CHARS)
    rows = []
    truncated = False

    def add(t, l='', r='', **extra):
        """追加一行；到上限就置 truncated 并返回 False。"""
        nonlocal truncated
        if len(rows) >= limit:
            truncated = True
            return False
        rows.append(dict({'t': t, 'l': l, 'r': r}, **extra))
        return True

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == 'equal':
            n = i2 - i1
            if huge or n <= SIDE_FOLD_MIN:
                for k in range(n):
                    if not add('=', a[i1 + k], b[j1 + k]):
                        break
            else:
                for k in range(context):
                    if not add('=', a[i1 + k], b[j1 + k]):
                        break
                if not truncated:
                    add('≋', a[i1 + context:i2 - context],
                        b[j1 + context:j2 - context], n=n - 2 * context)
                if not truncated:
                    for k in range(i2 - context, i2):
                        if not add('=', a[k], b[k - i2 + j2]):
                            break
        elif tag == 'delete':
            for k in range(i1, i2):
                if not add('-', a[k], ''):
                    break
        elif tag == 'insert':
            for k in range(j1, j2):
                if not add('+', '', b[k]):
                    break
        else:  # replace
            for k in range(max(i2 - i1, j2 - j1)):
                if not add('~',
                           a[i1 + k] if i1 + k < i2 else '',
                           b[j1 + k] if j1 + k < j2 else ''):
                    break
        if truncated:
            break
    return {'rows': rows, 'truncated': truncated,
            'folds': sum(1 for r in rows if r['t'] == '≋')}


def _diff_payload(old_raw: bytes, new_raw: bytes) -> dict:
    """两个字节流 -> 逐行 unified diff（文本类）；二进制只给大小变化。

    供 turn_diff（本轮改动）与 version_diff（产物版本对比）共用。
    """
    old_t, new_t = _decode_text(old_raw), _decode_text(new_raw)
    if old_t is None or new_t is None:
        return {"binary": True, "size_before": len(old_raw),
                "size_after": len(new_raw), "added": 0, "removed": 0,
                "lines": [], "truncated": False}
    lines = []
    added = removed = 0
    truncated = False
    diff = difflib.unified_diff(old_t.splitlines(), new_t.splitlines(),
                                fromfile='旧版本', tofile='当前', lineterm='', n=2)
    for line in diff:
        if line.startswith('+++') or line.startswith('---'):
            continue
        if line.startswith('+'):
            added += 1
        elif line.startswith('-'):
            removed += 1
        if len(lines) < 400:
            tag = line[:1] if line[:1] in ('+', '-', '@', ' ') else ' '
            lines.append({"t": tag, "s": line[:500]})
        else:
            truncated = True
    return {"binary": False, "size_before": len(old_raw),
            "size_after": len(new_raw), "added": added, "removed": removed,
            "lines": lines, "truncated": truncated}


class SessionCtx:
    """当前会话上下文（由会话管理器在每次运行时注入 workspace）。

    - slug: 会话标题的安全目录名（outputs 分类用）
    - date: 运行日期（outputs 分类用）
    - aliases: 别名表 {AI 写的相对路径: 真实相对路径}，保证 AI 读写一致
    """

    def __init__(self, session_id: str, title: str, date_str: str):
        self.id = session_id
        self.slug = sanitize_slug(title, session_id[:8])
        self.date = date_str
        self.aliases = {}


class Workspace:
    def __init__(self, root: str, max_upload_mb: float = DEFAULT_MAX_UPLOAD_MB):
        try:
            self.root = Path(root).resolve()
        except Exception as e:
            raise ValueError(f"工作台路径无效: {root}（{e}）") from e
        self.max_upload_mb = max_upload_mb
        for d in ('inputs', 'outputs', 'notes', '.agent'):
            try:
                (self.root / d).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise PermissionError(
                    f"工作台目录创建失败: {self.root / d}\n"
                    f"原因: {e}\n请检查盘符是否存在（如 D: 未挂载）、目录是否可写，"
                    f"或修改 config.json 的 workspace.root 指向有效路径。") from e
        self._tl = threading.local()   # 线程内：ctx / 本轮快照

    # ── 会话上下文（线程局部）─────────────────────────────────────
    @property
    def _ctx(self):
        return getattr(self._tl, 'ctx', None)

    def set_session(self, ctx: SessionCtx):
        self._tl.ctx = ctx

    def clear_session(self):
        self._tl.ctx = None

    # ── 本轮快照（撤销）──────────────────────────────────────────
    def begin_turn(self, session_id: str, turn_id: str = None):
        """开始一轮写入追踪（Agent.run 开头调用）。"""
        turn = turn_id or str(int(time.time() * 1000))
        d = (self.root / '.agent' / 'snapshots' / sanitize_slug(session_id, 'x') / turn)
        self._tl.snap = {'sid': session_id, 'turn': turn, 'dir': d,
                         'entries': [], 'seen': set()}
        return turn

    def _snap_record(self, real: Path, rel: str):
        snap = getattr(self._tl, 'snap', None)
        if not snap:
            return
        key = str(real)
        if key in snap['seen']:
            return
        snap['seen'].add(key)
        try:
            if real.is_file():
                snap['dir'].mkdir(parents=True, exist_ok=True)
                bak = f"{len(snap['entries'])}.bak"
                shutil.copy2(real, snap['dir'] / bak)
                snap['entries'].append({"rel": rel, "existed": True, "backup": bak})
            else:
                snap['entries'].append({"rel": rel, "existed": False})
        except OSError:
            pass

    def end_turn(self):
        """结束一轮：落 journal.json，返回可撤销信息（无写入则返回 None）。"""
        snap = getattr(self._tl, 'snap', None)
        self._tl.snap = None
        if not snap or not snap['entries']:
            return None
        try:
            d = snap['dir']
            d.mkdir(parents=True, exist_ok=True)
            (d / 'journal.json').write_text(json.dumps({
                "sid": snap['sid'], "turn": snap['turn'], "ts": time.time(),
                "entries": snap['entries']}, ensure_ascii=False), encoding='utf-8')
        except OSError:
            return None
        return {"turn": snap['turn'], "count": len(snap['entries']),
                "files": [e['rel'] for e in snap['entries']]}

    def _snap_root(self, sid: str) -> Path:
        return self.root / '.agent' / 'snapshots' / sanitize_slug(sid, 'x')

    def list_turns(self, sid: str, limit: int = 20) -> list:
        """列出可撤销轮次（最新在前）。"""
        d = self._snap_root(sid)
        out = []
        if not d.is_dir():
            return out
        for sub in sorted(os.listdir(d), reverse=True)[:limit]:
            j = d / sub / 'journal.json'
            if not j.is_file():
                continue
            try:
                data = json.loads(j.read_text(encoding='utf-8'))
            except Exception:
                continue
            out.append({"turn": data.get('turn', sub), "ts": data.get('ts', 0),
                        "count": len(data.get('entries') or []),
                        "files": [e.get('rel') for e in (data.get('entries') or [])]})
        return out

    def undo_turn(self, sid: str, turn: str) -> dict:
        """回滚某一轮的所有写入：新增文件删除、覆盖文件还原。"""
        d = self._snap_root(sid) / str(turn)
        j = d / 'journal.json'
        if not j.is_file():
            raise FileNotFoundError('该轮没有可撤销的记录')
        data = json.loads(j.read_text(encoding='utf-8'))
        restored, removed, failed = 0, 0, []
        for e in reversed(data.get('entries') or []):
            rel = str(e.get('rel') or '')
            try:
                real = (self.root / rel).resolve()
                real.relative_to(self.root.resolve())
            except Exception:
                failed.append(rel)
                continue
            try:
                if e.get('existed'):
                    bak = d / str(e.get('backup') or '')
                    if bak.is_file():
                        real.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(bak, real)
                        restored += 1
                else:
                    if real.is_file():
                        real.unlink()
                        removed += 1
            except OSError:
                failed.append(rel)
        shutil.rmtree(d, ignore_errors=True)
        return {"restored": restored, "removed": removed, "failed": failed}

    def version_diff(self, rel: str, ts: str) -> dict:
        """把某个历史版本与当前文件做统一 diff（产物版本并排对比用）。"""
        rel = str(rel).replace('\\', '/').strip()
        src = self.version_path(rel, ts)
        try:
            old_raw = src.read_bytes()
        except OSError:
            old_raw = b''
        cur = (self.root / rel).resolve()
        cur.relative_to(self.root.resolve())
        try:
            new_raw = cur.read_bytes() if cur.is_file() else b''
        except OSError:
            new_raw = b''
        payload = _diff_payload(old_raw, new_raw)
        if not payload.get('binary'):
            # 并排对比数据（单栏 diff 之外多给一份左右对齐行）
            payload['side'] = _diff_side(_decode_text(old_raw) or '',
                                         _decode_text(new_raw) or '')
        return {"rel": rel, "ts": ts, "filename": src.name, **payload}

    def turn_diff(self, sid: str, turn: str) -> dict:
        """本轮改动的统一 diff（变更审阅 UI 的数据源）。

        读取 journal.json + 备份文件，与当前文件比较。文本类返回逐行 diff，
        二进制只返回大小变化（不假装能看懂 docx 的二进制）。
        """
        d = self._snap_root(sid) / str(turn)
        j = d / 'journal.json'
        if not j.is_file():
            raise FileNotFoundError('该轮没有可审阅的改动记录')
        data = json.loads(j.read_text(encoding='utf-8'))
        out = []
        for e in (data.get('entries') or []):
            rel = str(e.get('rel') or '')
            if not rel:
                continue
            try:
                cur = (self.root / rel).resolve()
                cur.relative_to(self.root.resolve())
            except Exception:
                continue
            bak = d / str(e.get('backup') or '') if e.get('existed') else None
            old_raw = b''
            if bak is not None and bak.is_file():
                try:
                    old_raw = bak.read_bytes()
                except OSError:
                    old_raw = b''
            new_raw = b''
            if cur.is_file():
                try:
                    new_raw = cur.read_bytes()
                except OSError:
                    new_raw = b''
            if e.get('existed') and cur.is_file():
                status = 'modified'
            elif e.get('existed') and not cur.is_file():
                status = 'deleted'
            else:
                status = 'added'

            out.append({"rel": rel, "status": status,
                        **_diff_payload(old_raw, new_raw)})
        return {"sid": sid, "turn": turn, "files": out, "count": len(out)}

    def prune_snapshots(self, keep: int = 60):
        """清理过旧快照，避免 .agent 无限膨胀。"""
        base = self.root / '.agent' / 'snapshots'
        if not base.is_dir():
            return
        dirs = []
        for sid in os.listdir(base):
            p = base / sid
            if not p.is_dir():
                continue
            for sub in os.listdir(p):
                sp = p / sub
                if sp.is_dir():
                    dirs.append((sp.stat().st_mtime, sp))
        dirs.sort(reverse=True)
        for _m, sp in dirs[keep:]:
            shutil.rmtree(sp, ignore_errors=True)

    # ── 路径安全 ──────────────────────────────────────────────────
    def _inside(self, p: Path, base: Path) -> bool:
        try:
            p.resolve().relative_to(base.resolve())
            return True
        except ValueError:
            return False

    def _reject_symlinks(self, path: Path):
        """拒绝工作区路径中所有符号链接，避免白名单操作被重定向到别处。"""
        path = Path(path)
        try:
            rel = path.relative_to(self.root)
        except ValueError as e:
            raise PermissionError(f"路径必须在工作台内: {path}") from e
        if rel.is_absolute() or '..' in rel.parts:
            raise PermissionError(f"非法工作台路径: {path}")
        current = self.root
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                raise PermissionError(f"工作台路径不允许符号链接: {current.name}")

    def is_sensitive(self, p) -> bool:
        s = str(p)
        for pat in SENSITIVE_PATTERNS:
            if re.search(pat, s, re.IGNORECASE):
                return True
        return False

    # ── 产物版本（B3）：覆盖写前存旧版，可查看/恢复 ─────────────
    _KEEP_VERSIONS = 10

    def _version_save(self, real_path: Path, rel: str):
        """覆盖已有产出物前保存旧版（.agent/versions/<real_rel>/<ts>，最多 10 版）。"""
        try:
            if not real_path.is_file():
                return
            vdir = self.root / '.agent' / 'versions' / rel
            vdir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime('%Y%m%d-%H%M%S')
            target = vdir / ts
            n = 0
            while target.exists():
                n += 1
                target = vdir / f'{ts}-{n}'
            shutil.copy2(real_path, target)
            old = sorted((p for p in vdir.iterdir() if p.is_file()),
                         key=lambda p: p.name)
            for p in old[:-self._KEEP_VERSIONS]:
                try:
                    p.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    @staticmethod
    def _vdir_check(root: Path, rel: str) -> Path:
        rel = str(rel).replace('\\', '/').strip()
        if not rel or rel.startswith('/') or '..' in rel.split('/'):
            raise PermissionError(f'非法路径: {rel}')
        return root / '.agent' / 'versions' / rel

    def list_versions(self, rel: str) -> list:
        """列出某产出物的历史版本（新在前）。"""
        try:
            vdir = self._vdir_check(self.root, rel)
        except PermissionError:
            return []
        if not vdir.is_dir():
            return []
        out = []
        for p in sorted((x for x in vdir.iterdir() if x.is_file()),
                        key=lambda x: x.name, reverse=True):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"ts": p.name, "size": int(st.st_size)})
        return out

    def version_path(self, rel: str, ts: str) -> Path:
        """返回某个历史版本的真实路径（带越界校验）。"""
        vdir = self._vdir_check(self.root, rel)
        ts = str(ts).replace('\\', '/').strip()
        if not ts or '/' in ts or ts.startswith('.'):
            raise PermissionError(f'非法版本号: {ts}')
        p = vdir / ts
        self._reject_symlinks(p)
        # 版本目录在工作台内（再次兜底）
        p.resolve().relative_to(self.root.resolve())
        if not p.is_file():
            raise FileNotFoundError('该版本不存在')
        return p

    def restore_version(self, rel: str, ts: str) -> dict:
        """把某个历史版本恢复为当前文件（恢复前先把当前版本也存一版）。"""
        rel = str(rel).replace('\\', '/').strip()
        src = self.version_path(rel, ts)
        target = self.root / rel
        self._reject_symlinks(target)
        real = target.resolve()
        real.relative_to(self.root.resolve())
        if real.is_file():
            self._version_save(real, rel)   # 先存当前版，恢复可逆
        real.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, real)
        return {"restored": rel, "from": ts}

    # ── 写入路由（白名单）─────────────────────────────────────
    def resolve_write(self, rel: str) -> tuple:
        """把 AI 要写的相对路径路由到真实落盘路径。

        返回 (real_path: Path, display_rel: str) 或抛 PermissionError。
        - outputs/x / 裸文件名 -> outputs/<日期>/<会话>/x（登记别名）
        - notes/x             -> notes/x（原地）
        - 其它                 -> 拒绝
        """
        rel = str(rel).replace('\\', '/').strip()
        if not rel or rel.startswith('/') or re.match(r'^[A-Za-z]:', rel):
            raise PermissionError(f"写入拒绝（必须用工作台内相对路径）: {rel}")
        parts = [p for p in rel.split('/') if p not in ('', '.')]
        if not parts:
            raise PermissionError("空路径")
        if any(p == '..' for p in parts):
            raise PermissionError(f"写入拒绝（不允许越界）: {rel}")

        ctx = self._ctx
        if ctx is None:
            raise PermissionError("当前无会话上下文，无法路由写入路径")
        if self.is_sensitive(rel):
            raise PermissionError(f"写入拒绝（敏感文件名）: {rel}")

        top = parts[0]
        if top == 'outputs':
            inner = '/'.join(parts[1:]) or ''
            real_rel = f"outputs/{ctx.date}/{ctx.slug}/{inner}"
            ai_rel = rel
        elif top == 'notes':
            real_rel = rel
            ai_rel = rel
        elif top in WRITE_FORBIDDEN or top.startswith('.'):
            raise PermissionError(f"写入拒绝（{top}/ 是系统/输入目录）: {rel}")
        elif len(parts) == 1 and parts[0].startswith('.'):
            # 裸隐藏文件（如 .gitignore）视为显式根文件，仅 notes/ 下允许
            raise PermissionError(f"写入拒绝（隐藏文件需写明目录，如 notes/{parts[0]}）: {rel}")
        else:
            # 裸文件名或其它相对路径 -> 视为产出物，路由到 outputs 会话子目录
            real_rel = f"outputs/{ctx.date}/{ctx.slug}/{rel}"
            ai_rel = f"outputs/{rel}" if len(parts) == 1 else rel

        real_path = self.root / real_rel
        self._reject_symlinks(real_path)
        real_path.parent.mkdir(parents=True, exist_ok=True)
        self._snap_record(real_path, real_rel)
        # 覆盖已有产出物前先存一版（B3 产物版本，最多 10 版）
        if real_rel.startswith('outputs/'):
            self._version_save(real_path, real_rel)
        # 登记别名：AI 写的路径 -> 真实路径（读写一致）
        if ai_rel != real_rel:
            ctx.aliases[ai_rel] = real_rel
        return real_path, real_rel

    def resolve_managed(self, rel: str, must_exist: bool = False) -> tuple:
        """目录类操作（新建/删除/移动）的路径校验：仅 outputs/ notes/ 内。"""
        rel = str(rel).replace('\\', '/').strip()
        if not rel or rel.startswith('/') or re.match(r'^[A-Za-z]:', rel):
            raise PermissionError(f"路径拒绝（必须用工作台内相对路径）: {rel}")
        parts = [p for p in rel.split('/') if p not in ('', '.')]
        if not parts or any(p == '..' for p in parts):
            raise PermissionError(f"路径拒绝（不允许越界）: {rel}")
        if parts[0] not in MANAGED_TOPS:
            raise PermissionError(f"路径拒绝（只允许 outputs/ 或 notes/）: {rel}")
        if self.is_sensitive(rel):
            raise PermissionError(f"路径拒绝（敏感文件名）: {rel}")
        candidate = self.root / rel
        self._reject_symlinks(candidate)
        real = candidate.resolve()
        if not self._inside(real, self.root):
            raise PermissionError(f"路径拒绝（越界）: {rel}")
        if must_exist and not real.exists():
            raise FileNotFoundError(f"路径不存在: {rel}")
        return real, rel

    # ── 读取路由（任意目录只读 + 工作台内优先 + 敏感过滤）────────────
    def resolve_read(self, rel: str) -> Path:
        """把 AI 要读的路径解析为真实路径。工作台外任意目录可读，敏感文件拦截。"""
        rel = str(rel).replace('\\', '/').strip()
        ctx = self._ctx

        # 1) 工作台内路径优先（含 outputs 别名回读）
        if not re.match(r'^[A-Za-z]:', rel) and not rel.startswith('/'):
            # 会话别名：AI 写 outputs/x 实际在 outputs/<date>/<slug>/x
            if ctx and rel in ctx.aliases:
                p = self.root / ctx.aliases[rel]
                self._reject_symlinks(p)
                if p.exists():
                    return self._guard(p)
            workspace_path = self.root / rel
            self._reject_symlinks(workspace_path)
            cand = workspace_path.resolve()
            if self._inside(cand, self.root) and cand.exists():
                return self._guard(cand)

        # 2) 工作台外：允许读任意目录（安全只读，敏感文件拦截）
        p = Path(rel).resolve()
        return self._guard(p)

    def _guard(self, p: Path) -> Path:
        if self.is_sensitive(p):
            raise PermissionError(f"敏感文件已拦截: {p.name}")
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {p}")
        if p.is_dir():
            raise IsADirectoryError(f"是目录: {p}")
        return p

    # ── 上传 ──────────────────────────────────────────────────────
    def upload_path(self, filename: str) -> Path:
        """网页上传落盘 inputs/<日期>/<文件名>，重名自动加序号，大小由调用方校验。"""
        name = os.path.basename(str(filename).replace('\\', '/'))
        date = time.strftime('%Y-%m-%d')
        d = self.root / 'inputs' / date
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        self._reject_symlinks(p)
        n = 1
        while p.exists():
            stem, ext = os.path.splitext(name)
            p = d / f"{stem}_{n}{ext}"
            self._reject_symlinks(p)
            n += 1
        return p

    # ── 产物列表（供前端展示）──────────────────────────────────────
    def artifact_meta(self, rel: str) -> dict:
        """产出物元信息（大小/扩展名/相对路径），供产物面板展示。

        自动找回：若登记的路径已不存在（文件被移动 / 会话目录结构变化），
        按文件名在 outputs/ notes/ 下搜索真实位置返回——避免刷新后产物面板
        变空、点击报「文件不存在」。
        """
        rel = str(rel).replace('\\', '/')
        try:
            st = (self.root / rel).stat()
        except Exception:
            found = self._find_artifact(rel)
            if not found:
                return {}
            rel = found
            try:
                st = (self.root / rel).stat()
            except Exception:
                return {}
        ext = Path(rel).suffix.lower().lstrip('.')
        return {
            "rel": rel,
            "name": Path(rel).name,
            "ext": ext,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        }

    def _find_artifact(self, rel: str) -> str:
        """按文件名在 outputs/ notes/ 下找回真实位置（防止路径失效）。"""
        name = Path(str(rel).replace('\\', '/')).name
        if not name:
            return ''
        scanned = 0
        for top in ('outputs', 'notes'):
            base = self.root / top
            if not base.is_dir():
                continue
            if base.is_symlink():
                continue
            for dirpath, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [d for d in dirs if not d.startswith('.')
                           and not (Path(dirpath) / d).is_symlink()]
                for f in files:
                    scanned += 1
                    if scanned > 20000:  # 上限保护，防止巨型目录卡死
                        return ''
                    if f != name:
                        continue
                    full = os.path.join(dirpath, f)
                    if Path(full).is_symlink() or self.is_sensitive(self.root / full):
                        continue
                    return os.path.relpath(full, self.root).replace('\\', '/')
        return ''
