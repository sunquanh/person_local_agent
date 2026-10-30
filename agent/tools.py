# -*- coding: utf-8 -*-
"""Agent 工具集。

每个工具: name / description / parameters(JSON Schema) / execute(args)->ToolResult
execute 返回 ToolResult(text, artifacts[, image])。artifacts 为工作台相对路径列表,
写文件类工具自动登记 -> 会话产出物面板展示（产物跟踪=工具登记，无正则启发式）。

办公文档（word/excel/ppt）依赖可选库，未安装时工具保留但返回明确提示。
所有工具纯标准库实现，不引入任何需要联网安装的依赖。
"""
import base64
import itertools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import logsetup

log = logsetup.get('tools')

# ── 危险命令检测（命中即要求用户确认）──────────────────────────────
# 启发式正则：只拦常见 shell 写法，对拼接、编码、变量展开后的实际命令
# 没有拦截保证；对 execute_python 的代码文本更不构成安全保证——代码执行
# 不走这条白名单，见 core._approve_tool 的单独分支。
_DANGEROUS = [
    r'\brm\s+-rf\b', r'\brm\s+-r\b', r'\brmdir\s+/s', r'\brd\s+/s',
    r'\bdel\s+/[fq]', r'\bformat\b', r'\bdiskpart\b', r'\bshutdown\b',
    r'\bmkfs', r'\bdd\s+if=', r':\s*rmdir\s+/s', r'\bgit\s+push\b',
    r'\bgit\s+reset\s+--hard\b', r'\bgit\s+clean\s+-f', r'\bdrop\s+database\b',
    r'\btruncate\s+table\b', r'\bDROP\s+TABLE\b',
    # PowerShell / 计划任务 / 注册表 等同样具有破坏力的写法
    r'\bremove-item\b', r'\bri\s+-r', r'\bclear-content\b',
    r'\breg\s+delete\b', r'\bschtasks\s+/delete\b', r'\btaskkill\b',
    r'\bnet\s+user\b.*/delete', r'\bcipher\s+/w', r'\bicacls\b',
]

# 只读工具：可并行执行（互不依赖，纯读取）
READ_ONLY_TOOLS = {
    'read_file', 'list_dir', 'glob_search', 'grep_search',
    'read_word', 'read_excel', 'read_ppt', 'read_image',
    'memory_recall',
}

# 写/执行类工具（只读模式下禁用）
WRITE_TOOLS = {'write_file', 'edit_file', 'write_word', 'write_excel', 'write_ppt',
               'delete_file', 'move_file', 'mkdir'}
CODE_TOOLS = {'run_command', 'execute_python'}
ASK_FORBIDDEN = WRITE_TOOLS | CODE_TOOLS


# ── 审批档位（B4，对齐 Codex 的 approval policy）──────────────
# readonly   : 禁止一切写入/命令（与只读模式同效）
# suggest    : 读取自动；outputs/notes 写入自动；删除/移动与其它命令逐条确认
# auto_edit  : 写文件全自动；命令仍逐条确认
# trusted    : 全部自动放行，只拦危险命令（危险命令永远人工确认）
PROFILES = ('readonly', 'suggest', 'auto_edit', 'trusted')

# 只读命令白名单（suggest/auto_edit 档下自动放行，按整条命令前缀匹配）
_READONLY_CMDS = ('dir', 'ls', 'pwd', 'cd', 'cat', 'type', 'tree', 'echo',
                  'findstr', 'grep', 'find', 'wc', 'head', 'tail', 'stat',
                  'whoami', 'date', 'time', 'ver', 'uname', 'df', 'du',
                  'git status', 'git diff', 'git log', 'git branch',
                  'python --version', 'python -V', 'pip list', 'node -v')


def _is_dangerous(cmd: str) -> bool:
    return any(re.search(p, cmd, re.IGNORECASE) for p in _DANGEROUS)


def _kill_process_tree(proc: subprocess.Popen):
    """强制结束进程树（Windows 用 taskkill /T，POSIX 用进程组 SIGKILL）。"""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _decode(data: bytes) -> str:
    """命令输出解码：utf-8 -> gb18030 -> latin-1。

    中文 Windows 的 ipconfig/dir 等系统命令输出是 GBK，直接按 utf-8 解会全是乱码，
    模型看到乱码等于看不到错误原因。
    """
    if not data:
        return ''
    for enc in ('utf-8', 'gb18030'):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode('utf-8', errors='replace')


def _read_text_best_effort(p: Path):
    """按 utf-8 -> gb18030 -> latin-1 试读，返回 (文本, 编码)；与 read_file 同口径。"""
    raw = p.read_bytes()
    for enc in ('utf-8', 'gb18030', 'latin-1'):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', errors='replace'), 'utf-8'


class ToolResult:
    def __init__(self, text: str, artifacts=None, image=None):
        self.text = text
        self.artifacts = list(artifacts or [])
        self.image = image        # 图片绝对路径（多模态模型用）

    def to_dict(self):
        return {"text": self.text, "artifacts": self.artifacts}


def _safe_name(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9_]', '_', name)


class Tool:
    def __init__(self, name, description, parameters, fn):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.fn = fn

    def schema(self):
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}

    def execute(self, args: dict):
        try:
            r = self.fn(args or {})
            if isinstance(r, ToolResult):
                return r
            return ToolResult(str(r))
        except Exception as e:  # 工具错误返回给模型，不中断循环
            log.warning('工具 %s 执行失败: %s: %s', self.name, type(e).__name__, e)
            return ToolResult(f"❌ 工具执行失败: {type(e).__name__}: {e}")


# ── 可选依赖检测（启动环境检测 / /api/health 共用）───────────────
_OPTIONAL_DEPS = (
    ('docx', 'python-docx'), ('openpyxl', 'openpyxl'), ('pptx', 'python-pptx'),
    ('pypdf', 'pypdf'), ('PIL', 'Pillow'),
)


def detect_optional_deps() -> dict:
    """返回 {显示名: 是否可用}。启动时打印 + /api/health 返回，供离线机检查。"""
    import importlib
    out = {}
    for mod, label in _OPTIONAL_DEPS:
        try:
            importlib.import_module(mod)
            out[label] = True
        except Exception:
            out[label] = False
    return out


class ToolRegistry:
    def __init__(self, ws, memory, confirm_callback=None, allow_code_exec=True,
                 llm=None, hooks=None, allow_delegate=True, vision=False,
                 profile='suggest', auto_approve=None, session_allow=None):
        self.ws = ws
        self.memory = memory
        self.confirm_callback = confirm_callback  # (cmd)->bool 危险命令/写入确认
        self.allow_code_exec = allow_code_exec    # False = 不注册 run_command/execute_python
        # 审批档位（B4）
        self.profile = profile if profile in PROFILES else 'suggest'
        self.auto_approve = dict(auto_approve or {})
        # 本次运行用户批准过的完整命令（内存态，不落盘，重启清空）
        self.session_allow = session_allow if session_allow is not None else []
        self.pre_approved = set()     # 本轮已确认过的完整命令（避免二次弹窗）
        self.allow_delegate = allow_delegate      # 子 Agent 不再注册 delegate_task（防递归）
        self.vision = vision                      # 模型是否支持图片输入
        self._llm = llm                           # 供 delegate_task 创建子Agent 用
        self._hooks = hooks                       # Agent 实例（todo/ask_user 回调用）
        self.tools = {}
        self._register_all()

    # ── 审批策略（B4）────────────────────────────────────────
    def write_auto_approved(self, name: str, path: str) -> bool:
        """写/删/移类工具是否自动放行。"""
        if self.profile == 'readonly':
            return False
        if self.profile in ('auto_edit', 'trusted'):
            return True
        # suggest 档：按目录白名单 + delete_move 开关
        if name in ('delete_file', 'move_file'):
            return bool(self.auto_approve.get('delete_move', False))
        rel = str(path or '').replace('\\', '/').lstrip('/')
        if rel.startswith('outputs/'):
            return bool(self.auto_approve.get('write_outputs', True))
        if rel.startswith('notes/'):
            return bool(self.auto_approve.get('write_notes', True))
        return False

    def cmd_auto_approved(self, cmd: str) -> bool:
        """命令/代码是否自动放行。危险命令与只读模式永远例外。"""
        c = (cmd or '').strip()
        if not c or self.profile == 'readonly':
            return False
        if _is_dangerous(c):                     # 危险命令永远人工确认
            return False
        if self.profile == 'trusted' or self.auto_approve.get('run_other_cmds'):
            return True
        # shell 连接/重定向/替换元字符一律走人工确认：白名单判定只看“第一段
        # 前缀”，而实际执行的是整条命令，`echo x > 盘外路径`、`dir && del 文件`、
        # `cd . & del *` 都会借白名单前缀放行后半段。记忆存的是完整命令，
        # 但仍先过这一关：已记住的命令下次带上元字符也重新确认。
        if re.search(r'[&|;<>`$()\n\r]', c):
            return False
        norm = re.sub(r'\s+', ' ', c)
        if norm in self.session_allow:           # 本次运行已记住的完整命令
            return True
        head = re.split(r'&&|\|\||;', c, maxsplit=1)[0].strip().lower()
        return any(head == p or head.startswith(p + ' ') for p in _READONLY_CMDS)

    def remember_cmd(self, cmd: str):
        """用户本次批准后记住完整命令（危险命令不记，下次仍问）。

        只记完整命令（空白折叠后比对）：记首词会让批过 `python check.py`
        后同首词的任意命令都自动放行。"""
        c = re.sub(r'\s+', ' ', (cmd or '').strip())
        if not c or _is_dangerous(c):
            return
        if c not in self.session_allow:
            self.session_allow.append(c)

    def _add(self, name, desc, params, fn):
        self.tools[name] = Tool(name, desc, params, fn)

    def schemas(self):
        return [t.schema() for t in self.tools.values()]

    def get(self, name):
        return self.tools.get(name)

    # ── 工具定义 ──────────────────────────────────────────────────
    def _register_all(self):
        ws = self.ws
        mem = self.memory

        def _managed_existing(rel: str):
            """解析工作台内路径（含 outputs 会话子目录别名），必须存在。"""
            rel = str(rel).replace('\\', '/').strip()
            ctx = ws._ctx
            if ctx and rel in ctx.aliases:
                rel = ctx.aliases[rel]
            return ws.resolve_managed(rel, must_exist=True)

        # ── 文件 ────────────────────────────────────────────────
        def read_file(a):
            """读取文本文件，支持 offset/limit 分段（大文件/长 SQL 读不全的问题）。"""
            p = ws.resolve_read(a['path'])
            max_mb = int(a.get('max_mb', 10))
            if p.stat().st_size > max_mb * 1024 * 1024:
                return ToolResult(f"文件过大(>{max_mb}MB): {p.name}，请用别的工具或分段读取")
            text, _enc = _read_text_best_effort(p)
            try:
                offset = max(0, int(a.get('offset', 0) or 0))
            except (TypeError, ValueError):
                offset = 0
            try:
                limit = int(a.get('limit', 0) or 0)
            except (TypeError, ValueError):
                limit = 0
            if limit <= 0:
                limit = 8000  # 单次默认上限（与 core.TOOL_RESULT_MAX 对齐，避免二次截断）
            if offset > len(text):
                return ToolResult(f"── {p} ──\n（offset={offset} 超出文件长度 {len(text)}，已到文件末尾）")
            chunk = text[offset:offset + limit]
            head = f"── {p} ──\n" if offset == 0 else f"── {p}（从第 {offset} 字符起）──\n"
            if offset + len(chunk) < len(text):
                chunk += (f"\n\n…(文件共 {len(text)} 字符，本段显示第 {offset}-{offset + len(chunk)} 字符；"
                          f"如需继续请用 offset={offset + len(chunk)} 再次调用 read_file)")
            return ToolResult(head + chunk)

        def write_file(a):
            real, rel = ws.resolve_write(a['path'])
            real.write_text(a['content'], encoding='utf-8')
            return ToolResult(f"✅ 已写入 {rel}（{real.stat().st_size} 字节）", artifacts=[rel])

        def edit_file(a):
            real, rel = ws.resolve_write(a['path'])
            text, enc = _read_text_best_effort(real)
            old, new = a.get('old', ''), a.get('new', '')
            if not old:
                raise ValueError("edit_file 必须提供 old 片段")
            n = text.count(old)
            if n == 0:
                # 给模型一个可修复的线索：最相近的几行（小模型给不准 old 是常态）
                near = _closest_snippets(text, old)
                hint = ("\n最相近的片段（请从里面复制精确原文作为 old）：\n"
                        + "\n---\n".join(near)) if near else ''
                raise ValueError(f"在 {rel} 中找不到要替换的文本（注意大小写和空格）。{hint}")
            if n > 1 and not a.get('all'):
                raise ValueError(f"待替换文本出现 {n} 次，请提供更精确的 old 片段，或传 all=true")
            text = text.replace(old, new) if a.get('all') else text.replace(old, new, 1)
            real.write_text(text, encoding=enc)
            return ToolResult(f"✅ 已编辑 {rel}（替换 {n} 处）", artifacts=[rel])

        def list_dir(a):
            rel = a.get('path', '.')
            try:
                p = (ws.root / rel).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
            except Exception:
                p = Path(rel)
            if not p.is_dir():
                raise NotADirectoryError(f"不是目录: {rel}")
            entries = []
            try:
                items = sorted(os.listdir(p), key=lambda x: (not (p / x).is_dir(), x.lower()))
            except PermissionError:
                raise PermissionError(f"无权限读取目录: {p}")
            # 敏感文件连名字也不列出（与 resolve_read 的拦截口径一致）。
            items = [x for x in items if not ws.is_sensitive(p / x)]
            for name in items[:200]:
                fp = p / name
                tag = '📁' if fp.is_dir() else '  '
                try:
                    size = fp.stat().st_size if fp.is_file() else 0
                except Exception:
                    size = 0
                entries.append(f"{tag} {name}" + (f"  ({size}B)" if size else ""))
            more = f"\n… 共 {len(items)} 项，仅显示前 200" if len(items) >= 200 else ""
            return ToolResult(f"目录 {p}:\n" + "\n".join(entries) + more)

        def glob_search(a):
            pat = a['pattern']
            base = a.get('path', '.')
            base_p = (ws.root / base).resolve() if not os.path.isabs(base) else Path(base).resolve()
            hits = []
            for root, dirs, files in os.walk(base_p):
                dirs[:] = [d for d in dirs if not d.startswith('.')
                           and d not in ('node_modules', '__pycache__')]
                for f in files:
                    full = os.path.join(root, f)
                    # 敏感文件不进入结果（只返回路径名也是泄露存在性）。
                    if ws.is_sensitive(full):
                        continue
                    if re.search(pat, f, re.IGNORECASE):
                        hits.append(full)
                if len(hits) >= 100:
                    break
            if not hits:
                return ToolResult(f"未找到匹配 {pat} 的文件")
            return ToolResult(f"匹配 {pat} 的文件:\n" + "\n".join(hits[:100]))

        def grep_search(a):
            pat = a['pattern']
            base = a.get('path', '.')
            base_p = (ws.root / base).resolve() if not os.path.isabs(base) else Path(base).resolve()
            out = []
            for root, dirs, files in os.walk(base_p):
                dirs[:] = [d for d in dirs if not d.startswith('.')
                           and d not in ('node_modules', '__pycache__')]
                for f in files:
                    if f.endswith(('.pyc', '.db', '.png', '.jpg', '.xlsx', '.docx')):
                        continue
                    full = os.path.join(root, f)
                    # 敏感文件与符号链接一律不打开（与 resolve_read 的拦截口径一致）：
                    # 否则一句 grep 就能读出任意目录 .env 里的 key。
                    if ws.is_sensitive(full) or os.path.islink(full):
                        continue
                    try:
                        with open(full, encoding='utf-8', errors='replace') as fh:
                            for i, line in enumerate(fh, 1):
                                if re.search(pat, line, re.IGNORECASE):
                                    out.append(f"{full}:{i}: {line.rstrip()[:150]}")
                                    if len(out) >= 100:
                                        return ToolResult("\n".join(out))
                    except Exception:
                        continue
            return ToolResult("\n".join(out) if out else f"未找到匹配 {pat} 的内容")

        def delete_file(a):
            real, rel = _managed_existing(a['path'])
            ws._snap_record(real, rel)   # 删前快照：undo_turn 可还原文件、重建空目录
            if real.is_dir():
                if any(real.iterdir()):
                    raise ValueError(f"目录非空，请先删除其中文件: {rel}")
                real.rmdir()
                return ToolResult(f"✅ 已删除空目录 {rel}")
            real.unlink()
            return ToolResult(f"✅ 已删除文件 {rel}")

        def move_file(a):
            src, src_rel = _managed_existing(a['path'])
            dst, dst_rel = ws.resolve_managed(a['to'])
            if dst.exists():
                raise ValueError(f"目标已存在，请换一个名字: {dst_rel}")
            if src.is_file():
                # 文件移动：源快照（undo 还原）+ 目标“新增”记录（undo 删掉它）；
                # 目录移动不进快照（整目录回滚超出本快照机制范围）。
                ws._snap_record(src, src_rel)
                ws._snap_record(dst, dst_rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                import shutil
                shutil.move(str(src), str(dst))
            else:
                src.replace(dst)
            return ToolResult(f"✅ 已移动 {src_rel} → {dst_rel}", artifacts=[dst_rel])

        def mkdir(a):
            real, rel = ws.resolve_managed(a['path'])
            real.mkdir(parents=True, exist_ok=True)
            return ToolResult(f"✅ 已创建目录 {rel}")

        # ── 代码执行 ────────────────────────────────────────────
        def run_command(a):
            cmd = a['command']
            # 危险命令：核心层已确认过（pre_approved）则不重复弹窗，否则再拦一次
            if _is_dangerous(cmd) and cmd not in self.pre_approved:
                if self.confirm_callback is None:
                    return ToolResult(f"⛔ 危险命令被拒绝（未配置确认）：{cmd}")
                ok = self.confirm_callback(cmd)
                if not ok:
                    return ToolResult(f"⛔ 用户拒绝执行：{cmd}")
            try:
                timeout = min(max(int(a.get('timeout', 120)), 1), 900)
            except (TypeError, ValueError):
                timeout = 120
            kwargs = {}
            if os.name == 'nt':
                kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
            t0 = time.monotonic()
            try:
                proc = subprocess.Popen(cmd, shell=True, cwd=str(ws.root),
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        **kwargs)
            except Exception as e:
                return ToolResult(f"❌ 无法启动命令: {e}")
            timed_out = False
            try:
                out_b, err_b = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_tree(proc)
                try:
                    out_b, err_b = proc.communicate(timeout=5)
                except Exception:
                    out_b, err_b = b'', b''
            out = _decode(out_b)
            err = _decode(err_b)
            body = out + (("\n[stderr]\n" + err) if err.strip() else '')
            body = body.strip() or "(无输出)"
            if timed_out:
                return ToolResult(f"⏱️ 命令执行超时（>{timeout}s）已终止：{cmd}")
            # 长输出截断（避免一次命令把上下文打爆）
            if len(body) > 8000:
                body = body[:8000] + f"\n…(输出共 {len(body)} 字符，已截断)"
            if proc.returncode != 0:
                # 非零退出必须带 ❌ 前缀：core._exec_one 只看此前缀定 ok，
                # 否则连续失败计数被清零、熔断永不触发。
                return ToolResult(f"❌ $ {cmd}\n(exit {proc.returncode})\n"
                                  f"…(耗时 {time.monotonic() - t0:.1f}s)\n{body}")
            return ToolResult(f"$ {cmd}\n(exit {proc.returncode})\n"
                              f"…(耗时 {time.monotonic() - t0:.1f}s)\n{body}")

        def execute_python(a):
            """子进程隔离执行：超时/崩溃不影响服务器进程。

            - 强制 timeout（默认 60s，死循环会被杀掉而不是卡死服务器）
            - cwd 限工作台根目录：相对路径写入留在工作台内
            - 输出捕获后返回；异常/退出码非零会明确提示
            - 审批：不走 shell 命令白名单（见 core._approve_tool），除 trusted
              档外每次人工确认；_is_dangerous 正则不对代码文本做安全承诺
            """
            code = a['code']
            try:
                timeout = min(max(int(a.get('timeout', 60)), 1), 300)
            except (TypeError, ValueError):
                timeout = 60
            # 子进程包装器：重定向 stdout/stderr 到缓冲，最后统一输出（含 traceback）
            wrapper = (
                "import io,sys,traceback\n"
                "buf = io.StringIO()\n"
                "old = sys.stdout, sys.stderr\n"
                "sys.stdout = sys.stderr = buf\n"
                "try:\n"
                "    exec(compile(sys.stdin.read(), '<python>', 'exec'), {'__name__': '__main__'})\n"
                "except BaseException as e:\n"
                "    buf.write('\\n❌ %s: %s\\n%s' % (type(e).__name__, e, traceback.format_exc(limit=3)))\n"
                "finally:\n"
                "    sys.stdout, sys.stderr = old\n"
                "sys.stdout.write(buf.getvalue())\n"
            )
            kwargs = {}
            if os.name == 'nt':
                kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW：不闪黑窗
            try:
                proc = subprocess.Popen(
                    [sys.executable, '-c', wrapper],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                    cwd=str(self.ws.root), encoding='utf-8', errors='replace',
                    **kwargs)
            except Exception as e:
                return ToolResult(f"❌ 无法启动子进程: {e}")
            try:
                out, err = proc.communicate(input=code, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                return ToolResult(f"⏱️ 代码执行超时（>{timeout}s）已强制终止。请检查是否有死循环。")
            out = (out or '') + ((f"\n[stderr]\n{err}") if err else '')
            out = out.strip()
            if proc.returncode != 0 and not out:
                out = f"❌ (进程异常退出，exit {proc.returncode})"
            return ToolResult(out or "(无输出)")

        # ── 记忆 ────────────────────────────────────────────────
        def memory_save(a):
            mem.save_fact(a['content'], source='user')
            return ToolResult(f"✅ 已记住：{a['content'][:100]}")

        def memory_recall(a):
            rows = mem.recall(a.get('query', ''), limit=int(a.get('limit', 8)))
            if not rows:
                return ToolResult("（记忆库中没有相关内容）")
            return ToolResult("相关记忆：\n" + "\n".join(f"- {r['content']}" for r in rows))

        # ── 办公文档（可选依赖）──────────────────────────────────
        try:
            import docx  # noqa: F401
            HAS_DOCX = True
        except ImportError:
            HAS_DOCX = False
        try:
            import openpyxl  # noqa: F401
            HAS_XLSX = True
        except ImportError:
            HAS_XLSX = False
        try:
            from pptx import Presentation  # noqa: F401
            HAS_PPTX = True
        except ImportError:
            HAS_PPTX = False

        def read_word(a):
            if not HAS_DOCX:
                return ToolResult("❌ 缺少 python-docx 库，无法读 Word（pip install python-docx）")
            import docx
            p = ws.resolve_read(a['path'])
            d = docx.Document(str(p))
            paras = [x.text for x in d.paragraphs if x.text.strip()]
            # 表格也读出来（办公文档大量内容在表里）
            for ti, t in enumerate(d.tables, 1):
                paras.append(f"[表格{ti}]")
                for row in t.rows:
                    paras.append(" | ".join(c.text.strip() for c in row.cells))
            return ToolResult(f"── {p} ──\n" + "\n".join(paras))

        def write_word(a):
            if not HAS_DOCX:
                return ToolResult("❌ 缺少 python-docx 库，无法生成 Word")
            import docx
            from docx.shared import Pt
            real, rel = ws.resolve_write(a['path'])
            d = docx.Document()
            title = (a.get('title') or '').strip()
            if title:
                d.add_heading(title, level=0)
            blocks = a.get('blocks') or []
            if blocks:
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    t = b.get('type') or 'para'
                    text = str(b.get('text') or '')
                    if t == 'heading':
                        lvl = min(max(int(b.get('level', 1) or 1), 1), 4)
                        d.add_heading(text, level=lvl)
                    elif t == 'bullet':
                        d.add_paragraph(text, style='List Bullet')
                    elif t == 'number':
                        d.add_paragraph(text, style='List Number')
                    elif t == 'quote':
                        p = d.add_paragraph(text)
                        names = [s.name for s in d.styles]
                        for cand in ('Intense Quote', 'Quote'):
                            if cand in names:
                                p.style = d.styles[cand]
                                break
                    elif t == 'table':
                        rows = b.get('rows') or []
                        if rows:
                            cols = max(len(r) for r in rows)
                            tb = d.add_table(rows=len(rows), cols=cols)
                            tb.style = 'Table Grid'
                            for ri, row in enumerate(rows):
                                for ci in range(cols):
                                    val = row[ci] if ci < len(row) else ''
                                    cell = tb.cell(ri, ci)
                                    cell.text = '' if val is None else str(val)
                                    if ri == 0 and b.get('header', True):
                                        for run in cell.paragraphs[0].runs:
                                            run.bold = True
                    elif t == 'code':
                        p = d.add_paragraph()
                        run = p.add_run(text)
                        run.font.name = 'Consolas'
                        run.font.size = Pt(9.5)
                    else:
                        d.add_paragraph(text)
            else:
                # 兼容旧参数：content 每行一段
                for line in (a.get('content') or '').split('\n'):
                    if line.startswith('#'):
                        lvl = min(len(line) - len(line.lstrip('#')), 4)
                        d.add_heading(line.lstrip('#').strip(), level=lvl)
                    else:
                        d.add_paragraph(line)
            d.save(str(real))
            return ToolResult(f"✅ 已生成 Word：{rel}", artifacts=[rel])

        def read_excel(a):
            if not HAS_XLSX:
                return ToolResult("❌ 缺少 openpyxl 库，无法读 Excel")
            import openpyxl
            p = ws.resolve_read(a['path'])
            wb = openpyxl.load_workbook(str(p), data_only=True, read_only=True)
            names = wb.sheetnames
            want = a.get('sheet')
            if want:
                names = [n for n in names if n == want] or names[:1]
            else:
                names = names[:3]
            try:
                offset = max(0, int(a.get('offset', 0) or 0))
            except (TypeError, ValueError):
                offset = 0
            try:
                max_rows = min(max(int(a.get('max_rows', 200) or 200), 1), 2000)
            except (TypeError, ValueError):
                max_rows = 200
            out = []
            total_rows = 0
            try:
                for sheet in names:
                    sx = wb[sheet]
                    n_rows = sx.max_row or 0
                    total_rows += n_rows
                    out.append(f"── 工作表: {sheet} ({n_rows} 行 x {sx.max_column} 列) ──")
                    # 流式窗口：多读一行只为探知“还有没有”，整表不再物化。
                    shown = 0
                    has_more = False
                    for row in itertools.islice(sx.iter_rows(values_only=True),
                                                offset, offset + max_rows + 1):
                        if shown >= max_rows:
                            has_more = True
                            break
                        vals = ["" if v is None else str(v) for v in row]
                        if any(vals):
                            out.append(" | ".join(vals))
                        shown += 1
                    if has_more:
                        rest = n_rows - offset - max_rows if n_rows > offset + max_rows else None
                        if rest is not None:
                            out.append(f"…(还有 {rest} 行，用 offset={offset + max_rows} 继续读)")
                        else:
                            out.append(f"…(还有更多行，用 offset={offset + max_rows} 继续读)")
            finally:
                wb.close()
            return ToolResult("\n".join(out))

        def write_excel(a):
            if not HAS_XLSX:
                return ToolResult("❌ 缺少 openpyxl 库，无法生成 Excel")
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
            real, rel = ws.resolve_write(a['path'])
            wb = openpyxl.Workbook()
            wsx = wb.active
            wsx.title = a.get('sheet', 'Sheet1')
            rows = a.get('rows', [])
            for r_i, row in enumerate(rows, 1):
                for c_i, v in enumerate(row, 1):
                    wsx.cell(row=r_i, column=c_i, value=v)
            # 表头加粗 + 冻结 + 自适应列宽（比裸数据可读得多）
            if rows:
                head_fill = PatternFill('solid', fgColor='EDF1FF')
                for c_i in range(1, len(rows[0]) + 1):
                    cell = wsx.cell(row=1, column=c_i)
                    cell.font = Font(bold=True)
                    cell.fill = head_fill
                    cell.alignment = Alignment(vertical='center')
                wsx.freeze_panes = 'A2'
                for c_i in range(1, len(rows[0]) + 1):
                    width = 8
                    for r_i in range(1, min(len(rows), 200) + 1):
                        v = wsx.cell(row=r_i, column=c_i).value
                        if v is not None:
                            width = max(width, min(len(str(v)) * 1.6 + 2, 48))
                    wsx.column_dimensions[wsx.cell(row=1, column=c_i).column_letter].width = width
            wb.save(str(real))
            return ToolResult(f"✅ 已生成 Excel：{rel}（{len(rows)} 行）", artifacts=[rel])

        def read_ppt(a):
            if not HAS_PPTX:
                return ToolResult("❌ 缺少 python-pptx 库，无法读 PPT")
            from pptx import Presentation
            p = ws.resolve_read(a['path'])
            prs = Presentation(str(p))
            out = []
            for i, slide in enumerate(prs.slides, 1):
                texts = [sh.text for sh in slide.shapes if hasattr(sh, 'text') and sh.text.strip()]
                if texts:
                    out.append(f"── 第 {i} 页 ──\n" + "\n".join(texts))
            return ToolResult("\n".join(out) or "PPT 无可提取文本")

        def write_ppt(a):
            """生成 PPT：标题页 + 要点页（python-pptx 可选依赖）。"""
            if not HAS_PPTX:
                return ToolResult("❌ 缺少 python-pptx 库，无法生成 PPT（pip install python-pptx）")
            from pptx import Presentation
            from pptx.util import Pt
            real, rel = ws.resolve_write(a['path'])
            slides = a.get('slides') or []
            if not slides:
                return ToolResult("❌ 至少需要一页内容（slides 数组）")
            prs = Presentation()
            # 16:9
            prs.slide_width = prs.slide_width
            title_text = (a.get('title') or '').strip()
            if title_text:
                s = prs.slides.add_slide(prs.slide_layouts[0])
                s.shapes.title.text = title_text
                sub = (a.get('subtitle') or '').strip()
                if sub and len(s.placeholders) > 1:
                    s.placeholders[1].text = sub
            for item in slides[:40]:
                if not isinstance(item, dict):
                    continue
                st = str(item.get('title') or '').strip()
                bullets = [str(b).strip() for b in (item.get('bullets') or []) if str(b).strip()]
                s = prs.slides.add_slide(prs.slide_layouts[1])
                s.shapes.title.text = st or '要点'
                body = s.placeholders[1].text_frame
                body.clear()
                for i, b in enumerate(bullets[:12]):
                    p = body.paragraphs[0] if i == 0 else body.add_paragraph()
                    p.text = b
                    p.level = 0
                    for run in p.runs:
                        run.font.size = Pt(18)
                notes = str(item.get('notes') or '').strip()
                if notes:
                    s.notes_slide.notes_text_frame.text = notes
            prs.save(str(real))
            pages = sum(1 for _ in prs.slides)
            return ToolResult(f"✅ 已生成 PPT：{rel}（{pages} 页）", artifacts=[rel])

        def read_image(a):
            """把图片交给多模态模型（需要模型支持图片输入）。"""
            p = ws.resolve_read(a['path'])
            ext = p.suffix.lower().lstrip('.')
            if ext not in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'):
                return ToolResult(f"❌ 不是支持的图片格式: {ext}")
            size = p.stat().st_size
            if size > 8 * 1024 * 1024:
                return ToolResult("❌ 图片过大（>8MB），请先压缩")
            if not self.vision:
                return ToolResult(
                    f"⚠️ 当前模型未开启图片理解（设置页「模型支持图片输入」）。"
                    f"图片路径: {p}，大小 {size} 字节。请改为让用户描述图片内容，"
                    f"或用 execute_python + Pillow 做基础处理（尺寸/格式）。")
            return ToolResult(f"✅ 已读取图片 {p.name}（{size} 字节），图像内容见随后的图片消息。",
                              image=str(p))

        # ── Agent 协作工具（todo / ask_user / 计划）───────────────
        def todo(a):
            items = a.get('items') or []
            norm = []
            for it in items:
                if isinstance(it, dict):
                    t = str(it.get('text') or '').strip()
                    s = str(it.get('status') or 'pending').lower()
                else:
                    t, s = str(it).strip(), 'pending'
                if not t:
                    continue
                if s not in ('pending', 'doing', 'done'):
                    s = 'pending'
                norm.append({"text": t[:200], "status": s})
            if not norm:
                return ToolResult("❌ items 不能为空")
            upd = getattr(self._hooks, 'update_todos', None)
            if upd:
                upd(norm)
            mark = {'done': '[x]', 'doing': '[~]', 'pending': '[ ]'}
            done = sum(1 for i in norm if i['status'] == 'done')
            lines = [f"📋 任务清单（{done}/{len(norm)} 完成）"]
            lines += [f"{mark[i['status']]} {i['text']}" for i in norm]
            return ToolResult("\n".join(lines))

        def ask_user(a):
            q = str(a.get('question') or '').strip()
            if not q:
                return ToolResult("❌ 缺少 question")
            opts = [str(o)[:80] for o in (a.get('options') or []) if str(o).strip()][:6]
            cb = getattr(self._hooks, 'on_ask', None)
            if not cb:
                return ToolResult("⚠️ 当前无法向用户提问，请自行按最合理的方式处理并说明假设。")
            ans = cb(q, opts)
            if not ans:
                return ToolResult("（用户未回答，请按最合理的方式继续，并在结论里说明你的假设）")
            return ToolResult(f"用户回答：{ans}")

        def plan(a):
            """兼容旧调用：把计划写成待办清单。"""
            goal = a.get('goal', '')
            steps = a.get('steps', [])
            items = []
            for s in steps:
                if isinstance(s, dict):
                    txt = str(s.get('action') or '').strip()
                else:
                    txt = str(s).strip()
                if txt:
                    items.append({"text": txt, "status": "pending"})
            upd = getattr(self._hooks, 'update_todos', None)
            if upd and items:
                upd(items)
            lines = [f"📋 计划已记录，目标: {goal}"]
            for i, it in enumerate(items, 1):
                lines.append(f"  {i}. [ ] {it['text']}")
            if a.get('exit_condition'):
                lines.append(f"退出条件: {a['exit_condition']}")
            lines.append("（执行过程中请用 todo 工具更新每步状态）")
            return ToolResult("\n".join(lines))

        def final_answer(a):
            """模型调用此工具表示任务已完成，输出最终答案。"""
            answer = a.get('answer', '')
            summary = a.get('summary', '')
            parts = ["✅ 任务完成"]
            if summary:
                parts.append(f"摘要: {summary}")
            if answer:
                parts.append(f"\n{answer}")
            arts = []
            for p in (a.get('artifacts') or []):
                m = ws.artifact_meta(str(p))
                if m and m.get('rel') not in arts:
                    arts.append(m['rel'])
            if arts:
                parts.append(f"\n产出物: {', '.join(arts)}")
            return ToolResult("\n".join(parts), artifacts=arts)

        def delegate_task(a):
            """将子任务委托给独立的子Agent执行，适用于需要独立上下文的任务。"""
            task = a.get('task', '')
            if not task:
                return ToolResult("❌ 缺少 task 参数")
            llm_ref = getattr(self, '_llm', None)
            if not llm_ref:
                return ToolResult("❌ 无法创建子Agent（LLM未初始化）")
            try:
                from .core import Agent as SubAgent
                sub = SubAgent(
                    ws=self.ws, llm=llm_ref,
                    memory=self.memory, rag=None,
                    allow_code_exec=False,
                    # 继承主 Agent 的审批档位与确认通道（子 Agent 不能绕过审批）
                    confirm_callback=self.confirm_callback,
                    profile=self.profile, auto_approve=self.auto_approve,
                    session_allow=self.session_allow,
                    role_text="你是一个子任务执行器，专注于完成委托的具体任务。",
                    allow_delegate=False,        # 子 Agent 不能再委托（防无限递归）
                    ctx=self.ws._ctx,            # 继承同一会话上下文（产物路径一致）
                )
                sub._is_sub_agent = True
            except Exception as e:
                return ToolResult(f"❌ 无法创建子Agent（依赖未初始化）: {e}")
            context = a.get('context', '')
            prompt = (f"任务: {task}\n") + (f"上下文: {context}\n" if context else "")
            try:
                # 父级只读时子 Agent 必须同样只读：mode 写死 'craft' 会让子级
                # 拿到完整工具表，suggest 档的写文件自动放行直接绕过只读承诺。
                # _hooks 即父级 Agent（ToolRegistry 由它构造），_read_only 由当
                # 前 _run_loop 在工具执行前算好；独立构造的 registry 取不到则默认 craft。
                parent_ro = bool(getattr(getattr(self, '_hooks', None), '_read_only', False))
                result = sub.run([], prompt, mode='ask' if parent_ro else 'craft',
                                 max_turns=8)
                resp = result.get('response', '') or ''
                arts = result.get('artifacts', [])
                return ToolResult(f"子任务结果:\n{resp}", artifacts=arts)
            except Exception as e:
                return ToolResult(f"❌ 子任务执行失败: {e}")

        # ── 注册 ────────────────────────────────────────────────
        self._add("read_file",
                  "读取文本/代码文件内容（可读任意目录；敏感文件如 .env/.key 会被拦截）。"
                  "大文件支持分段：offset=起始字符（默认 0）、limit=本段最大字符数（默认 8000），"
                  "返回末尾会提示剩余长度和续读的 offset",
                  {"type": "object", "properties": {
                      "path": {"type": "string", "description": "文件路径（工作台内相对路径或任意绝对路径）"},
                      "max_mb": {"type": "integer", "description": "最大读取 MB，默认 10"},
                      "offset": {"type": "integer", "description": "从第几个字符开始读，默认 0（续读用）"},
                      "limit": {"type": "integer", "description": "本段最大读取字符数，默认 8000"}},
                   "required": ["path"]}, read_file)

        self._add("write_file",
                  "写入/新建文件。产出物统一落到 outputs/（自动按日期会话分类），写 notes/ 存知识库。禁止写系统/输入目录",
                  {"type": "object", "properties": {
                      "path": {"type": "string", "description": "目标路径，如 outputs/report.md 或 notes/想法.md 或裸文件名 report.md"},
                      "content": {"type": "string"}},
                   "required": ["path", "content"]}, write_file)

        self._add("edit_file",
                  "精确编辑已存在文件（文本替换）。会保留其余内容；产出物登记到会话。"
                  "old 必须与文件里的原文完全一致（含空格缩进）；找不到时会返回最相近的片段供修正",
                  {"type": "object", "properties": {
                      "path": {"type": "string"},
                      "old": {"type": "string", "description": "被替换的原文片段（唯一）"},
                      "new": {"type": "string"},
                      "all": {"type": "boolean", "description": "true=替换全部出现处"}},
                   "required": ["path", "old", "new"]}, edit_file)

        self._add("list_dir",
                  "列出目录内容（默认工作台根；可指定任意目录，只读）",
                  {"type": "object", "properties": {
                      "path": {"type": "string", "description": "目录路径，默认 ."}},
                   "required": []}, list_dir)

        self._add("glob_search",
                  "按文件名正则搜索文件",
                  {"type": "object", "properties": {
                      "pattern": {"type": "string", "description": "文件名正则，如 .*\\.md$"},
                      "path": {"type": "string", "description": "搜索起始目录，默认工作台"}},
                   "required": ["pattern"]}, glob_search)

        self._add("grep_search",
                  "在文本文件中按正则搜索内容",
                  {"type": "object", "properties": {
                      "pattern": {"type": "string"},
                      "path": {"type": "string", "description": "搜索起始目录，默认工作台"}},
                   "required": ["pattern"]}, grep_search)

        self._add("delete_file",
                  "删除 outputs/ 或 notes/ 下的文件（目录必须为空）。不可恢复，谨慎使用",
                  {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}, delete_file)

        self._add("move_file",
                  "移动/重命名 outputs/ 或 notes/ 下的文件",
                  {"type": "object", "properties": {
                      "path": {"type": "string", "description": "源路径"},
                      "to": {"type": "string", "description": "目标路径（同在白名单内）"}},
                   "required": ["path", "to"]}, move_file)

        self._add("mkdir",
                  "在 outputs/ 或 notes/ 下新建目录",
                  {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}, mkdir)

        if self.allow_code_exec:
            self._add("run_command",
                      "执行系统命令（shell，cwd=工作台根目录）。危险命令（删除/格式化/改注册表等）需用户确认",
                      {"type": "object", "properties": {
                          "command": {"type": "string"},
                          "timeout": {"type": "integer", "description": "超时秒数，默认 120，最大 900"}},
                       "required": ["command"]}, run_command)

            self._add("execute_python",
                      "执行 Python 代码片段（输出即返回）。在独立子进程运行，cwd=工作台根目录，"
                      "超时自动终止（默认 60s），死循环不会影响服务",
                      {"type": "object", "properties": {
                          "code": {"type": "string"},
                          "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 300"}},
                       "required": ["code"]}, execute_python)

        self._add("memory_save",
                  "把用户明确告知的重要信息/偏好存入长期记忆（之后对话会自动 recall）",
                  {"type": "object", "properties": {
                      "content": {"type": "string", "description": "要记住的事实，一句话"}},
                   "required": ["content"]}, memory_save)

        self._add("memory_recall",
                  "查询长期记忆中与关键词相关的内容",
                  {"type": "object", "properties": {
                      "query": {"type": "string"},
                      "limit": {"type": "integer", "description": "最多返回条数，默认 8"}},
                   "required": ["query"]}, memory_recall)

        if HAS_DOCX:
            self._add("read_word", "读取 Word 文档（正文段落 + 表格）",
                      {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}, read_word)
            self._add("write_word",
                      "生成 Word 文档。推荐用 blocks 结构化输出（heading/para/bullet/number/table/code），"
                      "排版比纯文本好得多；也兼容 content（每行一段，# 开头为标题）",
                      {"type": "object", "properties": {
                          "path": {"type": "string"},
                          "title": {"type": "string", "description": "文档大标题（可选）"},
                          "content": {"type": "string", "description": "纯文本正文（每行一段），与 blocks 二选一"},
                          "blocks": {"type": "array", "description": "结构化内容块",
                                     "items": {"type": "object", "properties": {
                                         "type": {"type": "string",
                                                  "description": "heading|para|bullet|number|table|code|quote"},
                                         "level": {"type": "integer"},
                                         "text": {"type": "string"},
                                         "rows": {"type": "array", "description": "table 用：二维数组"}}}}},
                       "required": ["path"]}, write_word)
        if HAS_XLSX:
            self._add("read_excel",
                      "读取 Excel 表格（默认前 3 个工作表、每表 200 行，可用 sheet/offset/max_rows 分页）",
                      {"type": "object", "properties": {
                          "path": {"type": "string"},
                          "sheet": {"type": "string", "description": "只读某个工作表"},
                          "offset": {"type": "integer", "description": "跳过前 N 行"},
                          "max_rows": {"type": "integer", "description": "本页最多行数，默认 200"}},
                       "required": ["path"]}, read_excel)
            self._add("write_excel", "生成 Excel 表格（二维数组 rows，自动加表头样式/冻结/列宽）",
                      {"type": "object", "properties": {
                          "path": {"type": "string"},
                          "sheet": {"type": "string", "description": "工作表名，默认 Sheet1"},
                          "rows": {"type": "array", "description": "二维数组，每行是值列表"}},
                       "required": ["path", "rows"]}, write_excel)
        if HAS_PPTX:
            self._add("read_ppt", "提取 PPT 每页文本",
                      {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}, read_ppt)
            self._add("write_ppt",
                      "生成 PPT（标题页 + 要点页）。slides 每项 {title, bullets:[...], notes}",
                      {"type": "object", "properties": {
                          "path": {"type": "string"},
                          "title": {"type": "string", "description": "封面标题"},
                          "subtitle": {"type": "string", "description": "封面副标题"},
                          "slides": {"type": "array", "items": {"type": "object", "properties": {
                              "title": {"type": "string"},
                              "bullets": {"type": "array", "items": {"type": "string"}},
                              "notes": {"type": "string", "description": "演讲者备注"}}}}},
                       "required": ["path", "slides"]}, write_ppt)

        self._add("read_image",
                  "读取图片内容（需要模型支持图片输入，在设置页开启）。"
                  "用于看截图/表格图片/设计稿。未开启时返回提示",
                  {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}, read_image)

        self._add("todo",
                  "维护任务清单（长任务必备）。每次传入完整清单，status: pending|doing|done。"
                  "用户界面会实时显示进度",
                  {"type": "object", "properties": {
                      "items": {"type": "array", "items": {"type": "object", "properties": {
                          "text": {"type": "string"},
                          "status": {"type": "string", "description": "pending|doing|done"}}}}},
                   "required": ["items"]}, todo)

        self._add("ask_user",
                  "向用户提问并等待回答（需求含糊、缺关键信息、需要在多个方案间选择时使用）。"
                  "不要用它确认显而易见的事",
                  {"type": "object", "properties": {
                      "question": {"type": "string"},
                      "options": {"type": "array", "items": {"type": "string"},
                                  "description": "可选答案（最多 6 个），用户也可自由输入"}},
                   "required": ["question"]}, ask_user)

        self._add("plan",
                  "制定任务计划（会转成待办清单展示给用户）。复杂任务开始时调用一次即可，"
                  "执行过程中用 todo 更新状态",
                  {"type": "object", "properties": {
                      "goal": {"type": "string", "description": "任务目标"},
                      "steps": {"type": "array", "description": "执行步骤列表",
                                "items": {"type": "object",
                                          "properties": {
                                              "action": {"type": "string"},
                                              "tool": {"type": "string", "description": "计划使用的工具名"},
                                              "expected": {"type": "string", "description": "预期结果"}}}},
                      "exit_condition": {"type": "string", "description": "什么时候算任务完成"}},
                   "required": ["goal", "steps"]}, plan)

        self._add("final_answer",
                  "输出最终答案并结束任务（可选）。所有步骤完成或问题已回答时调用",
                  {"type": "object", "properties": {
                      "answer": {"type": "string", "description": "最终答案"},
                      "summary": {"type": "string", "description": "任务摘要（可选）"},
                      "artifacts": {"type": "array", "description": "产出物路径列表",
                                    "items": {"type": "string"}}},
                   "required": ["answer"]}, final_answer)

        if self.allow_delegate:
            self._add("delegate_task",
                      "将子任务委托给独立的子Agent执行。适用于需要独立上下文、避免污染主对话的任务。"
                      "注意：子Agent看不到当前对话，必须在 task/context 里给足信息",
                      {"type": "object", "properties": {
                          "task": {"type": "string", "description": "子任务描述"},
                          "context": {"type": "string", "description": "需要传递的上下文"},
                          "timeout": {"type": "integer", "description": "超时秒数", "default": 120}},
                       "required": ["task"]}, delegate_task)

        # ── MCP 外部工具（B5）：注册进同一张表，默认逐条审批 ──
        self._register_mcp()

    def _register_mcp(self):
        """把已连接 MCP server 的工具注册成 mcp__<server>__<tool>。

        连接失败只影响对应 server，不影响本 Agent 的其它工具。
        """
        try:
            from .mcp import manager
            specs = manager.tool_specs()
        except Exception as e:
            log.warning('MCP 工具注册失败: %s', e)
            return
        for name, desc, params, fn in specs:
            if name in self.tools:
                continue
            if not isinstance(params, dict) or params.get('type') != 'object':
                params = {'type': 'object', 'properties': {}}
            self._add(name, desc, params, fn)


def _closest_snippets(text: str, needle: str, n: int = 3) -> list:
    """在文本里找与 needle 最相近的几段（edit_file 失败时给模型可修复的线索）。

    用"行窗口 + 字符交集比例"做轻量相似度，避免引入 difflib 之外的复杂度
    （difflib 是标准库，这里用 SequenceMatcher 更准）。
    """
    import difflib
    if not text or not needle:
        return []
    lines = text.splitlines()
    if not lines:
        return []
    target = needle.strip()
    best = []
    win = max(1, target.count('\n') + 1)
    for i in range(max(1, len(lines) - win + 1)):
        seg = '\n'.join(lines[i:i + win])
        ratio = difflib.SequenceMatcher(None, seg, target).ratio()
        if ratio > 0.45:
            best.append((ratio, seg))
    best.sort(key=lambda x: -x[0])
    return [s for _r, s in best[:n]]
