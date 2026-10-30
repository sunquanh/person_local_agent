# -*- coding: utf-8 -*-
"""HTTP 服务器：静态页面 + API + SSE 聊天流。

并发模型：
- 同一时刻只允许一个 agent 任务在跑（_busy 守卫，快速 409）
- SSE 聊天：agent 在子线程跑，通过回调 + 写锁推帧（delta/tool/confirm/result）
- 看门狗：chat 总时长超限强制 stop 并返回错误，避免 _busy 永久占用
"""
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import hmac
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent import logsetup
from agent.core import Agent, compact_history, estimate_messages
from agent.events import TURN_EVENTS
from agent.llm import LLMClient, LLMError
from agent.memory import Memory
from agent.workspace import Workspace
from server.batch import BatchRunner, valid_job_id
from server.sessions import SessionManager

log = logsetup.get('http')


def _split_cmd_args(s: str) -> list:
    """把“空格分隔的命令参数”拆成数组，支持双/单引号包住带空格的路径。"""
    out, buf, quote = [], '', ''
    for ch in str(s or ''):
        if quote:
            if ch == quote:
                quote = ''
            else:
                buf += ch
        elif ch in ('"', "'"):
            quote = ch
        elif ch.isspace():
            if buf:
                out.append(buf)
                buf = ''
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


WEB_DIR = Path(__file__).resolve().parent.parent / 'web'

# 脱敏占位符：status_dict / _redacted_config 发给前端的密钥类值统一用它
# 替换原文；前端回传该值时表示"未改动"，保存逻辑须保留旧值、不得加密存盘。
_ENV_MASKED = '已设置（脱敏）'

# 局域网访问的登录页（纯内联，无任何外部资源，离线可用）
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>工作台 · 需要访问令牌</title>
<style>
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e6e8ee;font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.card{width:min(92vw,420px);background:#171a21;border:1px solid #262b36;border-radius:14px;
padding:28px 26px;box-shadow:0 12px 40px rgba(0,0,0,.45)}
h1{margin:0 0 6px;font-size:19px;font-weight:600}
p{margin:0 0 18px;color:#8b93a7;font-size:13px}
input{width:100%;padding:11px 13px;border-radius:9px;border:1px solid #2e3542;
background:#0f1218;color:#e6e8ee;font-size:14px;outline:none}
input:focus{border-color:#4c7dff}
button{margin-top:12px;width:100%;padding:11px;border:0;border-radius:9px;cursor:pointer;
background:#4c7dff;color:#fff;font-size:14px;font-weight:600}
button:hover{background:#3f6ee8}
.tip{margin-top:16px;padding-top:14px;border-top:1px solid #242a35;color:#6f7789;font-size:12px;
line-height:1.7;word-break:break-all}
code{color:#9aa6c2;background:#0f1218;padding:1px 5px;border-radius:4px}
</style></head><body>
<div class="card">
  <h1>需要访问令牌</h1>
  <p>该工作台已开启局域网访问保护</p>
  <form method="get" action="/">
    <input name="t" type="password" placeholder="粘贴访问令牌" autofocus autocomplete="off">
    <button type="submit">进入工作台</button>
  </form>
  <div class="tip">令牌保存在工作台目录的 <code>.agent/access_token</code> 文件中，
  也可在启动窗口的横幅里看到。</div>
</div></body></html>"""



def _mcp_status() -> list:
    """MCP 连接状态（B5）：设置页显示，失败只标红不抛错。"""
    try:
        from agent.mcp import manager
        return manager.status()
    except Exception:
        return []


def _workspace_status(app) -> dict:
    """当前运行的工作台目录 vs config 里保存的目录（不同就得重启）。"""
    running = str(app.ws.root)
    configured = str((app.config.get('workspace') or {}).get('root') or running)
    try:
        same = os.path.normcase(os.path.abspath(configured)) == \
            os.path.normcase(os.path.abspath(running))
    except Exception:
        same = configured == running
    return {'root': running, 'configured': configured,
            'restart_required': not same}


class ConfirmBox:
    """一次请求内的「危险命令确认 / 向用户提问」通道。

    每个请求独立一个，避免旧版全局 _confirm_result 在超时后沿用上一次答案
    （上次点过"允许"，这次没人应答 -> 危险命令自动执行）。
    """

    def __init__(self, send, app):
        self.send = send
        self.app = app
        self.event = threading.Event()
        self.result = False
        self.answer = None
        self.kind = None
        self.alive = None      # 由请求处理器注入：客户端断开则立即返回，不空等
        self.owner = ''        # 发起该轮的浏览器标识（局域网：只认发起人的确认/回答）

    def confirm(self, cmd: str) -> bool:
        self.result = False          # 默认拒绝：超时/异常一律不执行
        self.event.clear()
        self.kind = 'confirm'
        self.app._pending_confirm = self
        try:
            self.send('confirm', command=cmd)
        except Exception:
            return False
        if not self.event.wait(timeout=180):
            log.warning('危险命令确认超时，按拒绝处理: %s', cmd[:120])
            return False
        return bool(self.result)

    def ask(self, question: str, options=None) -> str:
        self.answer = None
        self.event.clear()
        self.kind = 'ask'
        self.app._pending_confirm = self
        try:
            self.send('ask', question=question, options=list(options or []))
        except Exception:
            return ''
        # 180s 上限：超时/客户端断开都按"未回答"继续，绝不冻结整个应用
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.event.wait(timeout=1):
                return self.answer or ''
            if self.alive is not None:
                try:
                    if not self.alive():
                        log.warning('提问期间客户端断开，按未回答继续')
                        return ''
                except Exception:
                    pass
        log.warning('向用户提问超时（180s），按未回答继续')
        return ''

    def resolve(self, approved=None, answer=None):
        if answer is not None:
            self.answer = str(answer)
        if approved is not None:
            self.result = bool(approved)
        self.event.set()



def _validate_attachments(ws, rels):
    """附件白名单：只放行 inputs/ 下真实存在的普通文件（防越界/防系统目录/防敏感文件）。

    兼容旧数据：传入的可以是路径字符串或 {path,name,size} 对象；
    返回统一为 {path, name, size} 对象列表。
    """
    out = []
    for a in (rels or [])[:20]:
        if isinstance(a, dict):
            rel = str(a.get('path') or '')
            name = str(a.get('name') or '')
            size = a.get('size')
        else:
            rel = str(a)
            name = ''
            size = None
        rel = rel.replace('\\', '/').strip()
        if not rel.startswith('inputs/'):
            continue
        try:
            p = (ws.root / rel).resolve()
            p.relative_to(ws.root.resolve())
            if p.is_file() and not ws.is_sensitive(p):
                if not isinstance(size, int):
                    try:
                        size = p.stat().st_size
                    except Exception:
                        size = 0
                out.append({"path": rel, "name": name or p.name, "size": size})
        except Exception:
            continue
    return out


def _path_referenced_anywhere(app, rel: str) -> bool:
    """该路径是否仍被任何会话的附件引用（含当前会话其它消息）。"""
    for m in app.sessions.iter_all_messages():
        for a in (m.get('attachments') or []):
            key = a if isinstance(a, str) else (a or {}).get('path', '')
            if key == rel:
                return True
    return False


def _delete_attachment_ref(app, sid: str, ts: float, path: str):
    """移除会话消息里的附件引用（持久化）；若文件不再被任何会话引用则回收物理文件。

    返回 None 表示成功，否则返回错误信息字符串。
    """
    try:
        if not app.sessions._sdir(sid).is_dir():
            return '会话不存在'
    except ValueError:
        # _sdir 校验 sid 失败（'../..' 之类）：同样按“不存在”处理，不抛 500。
        return '会话不存在'
    try:
        s = app.sessions.load(sid)
    except Exception:
        return '会话不存在'

    def _key(a):
        return a if isinstance(a, str) else (a or {}).get('path', '')

    # 用户消息可能在数秒内连发（测试/快速操作），取 ts 最近的一条
    cands = [m for m in s.messages
             if m.get('role') == 'user' and abs((m.get('ts') or 0) - ts) <= 5]
    if not cands:
        return '消息不存在'
    m = min(cands, key=lambda x: abs((x.get('ts') or 0) - ts))
    atts = m.get('attachments') or []
    kept = [a for a in atts if _key(a) != path]
    if len(kept) == len(atts):
        return '该消息没有此附件'
    m['attachments'] = kept
    app.sessions._save(s)
    # 物理文件回收：仅删 inputs/ 内、且没有任何会话再引用的文件
    if not _path_referenced_anywhere(app, path):
        try:
            p = (app.ws.root / path).resolve()
            p.relative_to(app.ws.root.resolve())
            if p.is_file() and path.startswith('inputs/'):
                p.unlink()
        except Exception:
            pass
    return None


class AgentServer:
    def __init__(self, config: dict, config_path: str = None):
        self.config = config
        self.config_path = config_path
        m = config.get('model', {}) or {}
        w = config.get('workspace', {}) or {}
        limits = config.get('limits', {}) or {}
        self.max_upload_mb = float(limits.get('max_upload_mb', 50))
        self.chat_timeout_s = float(limits.get('chat_timeout_s', 600))

        self.ws = Workspace(w.get('root', os.path.expanduser('~/工作台')),
                            max_upload_mb=self.max_upload_mb)
        logsetup.setup(self.ws.root)
        self._start_ts = time.time()

        # ── API Key：内存存明文，磁盘永远只存 ENC: ─────────────────
        # 旧版把解密后的明文塞回 config，任何一次保存设置都会把它写进 config.json，
        # 加密形同虚设；这里彻底分开：_api_key_plain 只在内存里。
        self._api_key_plain = self._decrypt_key(m.get('api_key') or '')
        # 内存里的 config 一律只保留 ENC 形态（app.py 启动时会把明文塞进来）
        if m.get('api_key') and not str(m['api_key']).startswith('ENC:'):
            try:
                from encrypt_key import encrypt_key
                m['api_key'] = 'ENC:' + encrypt_key(str(m['api_key']))
            except Exception:
                m['api_key'] = ''
        self.llm = self._make_llm()

        # 角色提示词 + 铁律（自定义系统提示词，注入每轮 system）
        pr = config.get('prompt', {}) or {}
        self.agent_role = str(pr.get('role') or '').strip()
        self.agent_rules = [str(r).strip() for r in (pr.get('rules') or [])
                            if str(r).strip()]
        from agent.rag import Rag
        self.rag = Rag(w.get('root', '') or str(self.ws.root))
        self.memory = Memory(str(self.ws.root / '.agent' / 'memory.db'))
        self.sessions = SessionManager(str(self.ws.root / '.agent'))
        self.batch = BatchRunner(self)   # 批量作业（B8）：单 worker 串行执行
        # MCP 客户端（B5）：按 config.mcp.servers 拉起子进程（失败只影响对应 server）
        try:
            from agent.mcp import manager as _mcp
            _mcp.configure(self._servers_for_runtime(
                (config.get('mcp') or {}).get('servers') or []))
        except Exception as e:
            log.warning('MCP 初始化失败（不影响启动）: %s', e)

        self._busy = False
        self._busy_owner = None    # 持有 _busy 的请求凭证（防止误放别人的锁）
        self._busy_lock = threading.Lock()
        self._queue = []       # 排队中的 SSE 连接（Handler 实例，按序执行）
        self._jobs = []        # 最近完成的任务（内存，最多 20 条）
        self._running = None   # 当前正在跑的 Agent 实例（/api/stop 用）
        self._running_sid = ''   # 正在跑的会话 id（断流续看：刷新后告知前端“仍在运行”）
        self._running_owner = ''  # 发起该轮的浏览器标识（局域网多人，防止互相停/代确认）
        self._running_watch = None   # {deadline} 当前轮的看门狗（"再等 5 分钟"可延长）
        self._run_started = 0.0
        self._pending_confirm = None   # 当前等待用户回答的 ConfirmBox
        # 浏览器身份由服务端签名，不能信任前端自行写入的 wbclient cookie。
        # 密钥只驻留内存，重启后旧浏览器身份失效；运行任务本来也不会跨重启保留。
        self.client_cookie_key = secrets.token_bytes(32)
        # 审批档位（B4）：本次运行内用户批准过的命令前缀（内存态，不落盘，重启清空）
        self.session_allow = list((config.get('security', {}) or {}).get('session_allow') or [])
        self._current_sid = None
        self.access_token = self._init_token()

        # ── 关网页即退：页面登记表 + 空置倒计时 ────────────────────
        # 前端每个标签页打开时登记，关闭时用 sendBeacon 注销。集合空了再等
        # grace 秒才退，所以刷新页面、多标签、切到别的窗口都不会误杀。
        srv = config.get('server', {}) or {}
        self.auto_exit = bool(srv.get('auto_exit', True))
        try:
            self.auto_exit_grace_s = max(1.0, float(srv.get('auto_exit_grace_s', 5)))
        except (TypeError, ValueError):
            self.auto_exit_grace_s = 5.0
        self._pages = {}            # page_id -> 最后一次心跳时间
        self._pages_seen = False    # 出现过页面才启用，纯 API/CLI 用法不受影响
        self._pages_empty_at = 0.0
        self._exit_lock = threading.Lock()
        self._exiting = False
        self._httpd = None          # serve() 里回填

        log.info('工作台启动: root=%s model=%s', self.ws.root, m.get('name'))

    # ── 模型/密钥 ────────────────────────────────────────────────
    @staticmethod
    def _decrypt_key(raw: str) -> str:
        raw = (raw or '').strip()
        if not raw.startswith('ENC:'):
            return raw
        try:
            from encrypt_key import decrypt_key
            return decrypt_key(raw)
        except Exception as e:
            log.error('API Key 解密失败（密钥文件丢失/换了机器？）: %s', e)
            print(f"⚠️  API Key 解密失败（{e}）。"
                  f"密文依赖 ~/.local_agent_v2.key，换机器后需重新加密。")
            return ''

    # ── MCP 环境变量：与 api_key 同一套 ENC: 机制 ─────────────────
    # 磁盘 config 里只存 ENC: 密文；拉起子进程 / 返回设置页时才在内存里解密。
    # 旧版明文 env 兼容：非 ENC: 值原样保留，下次在设置页保存时自动转密文。
    @staticmethod
    def _encrypt_mcp_env(env: dict) -> dict:
        """落盘前加密。已是 ENC: 的值原样保留（防重复加密）；加密失败抛错，
        调用方拒绝保存，绝不让明文密钥进 config.json。"""
        from encrypt_key import encrypt_key
        out = {}
        for k, v in (env or {}).items():
            s = str(v)
            out[k] = s if s.startswith('ENC:') else 'ENC:' + encrypt_key(s)
        return out

    @staticmethod
    def _decrypt_mcp_env(env: dict) -> dict:
        """内存态解密（供拉起子进程用）。解密失败的值跳过并记日志，
        不把密文当成密钥传给子进程。"""
        out = {}
        for k, v in (env or {}).items():
            s = str(v)
            if s.startswith('ENC:'):
                try:
                    from encrypt_key import decrypt_key
                    s = decrypt_key(s)
                except Exception as e:
                    log.error('MCP 环境变量 %s 解密失败，已跳过: %s', k, e)
                    continue
            out[k] = s
        return out

    @classmethod
    def _servers_for_runtime(cls, servers) -> list:
        """交给 manager.configure 的 server 列表：env 已解密的新副本。

        config 里存的是 ENC: 形态，manager 拿到密文会原样传给子进程当密钥，
        所以 4 处 configure 调用点都要先过这一层；config 本身不动。
        """
        out = []
        for s in (servers or []):
            d = dict(s or {})
            if d.get('env'):
                d['env'] = cls._decrypt_mcp_env(d['env'])
            out.append(d)
        return out

    def _make_llm(self, model: str = None) -> LLMClient:
        m = self.config.get('model', {}) or {}
        limits = self.config.get('limits', {}) or {}
        return LLMClient(
            m.get('base_url', ''), self._api_key_plain,
            # 会话级临时换模型（B9）：只覆盖模型名，base_url/key/超时沿用全局配置
            str(model or '').strip() or m.get('name', 'qwen'),
            timeout=float(limits.get('llm_timeout_s', 300) or 300),
            thinking=m.get('thinking', 'collapse'),
            max_tokens=int(m.get('max_tokens', 0) or 0))

    def _init_token(self) -> str:
        """局域网访问令牌：只写回环地址时为空（不需要认证）。"""
        srv = self.config.get('server', {}) or {}
        host = str(srv.get('host') or '127.0.0.1')
        if host in ('127.0.0.1', 'localhost', '::1'):
            return ''
        tok = str(srv.get('token') or '').strip()
        if tok:
            return tok
        p = self.ws.root / '.agent' / 'access_token'
        try:
            if p.is_file():
                tok = p.read_text(encoding='utf-8').strip()
            if not tok:
                tok = secrets.token_urlsafe(18)
                p.write_text(tok, encoding='utf-8')
        except OSError:
            tok = secrets.token_urlsafe(18)
        # 令牌只存内存 + .agent/access_token 文件，绝不写进 self.config：
        # _persist 会把 config 全量 dump 到 config.json，而 config.json 可能
        # 被提交进公开仓库，写进去等于公开令牌。固定令牌请直接写 access_token
        # 文件（本函数优先读它），或由用户自行写进 config（自行承担保管责任）。
        return tok

    @property
    def auth_required(self) -> bool:
        return bool(self.access_token)

    # ── 关网页即退 ──────────────────────────────────────────────
    # 页面登记表由前端维护：打开登记、关闭注销、每 5 秒心跳续期。
    # 集合空了才开始计时，所以「刷新」和「多标签」天然安全。

    def page_open(self, pid: str) -> int:
        if not pid:
            return len(self._pages)
        self._pages[pid] = time.time()
        self._pages_seen = True
        self._pages_empty_at = 0.0
        log.info('页面登记 %s（当前 %d 个）', pid, len(self._pages))
        return len(self._pages)

    def page_touch(self, pid: str) -> None:
        if pid and pid in self._pages:
            self._pages[pid] = time.time()

    def page_close(self, pid: str) -> int:
        if pid:
            self._pages.pop(pid, None)
            log.info('页面注销 %s（剩余 %d 个）', pid, len(self._pages))
        return len(self._pages)

    def start_exit_watchdog(self) -> None:
        if not self.auto_exit:
            log.info('自动退出已关闭（server.auto_exit=false），关网页后服务继续运行')
            return
        threading.Thread(target=self._exit_watchdog, name='auto-exit',
                         daemon=True).start()

    def _exit_watchdog(self) -> None:
        while True:
            time.sleep(0.5)
            if self._exiting or not self._pages_seen:
                continue
            now = time.time()
            # 浏览器崩溃/被强杀不会发 close，靠心跳超时把残留登记清掉。
            # 阈值给到 2 小时：Chrome 会冻结长时间不可见的标签，定时器跟着停，
            # 阈值定小了会把「最小化了一下午」的正常页面当成残留误杀。
            # 真死掉的页面清不掉也不致命，只是服务不会自动退出而已。
            for k, ts in list(self._pages.items()):
                if now - ts > 7200:
                    self._pages.pop(k, None)
                    log.info('页面 %s 心跳超时（2 小时），视为已关闭', k)
            if self._pages:
                self._pages_empty_at = 0.0
                continue
            if not self._pages_empty_at:
                self._pages_empty_at = now
                continue
            if now - self._pages_empty_at >= self.auto_exit_grace_s:
                self._shutdown_by_page()

    def _shutdown_by_page(self) -> None:
        """页面全关且宽限期已过：退出进程，双击启动的 cmd 窗口随之关闭。"""
        with self._exit_lock:
            if self._exiting:
                return
            self._exiting = True
        msg = ('所有页面已关闭，%d 秒内没有新连接，服务自动退出'
               % round(self.auto_exit_grace_s))
        log.info(msg)
        try:
            print('\n' + msg, flush=True)
        except Exception:
            pass
        # 正在跑的一轮先叫停，避免留下写了一半的文件
        try:
            ag = self._running
            if ag is not None:
                ag.request_stop()
                time.sleep(0.3)
        except Exception:
            pass
        try:
            import logging as _logging
            _logging.shutdown()
        except Exception:
            pass
        # 用 _exit 而不是 sys.exit：看门狗在守护线程里，正常退出流程
        # 会被 serve_forever 卡住，进程挂在那里窗口也关不掉。
        os._exit(0)

    def make_agent(self, ctx, send, model: str = None):
        """为一次请求创建独立的 Agent（含独立的确认/提问通道）。

        旧版全局单例 Agent + 全局 on_delta 回调，一旦 _busy 守卫失效就会串会话；
        现在每个请求一个实例，回调闭包各自绑定自己的 SSE 连接。
        """
        m = self.config.get('model', {}) or {}
        sec = self.config.get('security', {}) or {}
        rag_cfg = self.config.get('rag', {}) or {}
        limits = self.config.get('limits', {}) or {}
        box = ConfirmBox(send, self)
        llm = (self._make_llm(model) if str(model or '').strip() else self.llm)
        agent = Agent(
            self.ws, llm, self.memory,
            confirm_callback=box.confirm,
            rag=self.rag if rag_cfg.get('enabled', True) else None,
            rag_top_k=int(rag_cfg.get('top_k', 4) or 4),
            compress_threshold=int(limits.get('compress_threshold_chars', 0) or 0),
            allow_code_exec=bool(sec.get('allow_code_exec', True)),
            profile=str(sec.get('profile') or 'suggest'),
            auto_approve=sec.get('auto_approve') or {},
            session_allow=self.session_allow,
            role_text=self.agent_role, rules=self.agent_rules,
            ctx=ctx, vision=bool(m.get('vision', False)),
            context_window=int(m.get('context_window', 0) or 0),
            max_turns=int(limits.get('max_turns', 25) or 25))
        agent.on_ask = box.ask
        return agent, box

    def make_batch_agent(self, ctx):
        """批量作业专用 Agent（B8）：无人值守，不弹确认、不提问。

        - 写入按 auto_edit 档位自动放行（结果写进 outputs/<日期>/<作业名>/）；
        - 命令只走用户预先批准的前缀，其余一律拒绝（fail-closed）；
        - 提问（ask_user）直接返回空，避免无人应答卡 180 秒。
        """
        m = self.config.get('model', {}) or {}
        sec = self.config.get('security', {}) or {}
        rag_cfg = self.config.get('rag', {}) or {}
        limits = self.config.get('limits', {}) or {}
        return Agent(
            self.ws, self.llm, self.memory,
            confirm_callback=lambda label: False,
            rag=self.rag if rag_cfg.get('enabled', True) else None,
            rag_top_k=int(rag_cfg.get('top_k', 4) or 4),
            compress_threshold=int(limits.get('compress_threshold_chars', 0) or 0),
            allow_code_exec=bool(sec.get('allow_code_exec', True)),
            profile='auto_edit',
            auto_approve=sec.get('auto_approve') or {},
            session_allow=self.session_allow,
            role_text=self.agent_role, rules=self.agent_rules,
            ctx=ctx, vision=bool(m.get('vision', False)),
            context_window=int(m.get('context_window', 0) or 0),
            max_turns=int(limits.get('max_turns', 25) or 25))

    def maybe_extract_memory(self, sid, full) -> bool:
        """后台记忆提取的准入门（B6）：在同一个模型上多跑一次推理，离线单卡场景
        抢槽明显，所以必须可关、且只在值得时提：

        - 配置 memory.extract=false（设置→运行可关）
        - 短会话（< 6 条）不值得提
        - 已有提取在跑 / 批量作业进行中：不抢同一个推理槽
        """
        if not (self.config.get('memory') or {}).get('extract', True):
            return False
        if len(getattr(full, 'messages', None) or []) < 6:
            return False
        if self.memory.extracting() or self.batch.running():
            return False
        self.memory.extract_summary(self.llm, sid, full.title, full.messages)
        return True

    # ── 配置保存/热切换 ─────────────────────────────────────────
    def save_config(self, model: dict = None, limits: dict = None, rag: dict = None,
                    security: dict = None, prompt: dict = None,
                    memory: dict = None):
        """保存配置到 config.json（api_key 自动加密），并热切换到新 LLM。

        关键约定：
        - 前端 api_key 留空 = **保持不变**（旧版会把空串写进去，直接抹掉用户 key）
        - 磁盘上 api_key 永远是 ENC: 密文，明文只存在 self._api_key_plain
        """
        if model is not None:
            m = self.config.setdefault('model', {})
            new_key = str(model.get('api_key') or '').strip()
            for k, v in model.items():
                if k != 'api_key':
                    m[k] = v
            if new_key:                       # 留空表示不修改
                if new_key.startswith('ENC:'):
                    m['api_key'] = new_key
                    self._api_key_plain = self._decrypt_key(new_key)
                else:
                    try:
                        from encrypt_key import encrypt_key
                        enc_key = 'ENC:' + encrypt_key(new_key)
                    except Exception as e:
                        # 加密失败绝不退回明文落盘：直接拒绝本次保存，磁盘上
                        # 的旧密文保持不变（否则明文 key 会随 _persist 进 config.json）。
                        log.error('API Key 加密失败，已拒绝保存: %s', e)
                        raise ValueError('密钥加密失败（密钥文件不可写？），已拒绝保存，请检查后重试')
                    m['api_key'] = enc_key
                    self._api_key_plain = new_key
            self.llm = self._make_llm()
            log.info('模型配置已更新: %s @ %s（key=%s）',
                     m.get('name'), m.get('base_url'),
                     '已设置' if self._api_key_plain else '未设置')
        if prompt is not None:
            # 清洗后写回 config：空规则剔除、去首尾空格
            p = self.config.setdefault('prompt', {})
            role = str((prompt or {}).get('role') or '').strip()
            rules = [str(r).strip() for r in ((prompt or {}).get('rules') or [])
                     if str(r).strip()]
            p['role'] = role
            p['rules'] = rules
            self.agent_role = role
            self.agent_rules = rules
        if limits is not None:
            self.config.setdefault('limits', {}).update(limits)
            self.max_upload_mb = float(self.config['limits'].get('max_upload_mb', 50))
            self.chat_timeout_s = float(self.config['limits'].get('chat_timeout_s', 600))
            if 'llm_timeout_s' in (limits or {}):
                # 模型超时改了要热切换 LLM 客户端，否则得重启才生效
                self.llm = self._make_llm()
        if rag is not None:
            self.config.setdefault('rag', {}).update(rag)
        if memory is not None:
            # 记忆提取开关（B6）：离线单卡场景关掉可避免后台再占一次推理
            mm = self.config.setdefault('memory', {})
            mm['extract'] = bool(memory.get('extract', True))
        if security is not None:
            from agent.tools import PROFILES
            sec = dict(security or {})
            # session_allow 是内存态（本次运行批准的命令前缀），不落盘
            if 'session_allow' in sec:
                self.session_allow = [str(x).strip().lower()
                                      for x in (sec.pop('session_allow') or [])
                                      if str(x).strip()][:50]
            if str(sec.get('profile') or 'suggest') not in PROFILES:
                sec['profile'] = 'suggest'
            self.config.setdefault('security', {}).update(sec)
            log.info('安全配置已更新: profile=%s, auto_approve=%s, 已记住 %s 个命令前缀',
                     self.config['security'].get('profile'),
                     self.config['security'].get('auto_approve'), len(self.session_allow))
        self._persist()

    def save_mcp_server(self, srv: dict, original_name: str = None):
        """设置页增删改 MCP server：校验 -> 写 config -> 立即重连。

        original_name 非空表示编辑已有项（名字可改，改完旧项删掉）。
        """
        import re as _re
        srv = dict(srv or {})
        name = str(srv.get('name') or '').strip()
        if not name:
            raise ValueError('缺少服务名')
        if not _re.fullmatch(r'[A-Za-z0-9\u4e00-\u9fa5_.-]{1,40}', name) or '__' in name:
            raise ValueError('服务名只能用字母/数字/中文/下划线/点/短横线（1-40 字，不能含 __）')
        command = str(srv.get('command') or '').strip()
        if not command:
            raise ValueError('缺少启动命令（如 npx / python / node / 绝对路径）')
        if '\n' in command or '\r' in command:
            raise ValueError('启动命令不能换行')
        args = srv.get('args')
        if isinstance(args, str):
            args = _split_cmd_args(args)
        args = [str(a) for a in (args or [])]
        env = srv.get('env') or {}
        if isinstance(env, str):
            pairs = {}
            for line in env.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                k, _, v = line.partition('=')
                if k.strip():
                    pairs[k.strip()] = v.strip()
            env = pairs
        if not isinstance(env, dict):
            raise ValueError('环境变量格式不对（应为 KEY=VALUE 每行一条）')
        try:
            timeout = float(srv.get('timeout') or 20)
        except (TypeError, ValueError):
            raise ValueError('超时必须是数字（秒）')
        timeout = min(600.0, max(1.0, timeout))

        mcfg = self.config.setdefault('mcp', {})
        servers = [dict(x) for x in (mcfg.get('servers') or [])]
        old_name = str(original_name or '').strip()
        existing = {str(x.get('name') or '') for x in servers}
        if not old_name and name in existing:
            raise ValueError(f'同名 MCP 服务已存在：{name}（可在该项上点“编辑”）')
        if old_name and old_name != name and name in existing:
            raise ValueError(f'已存在同名 MCP 服务：{name}')
        old = next((x for x in servers
                    if str(x.get('name') or '') in (old_name, name)), {})
        # 脱敏占位符回传 = 不改：status_dict 只给前端占位符，保存时收到占
        # 位符就保留旧的 ENC: 密文；否则占位符会被当成真值加密存盘，覆盖
        # 掉原来的密钥。旧配置里没有该 key 时占位符按空处理（丢弃）。
        old_env = old.get('env') or {}
        if isinstance(old_env, dict):
            for k, v in list(env.items()):
                if v == _ENV_MASKED:
                    if k in old_env:
                        env[k] = old_env[k]
                    else:
                        del env[k]
        rec = {
            'name': name, 'command': command, 'args': args, 'env': env,
            'enabled': bool(srv.get('enabled', old.get('enabled', True))),
            'timeout': timeout,
        }
        servers = [x for x in servers
                   if str(x.get('name') or '') not in (old_name, name)]
        try:
            # 环境变量只存 ENC: 密文（与 api_key 同机制）；加密失败拒绝保存。
            rec['env'] = self._encrypt_mcp_env(env)
        except Exception as e:
            log.error('MCP 环境变量加密失败，已拒绝保存: %s', e)
            raise ValueError('MCP 密钥加密失败（密钥文件不可写？），已拒绝保存，请检查后重试')
        servers.append(rec)
        mcfg['servers'] = servers
        self._persist()
        from agent.mcp import manager
        manager.configure(self._servers_for_runtime(servers))
        log.info('MCP 配置已保存: %s -> %s %s', old_name or '(新增)', command,
                 ' '.join(args)[:120])
        return servers

    def delete_mcp_server(self, name: str) -> list:
        """删除一个 MCP server（同时停掉子进程）。"""
        name = str(name or '').strip()
        if not name:
            raise ValueError('缺少服务名')
        mcfg = self.config.setdefault('mcp', {})
        servers = [dict(x) for x in (mcfg.get('servers') or [])]
        keep = [x for x in servers if str(x.get('name') or '') != name]
        if len(keep) == len(servers):
            raise ValueError(f'没有找到 MCP 服务：{name}')
        mcfg['servers'] = keep
        self._persist()
        from agent.mcp import manager
        manager.configure(self._servers_for_runtime(keep))
        log.info('MCP 已删除: %s', name)
        return keep

    def set_mcp_enabled(self, toggles):
        """设置页开关 MCP server（B5）：只改 enabled 并按新配置重连。"""
        flag = {str(t.get('name')): bool(t.get('enabled', True))
                for t in (toggles or []) if isinstance(t, dict)}
        if not flag:
            return
        mcfg = self.config.setdefault('mcp', {})
        servers = [dict(s) for s in (mcfg.get('servers') or [])]
        for s in servers:
            nm = str(s.get('name') or '')
            if nm in flag:
                s['enabled'] = flag[nm]
        mcfg['servers'] = servers
        self._persist()
        from agent.mcp import manager
        manager.configure(self._servers_for_runtime(servers))
        log.info('MCP 配置已更新: %s',
                 ', '.join(f"{s.get('name')}={'开' if s.get('enabled', True) else '关'}"
                           for s in servers) or '（空）')

    def _persist(self):
        """原子写 config.json（api_key 已保证是 ENC 形态）。"""
        if not self.config_path:
            return
        try:
            import tempfile
            p = self.config_path
            d = os.path.dirname(p) or '.'
            fd, tmp = tempfile.mkstemp(dir=d, prefix='config.', suffix='.tmp')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception as e:
            log.error('保存配置失败: %s', e)

    def _redacted_config(self) -> dict:
        """配置快照（**脱敏**）：api_key / MCP 环境变量 / 访问令牌一律不外带。

        界面用的是 /api/settings（本身就脱敏）；接口留给排障脚本，但绝不能
        把密钥原样吐出来（之前任何能访问的人都能拿走 MCP 的 env 与密文 key）。
        """
        import copy as _copy
        cfg = _copy.deepcopy(self.config or {})
        m = cfg.get('model')
        if isinstance(m, dict):
            m['api_key'] = _ENV_MASKED if m.get('api_key') else ''
        srv = cfg.get('server')
        if isinstance(srv, dict):
            srv.pop('token', None)
        mcp = cfg.get('mcp')
        if isinstance(mcp, dict):
            for s in (mcp.get('servers') or []):
                if isinstance(s, dict) and s.get('env'):
                    s['env'] = {k: _ENV_MASKED for k in s['env']}
        sec = cfg.get('security')
        if isinstance(sec, dict) and 'session_allow' in sec:
            sec['session_allow'] = len(sec.get('session_allow') or [])
        return cfg

    # ── 危险命令确认 / 向用户提问（agent 线程调用，推帧 + 等用户）───
    # 局域网多人共用时：只有发起该轮的人能回答/停止（owner = 浏览器标识）
    def submit_confirm(self, approved: bool, owner: str = None) -> str:
        """返回 'ok'（已生效）/ 'stale'（已超时或不属于当前轮）/ 'forbidden'（不是发起人）。"""
        box = self._pending_confirm
        if box is None:
            return 'stale'
        if not box.owner or not owner or box.owner != owner:
            log.warning('忽略非发起人的确认应答：owner=%s 请求方=%s', box.owner or '(缺失)', owner or '(缺失)')
            return 'forbidden'
        box.resolve(approved=approved)
        return 'ok'

    def submit_answer(self, text: str, owner: str = None) -> str:
        box = self._pending_confirm
        if box is None:
            return 'stale'
        if not box.owner or not owner or box.owner != owner:
            log.warning('忽略非发起人的提问应答：owner=%s 请求方=%s', box.owner or '(缺失)', owner or '(缺失)')
            return 'forbidden'
        box.resolve(answer=text)
        return 'ok'

    # ── 状态 ─────────────────────────────────────────────────────
    @property
    def busy(self):
        return self._busy

    # ── 会话操作 ─────────────────────────────────────────────────
    def _auto_title(self, message: str) -> str:
        t = ' '.join((message or '').split())
        return t[:20] or '新会话'

    # ── 常用 prompt 模板（工作台 .agent/templates.json，离线本地存储）──
    def _templates_path(self):
        return self.ws.root / '.agent' / 'templates.json'

    def load_templates(self):
        try:
            with open(self._templates_path(), 'r', encoding='utf-8') as f:
                data = json.load(f)
            return [t for t in data.get('templates', []) if isinstance(t, dict)]
        except (OSError, ValueError):
            return []

    def save_templates(self, templates: list):
        p = self._templates_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        clean = []
        for t in templates:
            if not isinstance(t, dict):
                continue
            name = str(t.get('name') or '').strip()[:60]
            content = str(t.get('content') or '').strip()
            if name and content:
                clean.append({"name": name, "content": content})
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix='templates.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({"templates": clean}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── 产物元信息 ───────────────────────────────────────────────
    def _artifact_metas(self, rels):
        out = []
        for r in rels:
            m = self.ws.artifact_meta(r)
            if m:
                out.append(m)
            else:
                # 文件确实找不到（被删除/移出工作台）：仍列出并标记 missing，
                # 前端显示「文件已移动」而不是静默消失（用户之前反馈"刷新后变空"）
                name = os.path.basename(str(r).replace('\\', '/'))
                out.append({"rel": r, "name": name, "ext": '', "size": 0,
                            "missing": True})
        return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'AgentWorkbench/1.0'

    # ── 辅助 ─────────────────────────────────────────────────────
    @property
    def app(self) -> AgentServer:
        return self.server.agent_server

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _read_body(self, limit=5 * 1024 * 1024):
        length = int(self.headers.get('Content-Length') or 0)
        if length > limit:
            raise ValueError('body too large')
        return self.rfile.read(length)

    def _is_loopback(self) -> bool:
        """请求是否来自本机（127.0.0.1）。

        局域网访问时“在文件管理器里打开 / 用系统程序打开”实际是在**服务器那台
        机器**上弹窗，远程用户看不到，也不该让远程会话去启动主机程序。
        """
        try:
            host = (self.client_address or ('', ))[0]
        except Exception:
            return False
        return str(host) in ('127.0.0.1', '::1', 'localhost', '::ffff:127.0.0.1')

    def _client_id(self) -> str:
        """读取服务端签名的浏览器标识；拒绝客户端自行伪造/改写的普通 cookie。"""
        ck = self.headers.get('Cookie') or ''
        for part in ck.split(';'):
            k, _, value = part.strip().partition('=')
            if k != 'wbclient':
                continue
            client_id, dot, signature = value.partition('.')
            if dot and re.fullmatch(r'[A-Za-z0-9_-]{16,64}', client_id):
                expected = hmac.new(self.app.client_cookie_key, client_id.encode('ascii'),
                                    'sha256').hexdigest()
                if hmac.compare_digest(signature, expected):
                    return client_id
            break
        # 第一次授权请求即使是 POST，也使用本次响应要签发的身份；
        # 但它绝不会与已有任务 owner 匹配，因此不能代替他人操作。
        return getattr(self, '_issued_client_id', '')

    def _issue_client_cookie(self):
        """首次访问时发放 HttpOnly 的签名身份 cookie，避免由浏览器脚本自报身份。"""
        if self._client_id():
            return
        client_id = secrets.token_urlsafe(24)
        signature = hmac.new(self.app.client_cookie_key, client_id.encode('ascii'),
                             'sha256').hexdigest()
        self._issued_client_id = client_id
        self._pending_client_cookie = f'{client_id}.{signature}'

    def _client_alive(self) -> bool:
        """探测客户端连接是否还活着（浏览器关页/断网时快速退出等待，不占线程）。

        非阻塞 recv + MSG_PEEK：有数据/无数据都视为活着，只有连接已关闭才返回 False。
        """
        try:
            import socket
            self.connection.setblocking(False)
            try:
                got = self.connection.recv(1, socket.MSG_PEEK)
                # 空字节 = 对端已关闭（FIN），必须视为断开——否则排队任务会挂满超时
                return bool(got)
            except BlockingIOError:
                return True
            except OSError:
                return False
            finally:
                # 必须恢复阻塞：否则后续 SSE wfile.write 可能 BlockingIOError，帧被静默丢弃
                self.connection.setblocking(True)
        except Exception:
            return True

    # ── 局域网访问令牌 ───────────────────────────────────────────
    def end_headers(self):
        """统一安全响应头与访问令牌/浏览器身份 cookie。"""
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        tok = getattr(self, '_pending_cookie', None)
        if tok:
            self._pending_cookie = None
            self.send_header('Set-Cookie',
                             f'wbtoken={tok}; Path=/; HttpOnly; SameSite=Lax; '
                             f'Max-Age=2592000')
        client = getattr(self, '_pending_client_cookie', None)
        if client:
            self._pending_client_cookie = None
            self.send_header('Set-Cookie',
                             f'wbclient={client}; Path=/; HttpOnly; SameSite=Lax; '
                             f'Max-Age=2592000')
        super().end_headers()

    def _auth_ok(self) -> bool:
        """校验访问令牌：Authorization: Bearer / ?t= / cookie 三选一。

        只监听回环地址时 access_token 为空，直接放行（本地单机无认证负担）。
        """
        app = self.app
        tok = app.access_token
        if not tok:
            self._issue_client_cookie()
            return True
        h = self.headers.get('Authorization') or ''
        if h.startswith('Bearer ') and secrets.compare_digest(h[7:].strip(), tok):
            self._issue_client_cookie()
            return True
        qs = parse_qs(urlparse(self.path).query)
        q = (qs.get('t') or [''])[0]
        if q and secrets.compare_digest(q, tok):
            # 首次带 ?t= 访问：种 cookie，之后地址栏不再暴露令牌
            self._pending_cookie = tok
            self._issue_client_cookie()
            return True
        ck = self.headers.get('Cookie') or ''
        for part in ck.split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'wbtoken' and secrets.compare_digest(v.strip(), tok):
                self._issue_client_cookie()
                return True
        return False

    def _same_origin_request(self) -> bool:
        """验证浏览器写请求的来源；无来源头时保留本机脚本/CLI兼容。"""
        source = self.headers.get('Origin') or self.headers.get('Referer')
        if not source:
            return True
        try:
            parsed = urlparse(source)
            host = (self.headers.get('Host') or '').lower()
            return parsed.scheme in ('http', 'https') and parsed.netloc.lower() == host
        except Exception:
            return False

    def _auth_fail(self):
        """未认证：页面请求给登录页，API 请求给 401 JSON。"""
        path = urlparse(self.path).path
        if path.startswith('/api/'):
            return self._json({"error": '需要访问令牌', "auth_required": True}, 401)
        body = _LOGIN_HTML.encode('utf-8')
        self.send_response(401)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── GET ──────────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        # /api/health 免认证：局域网内探活/排查用，不泄露任何数据
        authed = path == '/api/health' or self._auth_ok()
        if not authed:
            return self._auth_fail()
        # 成功使用 ?t= 登录后马上移除地址栏令牌，避免浏览历史/复制链接泄漏。
        if path in ('/', '/index.html') and (qs.get('t') or [''])[0]:
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        try:
            if path == '/' or path == '/index.html':
                return self._static('index.html')
            if path == '/app.css':
                return self._static('app.css')
            if path == '/app.js':
                return self._static('app.js')
            if path == '/api/sessions':
                return self._json({"sessions": self.app.sessions.list_all()})
            if path == '/api/session':
                sid = (qs.get('id') or [''])[0]
                try:
                    s = self.app.sessions.load(sid)
                except (ValueError, FileNotFoundError):
                    return self._err(404, '会话不存在')
                return self._json({"session": {
                    "id": s.id, "title": s.title, "created_at": s.created_at,
                    "messages": s.messages,
                    "compact": s.compact,
                    "artifacts": self.app._artifact_metas(s.artifacts)}})
            if path == '/api/file':
                rel = (qs.get('path') or [''])[0]
                return self._serve_file(rel)
            if path == '/api/preview':
                # 站内预览：产物点开不再直接下载/新开页（U6）
                rel = (qs.get('path') or [''])[0]
                return self._preview(rel)
            if path == '/api/artifact/versions':
                rel = (qs.get('path') or [''])[0]
                return self._json({"versions": self.app.ws.list_versions(rel)})
            if path == '/api/artifact/version':
                # 历史版本原始文件（预览/下载）
                rel = (qs.get('path') or [''])[0]
                ts = (qs.get('ts') or [''])[0]
                try:
                    p = self.app.ws.version_path(rel, ts)
                except PermissionError as e:
                    return self._err(400, str(e))
                except FileNotFoundError as e:
                    return self._err(404, str(e))
                return self._send_file(p)
            if path == '/api/artifact/diff':
                # 历史版本与当前的逐行 diff（产物版本对比视图）
                rel = (qs.get('path') or [''])[0]
                ts = (qs.get('ts') or [''])[0]
                if not rel or not ts:
                    return self._err(400, '缺少 path / ts')
                try:
                    return self._json(self.app.ws.version_diff(rel, ts))
                except PermissionError as e:
                    return self._err(400, str(e))
                except FileNotFoundError as e:
                    return self._err(404, str(e))
            if path in ('/api/open-folder', '/api/open-file'):
                rel = (qs.get('path') or [''])[0]
                return self._open_folder(rel)
            if path == '/api/workspace/tree':
                return self._json({"root": str(self.app.ws.root),
                                   "tree": self._workspace_tree()})
            if path == '/api/memory':
                return self._json({"facts": self.app.memory.all_facts(),
                                   "prefs": self.app.memory.get_prefs()})
            if path == '/api/health':
                authed = self._auth_ok()
                base = {
                    "ok": True,
                    "busy": self.app.busy,
                    "auth_required": self.app.auth_required,
                    "authed": bool(authed),
                    "is_loopback": self._is_loopback(),
                    "uptime_s": round(time.time() - self.app._start_ts, 1),
                    "auto_exit": self.app.auto_exit,
                    "auto_exit_grace_s": self.app.auto_exit_grace_s,
                    "pages": len(self.app._pages),
                }
                # 免认证探活：只给"活着/忙不忙"，不泄露工作目录与模型地址
                if self.app.auth_required and not authed:
                    return self._json(base)
                from agent.tools import detect_optional_deps
                rag_ok = True
                try:
                    rag_ok = bool(self.app.rag.stats().get('ok', True))
                except Exception:
                    rag_ok = False
                m = self.app.config.get('model', {}) or {}
                # 免认证接口：绝不能把 api_key（哪怕是 ENC 密文）带出去
                safe_model = {k: v for k, v in m.items() if k != 'api_key'}
                base.update({
                    "model": safe_model,
                    "context_window": int(m.get('context_window', 0) or 0),
                    "workspace": str(self.app.ws.root),
                    "deps": detect_optional_deps(),
                    "rag_ok": rag_ok,
                })
                return self._json(base)
            if path == '/api/config':
                return self._json(self.app._redacted_config())
            if path == '/api/settings':
                return self._json(self._settings())
            if path == '/api/rag/index':
                return self._json(self.app.rag.refresh())
            if path == '/api/rag/search':
                q = (qs.get('q') or [''])[0]
                return self._json({"results": self.app.rag.search(q, 5)})
            if path == '/api/tree':
                return self._json(self._dir_listing(qs))
            if path == '/api/search':
                q = (qs.get('q') or [''])[0]
                return self._json({"results": self.app.sessions.search(q)})
            if path == '/api/jobs':
                cid = self._client_id()
                title = ''
                if self.app._running_sid:
                    try:
                        title = self.app.sessions.load(self.app._running_sid).title
                    except Exception:
                        title = ''
                return self._json({"jobs": self.app._jobs, "queue": len(self.app._queue),
                                   "running": bool(self.app._running),
                                   "running_sid": self.app._running_sid or '',
                                   "running_title": title,
                                   "running_owner_is_me": bool(
                                       self.app._running_owner and cid
                                       and self.app._running_owner == cid),
                                   "started_at": self.app._run_started})
            if path == '/api/batch':
                # 批量作业（B8）：带 id 返回单个台账（含逐项状态），否则返回列表
                jid = (qs.get('id') or [''])[0]
                if jid:
                    try:
                        return self._json({"job": self.app.batch.get(jid)})
                    except PermissionError as e:
                        return self._err(400, str(e))
                    except FileNotFoundError as e:
                        return self._err(404, str(e))
                return self._json({"jobs": self.app.batch.list(),
                                   "running": self.app.batch._cur is not None})
            if path == '/api/batch/csv':
                jid = (qs.get('id') or [''])[0]
                try:
                    txt = self.app.batch.to_csv(jid)
                except PermissionError as e:
                    return self._err(400, str(e))
                except FileNotFoundError as e:
                    return self._err(404, str(e))
                data = ('\ufeff' + txt).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv; charset=utf-8')
                self.send_header(
                    'Content-Disposition',
                    f'attachment; filename="batch-{jid}.csv"')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            if path == '/api/logs':
                n = 300
                try:
                    n = max(20, min(2000, int((qs.get('n') or ['300'])[0])))
                except ValueError:
                    pass
                return self._json({"path": str(logsetup.path(self.app.ws.root)),
                                   "lines": logsetup.tail(self.app.ws.root, n)})
            if path == '/api/rag/stats':
                return self._json(self.app.rag.stats())
            if path == '/api/undo/list':
                sid = (qs.get('id') or [''])[0]
                if not sid:
                    return self._err(400, '缺少 id')
                return self._json({"turns": self.app.ws.list_turns(sid)})
            if path == '/api/turn/diff':
                sid = (qs.get('id') or [''])[0]
                turn = (qs.get('turn') or [''])[0]
                if not sid or not turn:
                    return self._err(400, '缺少 id / turn')
                try:
                    return self._json(self.app.ws.turn_diff(sid, turn))
                except FileNotFoundError as e:
                    return self._err(404, str(e))
            if path == '/api/project':
                return self._json({"files": self._project_files()})
            if path == '/api/templates':
                return self._json({"templates": self.app.load_templates()})
            if path == '/api/export_session':
                sid = (qs.get('id') or [''])[0]
                return self._export_session(sid)
            if path == '/api/export_artifacts':
                sid = (qs.get('id') or [''])[0]
                return self._export_artifacts_zip(sid)
            if path == '/api/backup':
                return self._backup_zip()
            if path.startswith('/vendor/'):
                return self._static(path.lstrip('/'))
            if path == '/favicon.ico':
                return self._err(404, 'not found')
            return self._err(404, 'not found')
        except Exception as e:
            self._err(500, f'{type(e).__name__}: {e}')

    def _static(self, name):
        # 名字来自 URL（/vendor/../../config.json）：先 resolve 再校验落在
        # WEB_DIR 内，否则 404；原样拼路径会越界读到项目内任意文件。
        try:
            p = (WEB_DIR / name).resolve()
            p.relative_to(WEB_DIR.resolve())
        except (ValueError, RuntimeError, OSError):
            return self._err(404, 'not found')
        if not p.is_file():
            return self._err(404, 'not found')
        body = p.read_bytes()
        ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        self.send_response(200)
        self.send_header('Content-Type', f'{ctype}; charset=utf-8')
        # 禁用缓存：离线机升级后浏览器必须拿到最新 app.js/app.css，否则"改了个寂寞"
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _open_folder(self, rel: str):
        """在系统文件管理器中打开产物所在文件夹并选中该文件（本地桌面应用）。

        参考老版 agent 的 /api/open-folder。仅允许工作台内路径；文件被移动时
        先按文件名自动找回真实位置再打开。

        仅限本机（回环）请求：局域网里从别的机器/手机点这个按钮，弹出的是
        服务器主机的窗口，远程用户既看不到也不应该触发。
        """
        if not self._is_loopback():
            return self._err(403, '该操作在服务器本机执行，远程访问时不可用；'
                                  '请在本机上操作或改用「下载」')
        rel = str(rel).replace('\\', '/').strip()
        if not rel or rel.startswith('/') or '..' in rel.split('/'):
            return self._err(400, '非法路径')
        try:
            candidate = self.app.ws.root / rel
            self.app.ws._reject_symlinks(candidate)
            p = candidate.resolve()
            p.relative_to(self.app.ws.root.resolve())
        except Exception:
            return self._err(400, '路径必须在工作台内且不能包含符号链接')
        if not p.exists():
            m = self.app.ws.artifact_meta(rel)
            if not m:
                return self._err(404, '文件不存在')
            p = (self.app.ws.root / m['rel']).resolve()
        target = str(p)
        try:
            if os.name == 'nt':
                subprocess.Popen(['explorer', '/select,', target])
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', '-R', target])
            else:
                subprocess.Popen(['xdg-open', os.path.dirname(target)])
        except Exception as e:
            return self._err(500, f'无法打开文件夹: {e}')
        return self._json({"ok": True, "opened": rel})

    def _serve_file(self, rel: str):
        """产物/笔记预览下载。仅允许工作台内路径（防越界读取）。"""
        try:
            candidate = self.app.ws.root / rel
            self.app.ws._reject_symlinks(candidate)
            p = candidate.resolve()
            p.relative_to(self.app.ws.root.resolve())
        except Exception:
            return self._err(400, '路径必须在工作台内且不能包含符号链接')
        if not p.is_file():
            return self._err(404, '文件不存在')
        return self._send_file(p)

    # 会执行脚本的类型：必须以独立源 + 禁脚本呈现，否则任何产物 html 都能以
    # 用户身份调用全部接口（局域网模式下最危险的一条）
    _ACTIVE_EXT = {'html', 'htm', 'xhtml', 'shtml', 'svg', 'xml', 'xsl', 'xslt',
                   'js', 'mjs', 'htc'}
    _CHUNK = 256 * 1024

    def _send_file(self, p):
        """分块发送文件：支持 Range（大 PDF/日志不再整份读进内存）。"""
        try:
            size = p.stat().st_size
        except OSError:
            return self._err(404, '文件不可读')
        ext = p.suffix.lower().lstrip('.')
        start, end, code = 0, max(0, size - 1), 200
        m = re.match(r'bytes=(\d*)-(\d*)$', (self.headers.get('Range') or '').strip())
        if m and size > 0:
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
                end = size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None
            end = min(end, size - 1)
            code = 206
        length = max(0, end - start + 1)
        ctype = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
        if ctype.startswith('text/'):
            ctype += '; charset=utf-8'
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        # 中文文件名: HTTP 头仅 ASCII, 用 RFC 5987 编码(否则 latin-1 抛异常 -> 502)
        from urllib.parse import quote as _quote
        self.send_header('Content-Disposition',
                         f"inline; filename*=UTF-8''{_quote(p.name)}")
        if ext in self._ACTIVE_EXT:
            # sandbox：当作独立源且禁止脚本，但样式/图片照常渲染（报告类 html 仍能看）
            self.send_header('Content-Security-Policy', 'sandbox')
        self.end_headers()
        try:
            with open(p, 'rb') as f:
                if start:
                    f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(self._CHUNK, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (OSError, ValueError) as e:
            log.warning('发送文件中断: %s (%s)', p.name, e)
        return None

    # ── 站内预览（U6 产物内联预览）────────────────────────────
    _IMG_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico'}
    _TEXT_EXT = {'md', 'txt', 'csv', 'tsv', 'json', 'log', 'py', 'js', 'ts', 'jsx',
                 'tsx', 'html', 'css', 'scss', 'yaml', 'yml', 'xml', 'sql', 'ini',
                 'cfg', 'conf', 'sh', 'ps1', 'bat', 'toml', 'properties', 'java',
                 'c', 'cpp', 'h', 'hpp', 'go', 'rs', 'vue', 'env.example'}

    def _preview(self, rel: str):
        """返回文件预览负载：文本直接给内容，办公文档抽取文本，图片/PDF 由前端直连 /api/file。

        仅允许工作台内路径（与 _serve_file 同一套越界防护）。
        """
        rel = str(rel).replace('\\', '/').strip()
        if not rel or rel.startswith('/') or '..' in rel.split('/'):
            return self._err(400, '非法路径')
        try:
            candidate = self.app.ws.root / rel
            self.app.ws._reject_symlinks(candidate)
            p = candidate.resolve()
            p.relative_to(self.app.ws.root.resolve())
        except Exception:
            return self._err(400, '路径必须在工作台内且不能包含符号链接')
        if not p.is_file():
            return self._err(404, '文件不存在')
        ext = p.suffix.lower().lstrip('.')
        try:
            size = p.stat().st_size
        except OSError:
            return self._err(404, '文件不可读')
        if ext in self._IMG_EXT:
            return self._json({'type': 'image', 'ext': ext, 'size': size})
        if ext == 'pdf':
            return self._json({'type': 'pdf', 'ext': ext, 'size': size})
        if ext in self._TEXT_EXT:
            cap = 512 * 1024
            # 只读需要的一段：大日志/大 CSV 预览不再整价文件进内存
            with open(p, 'rb') as f:
                raw = f.read(cap + 1)
            return self._json({'type': 'text', 'ext': ext, 'size': size,
                               'text': raw[:cap].decode('utf-8', errors='replace'),
                               'truncated': len(raw) > cap or size > cap})
        if ext == 'docx':
            try:
                import docx
            except ImportError:
                return self._json({'type': 'unsupported', 'ext': ext, 'size': size,
                                   'hint': '缺少 python-docx（pip install python-docx）后可预览正文'})
            d = docx.Document(str(p))
            lines = [x.text for x in d.paragraphs if x.text.strip()]
            for ti, t in enumerate(d.tables, 1):
                lines.append(f'[表格{ti}]')
                for row in list(t.rows)[:80]:
                    lines.append(' | '.join(c.text.strip() for c in row.cells))
            t, tr = self._clip_text('\n'.join(lines))
            return self._json({'type': 'doc', 'ext': ext, 'size': size,
                               'text': t, 'truncated': tr})
        if ext in ('xlsx', 'xlsm'):
            try:
                import openpyxl
            except ImportError:
                return self._json({'type': 'unsupported', 'ext': ext, 'size': size,
                                   'hint': '缺少 openpyxl（pip install openpyxl）后可预览表格'})
            wb = openpyxl.load_workbook(str(p), data_only=True, read_only=True)
            out = []
            for sheet in wb.sheetnames[:3]:
                sx = wb[sheet]
                out.append(f'── 工作表: {sheet} ──')
                n = 0
                for row in sx.iter_rows(values_only=True):
                    if n >= 200:
                        out.append('…（仅预览前 200 行）')
                        break
                    vals = ['' if v is None else str(v) for v in row[:40]]
                    if any(vals):
                        out.append(' | '.join(vals))
                    n += 1
            try:
                wb.close()
            except Exception:
                pass
            t, tr = self._clip_text('\n'.join(out))
            return self._json({'type': 'doc', 'ext': ext, 'size': size,
                               'text': t, 'truncated': tr})
        if ext == 'pptx':
            try:
                from pptx import Presentation
            except ImportError:
                return self._json({'type': 'unsupported', 'ext': ext, 'size': size,
                                   'hint': '缺少 python-pptx（pip install python-pptx）后可预览文字'})
            prs = Presentation(str(p))
            out = []
            for i, slide in enumerate(prs.slides, 1):
                texts = [sh.text for sh in slide.shapes
                         if hasattr(sh, 'text') and sh.text.strip()]
                if texts:
                    out.append(f'── 第 {i} 页 ──\n' + '\n'.join(texts))
            t, tr = self._clip_text('\n'.join(out) or '（无可提取文本）')
            return self._json({'type': 'doc', 'ext': ext, 'size': size,
                               'text': t, 'truncated': tr})
        return self._json({'type': 'binary', 'ext': ext, 'size': size})

    @staticmethod
    def _clip_text(s: str, cap: int = 400_000) -> tuple:
        if len(s) <= cap:
            return s, False
        return s[:cap], True

    # ── POST ─────────────────────────────────────────────────────
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        if not self._auth_ok():
            return self._auth_fail()
        if not self._same_origin_request():
            return self._err(403, '跨站请求已拒绝')
        try:
            if path == '/api/chat':
                return self._handle_chat()
            if path == '/api/session/new':
                d = json.loads(self._read_body() or b'{}')
                s = self.app.sessions.create(d.get('title') or '新会话')
                return self._json({"session": s.to_meta()})
            if path == '/api/session/rename':
                d = json.loads(self._read_body() or b'{}')
                self.app.sessions.rename(d['id'], d.get('title') or '新会话')
                return self._json({"ok": True})
            if path == '/api/session/delete':
                d = json.loads(self._read_body() or b'{}')
                self.app.sessions.delete(d['id'])
                return self._json({"ok": True})
            if path == '/api/session/pin':
                d = json.loads(self._read_body() or b'{}')
                self.app.sessions.set_pinned(d['id'], bool(d.get('pinned')))
                return self._json({"ok": True})
            if path == '/api/session/regenerate':
                d = json.loads(self._read_body() or b'{}')
                try:
                    s = self.app.sessions.load(d['id'])
                except (ValueError, FileNotFoundError):
                    return self._err(404, '会话不存在')
                while s.messages and s.messages[-1].get('role') == 'assistant':
                    s.messages.pop()
                self.app.sessions._save(s)
                return self._json({"ok": True})
            if path == '/api/session/fork':
                # 从某条消息分叉出新会话（「从这里分支」）
                d = json.loads(self._read_body() or b'{}')
                sid = str(d.get('id') or '')
                if not sid:
                    return self._err(400, '缺少 id')
                try:
                    s = self.app.sessions.fork(sid, int(d.get('index') or 0))
                except ValueError as e:
                    return self._err(400, str(e))
                log.info('会话分叉: %s -> %s (index=%s)', sid, s.id, d.get('index'))
                return self._json({"ok": True, "session": s.to_meta()})
            if path == '/api/session/truncate':
                # 删除某条及其之后的对话（编辑重发 / 删除该轮）
                d = json.loads(self._read_body() or b'{}')
                sid = str(d.get('id') or '')
                if not sid:
                    return self._err(400, '缺少 id')
                removed = self.app.sessions.truncate(sid, int(d.get('index') or 0))
                log.info('会话截断: %s 从 index=%s 起删除 %s 条', sid, d.get('index'), removed)
                return self._json({"ok": True, "removed": removed})
            if path == '/api/note':
                d = json.loads(self._read_body(limit=2 * 1024 * 1024) or b'{}')
                return self._save_note(d)
            if path == '/api/upload':
                return self._handle_upload(qs)
            if path == '/api/attachment/delete':
                d = json.loads(self._read_body() or b'{}')
                return self._attachment_delete(d)
            # ── 页面生命周期：全部关闭后自动退出 ──────────────────
            if path == '/api/page/open':
                d = json.loads(self._read_body() or b'{}')
                pid = str(d.get('id') or '')[:64]
                n = self.app.page_open(pid)
                return self._json({"ok": True, "pages": n,
                                   "auto_exit": self.app.auto_exit,
                                   "grace_s": self.app.auto_exit_grace_s})
            if path == '/api/page/close':
                # sendBeacon 的 body 是 text/plain，id 走 query 最省事
                pid = (qs.get('id') or [''])[0][:64]
                if not pid:
                    try:
                        pid = str(json.loads(self._read_body() or b'{}')
                                  .get('id') or '')[:64]
                    except Exception:
                        pid = ''
                return self._json({"ok": True, "pages": self.app.page_close(pid)})
            if path == '/api/page/alive':
                pid = (qs.get('id') or [''])[0][:64]
                self.app.page_touch(pid)
                return self._json({"ok": True})
            if path == '/api/stop':
                ag = self.app._running
                if ag is None:
                    return self._json({"ok": True, "running": False})
                cid = self._client_id()
                owner = self.app._running_owner or ''
                # 局域网多人：只能停自己发起的那一轮（否则 B 会随手把 A 的任务停了）
                if not owner or not cid or owner != cid:
                    return self._err(403, '任务发起者身份缺失或不匹配，拒绝停止')
                ag.request_stop()
                return self._json({"ok": True, "running": True})
            if path == '/api/extend':
                # 「再等 5 分钟」：延长当前轮的看门狗（不做就没有这轮了，只能重试）
                d = json.loads(self._read_body() or b'{}')
                try:
                    add = max(60, min(3600, int(d.get('seconds') or 300)))
                except (TypeError, ValueError):
                    add = 300
                w = self.app._running_watch
                if not w:
                    return self._json({"ok": True, "extended": False})
                cid = self._client_id()
                owner = self.app._running_owner or ''
                if not owner or not cid or owner != cid:
                    return self._err(403, '任务发起者身份缺失或不匹配，拒绝延长')
                w['deadline'] += add
                log.info('看门狗延长 +%ss（%s）', add, cid or '本机')
                return self._json({"ok": True, "extended": True, "seconds": add})
            if path == '/api/confirm':
                d = json.loads(self._read_body() or b'{}')
                st = self.app.submit_confirm(bool(d.get('approved')), self._client_id())
                if st == 'forbidden':
                    return self._err(403, '只能由发起该任务的浏览器确认')
                return self._json({"ok": True, "applied": st == 'ok'})
            if path == '/api/answer':
                d = json.loads(self._read_body() or b'{}')
                st = self.app.submit_answer(str(d.get('answer') or ''), self._client_id())
                if st == 'forbidden':
                    return self._err(403, '只能由发起该任务的浏览器回答')
                return self._json({"ok": True, "applied": st == 'ok'})
            if path == '/api/artifact/restore':
                # 恢复产物到某个历史版本（恢复前当前版会先存一版，可再反悔）
                d = json.loads(self._read_body() or b'{}')
                rel = str(d.get('path') or '')
                ts = str(d.get('ts') or '')
                if not rel or not ts:
                    return self._err(400, '缺少 path / ts')
                try:
                    r = self.app.ws.restore_version(rel, ts)
                except PermissionError as e:
                    return self._err(400, str(e))
                except FileNotFoundError as e:
                    return self._err(404, str(e))
                except ValueError as e:
                    return self._err(400, str(e))
                log.info('产物版本恢复: %s <- %s', rel, ts)
                return self._json({"ok": True, **r})
            if path == '/api/batch':
                # 创建批量作业（B8）
                d = json.loads(self._read_body() or b'{}')
                try:
                    job = self.app.batch.create(d.get('name'), d.get('template'),
                                                d.get('files'),
                                                bool(d.get('preview')))
                except PermissionError as e:
                    return self._err(400, str(e))
                except FileNotFoundError as e:
                    return self._err(404, str(e))
                except ValueError as e:
                    return self._err(400, str(e))
                log.info('批量作业创建: %s (%d 项)', job['name'], len(job['items']))
                return self._json({"ok": True, "job": self.app.batch._summary(job)})
            if path == '/api/batch/control':
                # 暂停 / 继续 / 取消 / 重试失败项
                d = json.loads(self._read_body() or b'{}')
                jid = str(d.get('id') or '')
                action = str(d.get('action') or '')
                if not valid_job_id(jid):
                    return self._err(400, '非法作业 id')
                try:
                    job = self.app.batch.control(jid, action)
                except PermissionError as e:
                    return self._err(400, str(e))
                except FileNotFoundError as e:
                    return self._err(404, str(e))
                except ValueError as e:
                    return self._err(400, str(e))
                return self._json({"ok": True, "job": self.app.batch._summary(job)})
            if path == '/api/undo':
                d = json.loads(self._read_body() or b'{}')
                sid = str(d.get('session_id') or '')
                turn = str(d.get('turn') or '')
                if not sid or not turn:
                    return self._err(400, '缺少 session_id / turn')
                try:
                    r = self.app.ws.undo_turn(sid, turn)
                except FileNotFoundError as e:
                    return self._err(404, str(e))
                except Exception as e:
                    return self._err(500, f'撤销失败: {e}')
                try:
                    self.app.sessions.mark_turn_undone(sid, turn)
                except Exception as e:
                    log.warning('标记撤销状态失败: %s', e)
                log.info('撤销第 %s 轮: 还原 %s 个 / 删除 %s 个',
                         turn, r.get('restored'), r.get('removed'))
                return self._json({"ok": True, **r})
            if path == '/api/workspace/delete':
                d = json.loads(self._read_body() or b'{}')
                return self._workspace_delete(d.get('path', ''))
            if path == '/api/settings':
                d = json.loads(self._read_body() or b'{}')
                # 工作台目录（D）：只校验 + 写 config，热切换风险大（会话/记忆/RAG/作业
                # 都绑在当前 root 上），所以留到重启生效。
                wsr = str((d.get('workspace') or {}).get('root') or '').strip()
                if wsr:
                    try:
                        cand = Workspace(wsr)
                    except (ValueError, PermissionError) as e:
                        return self._err(400, f'工作台路径无效: {e}')
                    cfg_root = str(((self.app.config.get('workspace') or {})
                                    .get('root') or self.app.ws.root))
                    same_cfg = (os.path.normcase(os.path.abspath(str(cand.root)))
                                == os.path.normcase(os.path.abspath(cfg_root)))
                    if not same_cfg:
                        # 与「已保存值」比，而不是与运行中的 root 比：
                        # 否则改回当前目录时永远写不回去，restart_required 会卡在 true
                        self.app.config.setdefault('workspace', {})['root'] = \
                            str(cand.root)
                        self.app._persist()
                        log.info('工作台目录已改（重启生效）: %s', cand.root)
                self.app.save_config(model=d.get('model'), limits=d.get('limits'),
                                     rag=d.get('rag'), security=d.get('security'),
                                     prompt=d.get('prompt'), memory=d.get('memory'))
                if d.get('mcp') is not None:
                    self.app.set_mcp_enabled(d.get('mcp') or [])
                return self._json(self._settings())
            if path == '/api/mcp/reload':
                # 手动重连某个 MCP server（B5）
                d = json.loads(self._read_body() or b'{}')
                from agent.mcp import manager as _mcp_mgr
                ok = _mcp_mgr.restart(str(d.get('name') or ''))
                return self._json({'ok': ok, 'mcp': _mcp_status()})
            if path == '/api/mcp/resource':
                # 读取 MCP resource 内容（B5 补：resources/read）
                d = json.loads(self._read_body() or b'{}')
                from agent.mcp import McpError, manager as _mcp_mgr
                try:
                    txt = _mcp_mgr.read_resource(str(d.get('server') or ''),
                                                 str(d.get('uri') or ''))
                except McpError as e:
                    return self._err(400, str(e))
                return self._json({'ok': True, 'text': txt})
            if path == '/api/mcp/save':
                # 设置页新增/编辑 MCP server（无需手改 config.json）
                d = json.loads(self._read_body() or b'{}')
                srv = d.get('server') or d
                try:
                    servers = self.app.save_mcp_server(
                        srv, str(d.get('original_name') or ''))
                except ValueError as e:
                    return self._err(400, str(e))
                return self._json({"ok": True, "mcp": _mcp_status(),
                                   "servers": servers})
            if path == '/api/mcp/delete':
                d = json.loads(self._read_body() or b'{}')
                try:
                    servers = self.app.delete_mcp_server(str(d.get('name') or ''))
                except ValueError as e:
                    return self._err(400, str(e))
                return self._json({"ok": True, "mcp": _mcp_status(),
                                   "servers": servers})
            if path == '/api/compact':
                # 手动压缩本会话历史（长会话主动释放上下文）
                return self._compact_session()
            if path == '/api/mcp/prompt':
                # 取 MCP prompt 文本（B5 补：prompts/get），前端一键填进输入框
                d = json.loads(self._read_body() or b'{}')
                from agent.mcp import McpError, manager as _mcp_mgr
                try:
                    txt = _mcp_mgr.get_prompt(str(d.get('server') or ''),
                                              str(d.get('name') or ''))
                except McpError as e:
                    return self._err(400, str(e))
                return self._json({'ok': True, 'text': txt})
            if path == '/api/project':
                d = json.loads(self._read_body() or b'{}')
                return self._save_project_file(str(d.get('rel') or ''), str(d.get('content') or ''))
            if path == '/api/templates':
                d = json.loads(self._read_body(limit=1024 * 1024) or b'{}')
                self.app.save_templates(d.get('templates') or [])
                return self._json({"ok": True, "templates": self.app.load_templates()})
            if path == '/api/test_llm':
                return self._test_llm()
            if path == '/api/rag/index':
                return self._json(self.app.rag.refresh())
            if path == '/api/memory/delete':
                d = json.loads(self._read_body() or b'{}')
                self.app.memory.delete_fact(int(d.get('id', 0)))
                return self._json({"ok": True})
            if path == '/api/memory/update':
                d = json.loads(self._read_body() or b'{}')
                self.app.memory.update_fact(int(d.get('id', 0)), d.get('content', ''))
                return self._json({"ok": True})
            if path == '/api/memory/important':
                d = json.loads(self._read_body() or b'{}')
                self.app.memory.set_important(int(d.get('id', 0)), bool(d.get('important')))
                return self._json({"ok": True})
            if path == '/api/memory/tags':
                d = json.loads(self._read_body() or b'{}')
                tags = d.get('tags')
                if tags is None:
                    self.app.memory.add_tag(int(d.get('id', 0)), d.get('tag', ''))
                else:
                    self.app.memory.set_tags(int(d.get('id', 0)), tags)
                return self._json({"ok": True})
            if path == '/api/memory/add':
                d = json.loads(self._read_body() or b'{}')
                self.app.memory.save_fact(d.get('content', ''), source=d.get('source', 'manual'),
                                          important=bool(d.get('important')))
                return self._json({"ok": True})
            if path == '/api/memory/pref':
                d = json.loads(self._read_body() or b'{}')
                self.app.memory.save_pref(d.get('key', ''), d.get('value', ''))
                return self._json({"ok": True})
            return self._err(404, 'not found')
        except Exception as e:
            self._err(500, f'{type(e).__name__}: {e}')

    # ── 工作台目录浏览/删除 ──────────────────────────────────────
    def _workspace_tree(self):
        """返回 inputs/outputs/notes 目录树（排除隐藏/系统目录）。"""
        root = self.app.ws.root

        def walk(p, rel):
            items = []
            try:
                names = sorted(os.listdir(p), key=lambda x: (not (p / x).is_dir(), x.lower()))
            except PermissionError:
                return items
            for name in names:
                if name.startswith('.'):
                    continue
                fp = p / name
                r = f"{rel}/{name}" if rel else name
                try:
                    if fp.is_symlink():
                        continue
                    if fp.is_dir():
                        items.append({"type": "dir", "name": name, "path": r,
                                      "children": walk(fp, r)})
                    else:
                        st = fp.stat()
                        items.append({"type": "file", "name": name, "path": r,
                                      "size": st.st_size, "mtime": int(st.st_mtime)})
                except Exception:
                    continue
            return items

        tree = []
        for top in ('inputs', 'outputs', 'notes'):
            p = root / top
            if p.is_symlink():
                continue
            if p.is_dir():
                tree.append({"type": "dir", "name": top, "path": top,
                             "children": walk(p, top)})
        return tree

    def _workspace_delete(self, rel: str):
        """删除工作台内文件/空目录。仅限 inputs/outputs/notes，系统目录不可删。"""
        rel = str(rel).replace('\\', '/').strip()
        if not rel or rel.startswith('/') or '..' in rel.split('/'):
            return self._err(400, '非法路径')
        top = rel.split('/')[0]
        if top not in ('inputs', 'outputs', 'notes'):
            return self._err(403, '只能删除 inputs/outputs/notes 下的内容')
        try:
            candidate = self.app.ws.root / rel
            self.app.ws._reject_symlinks(candidate)
            p = candidate.resolve()
            p.relative_to(self.app.ws.root.resolve())
        except Exception:
            return self._err(400, '路径必须在工作台内且不能包含符号链接')
        if not p.exists():
            return self._err(404, '文件不存在')
        try:
            if p.is_dir():
                if any(p.iterdir()):
                    return self._err(400, '目录非空，请先清空后再删')
                p.rmdir()
            else:
                p.unlink()
        except Exception as e:
            return self._err(500, f'删除失败: {e}')
        return self._json({"ok": True})

    # ── 上传 ─────────────────────────────────────────────────────
    def _handle_upload(self, qs):
        filename = (qs.get('filename') or [''])[0]
        if not filename:
            return self._err(400, '缺少 filename 参数')
        try:
            data = self._read_body(limit=int(self.app.max_upload_mb * 1024 * 1024))
        except ValueError:
            return self._err(413, f'文件超过大小限制 {self.app.max_upload_mb}MB')
        p = self.app.ws.upload_path(filename)
        p.write_bytes(data)
        rel = str(p.relative_to(self.app.ws.root)).replace('\\', '/')
        # 上传后后台增量索引（RAG 开启时）：不阻塞上传响应，下一次提问即可检索到新文件
        try:
            if (self.app.config.get('rag') or {}).get('enabled'):
                threading.Thread(target=self.app.rag.refresh, daemon=True).start()
        except Exception as e:
            log.debug('上传后增量索引失败: %s', e)
        return self._json({"ok": True, "path": rel, "name": p.name, "size": len(data)})

    # ── 附件删除 ────────────────────────────────────────────────
    def _attachment_delete(self, d: dict):
        """POST /api/attachment/delete：移除消息引用 + 回收无引用文件。"""
        sid = str(d.get('session_id') or '')
        try:
            ts = float(d.get('ts') or 0)
        except (TypeError, ValueError):
            ts = 0
        path = str(d.get('path') or '').replace('\\', '/').strip()
        if not sid or not ts or not path:
            return self._err(400, '缺少参数（session_id/ts/path）')
        err = _delete_attachment_ref(self.app, sid, ts, path)
        if err:
            return self._err(404, err)
        return self._json({"ok": True})

    # ── SSE 聊天 ─────────────────────────────────────────────────
    def _build_user_input(self, message: str, valid_att: list):
        """拼装发给模型的用户输入（含附件说明；多模态时返回 parts 列表）。"""
        text = message
        if valid_att:
            lines = ["\n\n【用户本次上传的附件】请按需读取这些文件来回答（工作台内相对路径）："]
            lines += [f"- {a['path']}" for a in valid_att]
            text = message + "\n".join(lines)
        vision = bool((self.app.config.get('model', {}) or {}).get('vision', False))
        if not vision:
            return text
        imgs = [a for a in valid_att
                if str(a.get('path', '')).lower().rsplit('.', 1)[-1]
                in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp')]
        if not imgs:
            return text
        parts = [{"type": "text", "text": text}]
        for a in imgs[:4]:
            try:
                p = (self.app.ws.root / a['path']).resolve()
                p.relative_to(self.app.ws.root.resolve())
                if p.stat().st_size > 8 * 1024 * 1024:
                    continue
                import base64
                ext = p.suffix.lower().lstrip('.')
                mime = 'image/png' if ext == 'png' else 'image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}'
                b64 = base64.b64encode(p.read_bytes()).decode('ascii')
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{mime};base64,{b64}"}})
            except Exception:
                continue
        return parts if len(parts) > 1 else text

    def _handle_chat(self):
        app = self.app
        self._sse_started = False
        token = object()          # 本请求的 busy 持有凭证（防止误放别人的锁）
        released = {'v': False}

        def release_busy():
            if released['v']:
                return
            released['v'] = True
            with app._busy_lock:
                if app._busy_owner is token:
                    app._busy_owner = None
                    app._busy = False

        try:
            d = json.loads(self._read_body() or b'{}')
            message = (d.get('message') or '').strip()
            mode = d.get('mode') or 'craft'
            regenerate = bool(d.get('regenerate'))
            if not message:
                return self._err(400, '消息为空')

            # 附件：前端随消息带上用户已上传的文件（inputs/ 等工作台内路径）
            # 校验只放行工作台内真实存在的普通文件（防越界/防敏感文件），
            # 然后注入到发给模型的消息上下文里，让 AI 知道并读取附件。
            valid_att = _validate_attachments(app.ws, d.get('attachments'))

            sid = d.get('session_id') or ''
            if sid:
                try:
                    session = app.sessions.load(sid)
                except Exception:
                    return self._err(400, '会话不存在')
            else:
                session = app.sessions.create(app._auto_title(message))
            sid = session.id
            # 会话标题自动更新：新会话（无消息、默认标题）首条消息 -> 取消息前 20 字。
            # 已有内容/用户手动命名过的会话不覆盖。
            if not session.messages and session.title in ('新会话', ''):
                new_title = app._auto_title(message)
                app.sessions.rename(sid, new_title)
                session.title = new_title
            app._current_sid = sid

            user_input = self._build_user_input(message, valid_att)
            history = self._session_history(session)

            # 先落盘用户消息：即使随后断流/刷新/崩溃，这一轮提问也不会丢
            if not regenerate:
                user_extra = {'attachments': valid_att} if valid_att else None
                app.sessions.add_message(sid, 'user', message, extra=user_extra)

            # SSE 头
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            # SSE 流结束即断开，前端 fetch reader 收到 done 才正常收尾
            self.send_header('Connection', 'close')
            self.end_headers()
            self._sse_started = True

            send_lock = threading.Lock()

            def send(kind, **data):
                if kind not in TURN_EVENTS:
                    # 开发期显眼提示；生产不因一个坏帧打死整轮对话
                    log.error('未登记的 SSE 事件类型: %s', kind)
                payload = json.dumps({"type": kind, **data}, ensure_ascii=False)
                with send_lock:
                    try:
                        self.wfile.write(f"data: {payload}\n\n".encode('utf-8'))
                        self.wfile.flush()
                    except Exception:
                        pass

            def raw_send(blob: bytes):
                with send_lock:
                    try:
                        self.wfile.write(blob)
                        self.wfile.flush()
                    except Exception:
                        pass

            # ── 排队：单任务串行，后到者先推 queued 帧再等待 ──────
            app._queue.append(self)
            if len(app._queue) > 1 or app.busy:
                raw_send(f"data: {json.dumps({'type': 'queued', 'position': len(app._queue)}, ensure_ascii=False)}\n\n".encode('utf-8'))
            wait_t0 = time.monotonic()
            wait_tick = 0
            while app.busy or app._queue[0] is not self:
                if time.monotonic() - wait_t0 > app.chat_timeout_s:
                    try:
                        app._queue.remove(self)
                    except ValueError:
                        pass
                    raw_send(f"data: {json.dumps({'type': 'error', 'message': '排队超时，已取消。请重试。'}, ensure_ascii=False)}\n\n".encode('utf-8'))
                    return
                # 死连接感知：客户端已断开则退出排队，不挂满超时时间占线程
                wait_tick += 1
                if wait_tick % 4 == 0 and not self._client_alive():
                    try:
                        app._queue.remove(self)
                    except ValueError:
                        pass
                    return
                time.sleep(0.5)
            with app._busy_lock:
                app._busy = True
                app._busy_owner = token
                try:
                    app._queue.remove(self)
                except ValueError:
                    pass

            # ── 每个请求一个独立 Agent（回调各自绑定本连接，不串会话）──
            ctx = app.sessions.session_ctx(session)
            agent, box = app.make_agent(ctx, send, model=d.get('model'))
            box.alive = self._client_alive          # 提问时感知断开，不空等 180s
            box.owner = self._client_id()           # 局域网：只有发起者能确认/回答/停止
            app._running = agent
            app._running_sid = sid                  # 断流续看：供 /api/jobs 报告“仍在运行”
            app._running_owner = box.owner
            app._run_started = time.time()
            agent.reset_stop()
            agent.on_delta = lambda t: send('delta', text=t)
            agent.on_tool = lambda n, a, ok, r, arts, ms=None: send(
                'tool', name=n, args=a, ok=ok, result=r, artifacts=arts, ms=ms)
            agent.on_tool_start = lambda n, a: send('tool_start', name=n, args=a)
            agent.on_todo = lambda items: send('todo', items=items)
            # thinking=hide 时不推 think 帧（前端不显示思考块）；collapse/keep 正常推送
            if app.config.get('model', {}).get('thinking', 'collapse') != 'hide':
                agent.on_think = lambda t: send('think', text=t)
            else:
                agent.on_think = None

            result_box = {}
            run_t0 = time.monotonic()

            def run_agent():
                try:
                    result_box['r'] = agent.run(history, user_input, mode=mode)
                    result_box['ok'] = True
                except LLMError as e:
                    result_box['e'] = str(e)
                    log.warning('对话失败(LLM): %s', e)
                except Exception as e:
                    result_box['e'] = f'{type(e).__name__}: {e}'
                    log.exception('对话失败: %s', e)
                finally:
                    release_busy()
                    r = result_box.get('r')
                    app._jobs.insert(0, {
                        'message': message[:200],
                        'ok': 'r' in result_box,
                        'response': (r.get('response') if r else '')[:200],
                        'elapsed_s': round(time.monotonic() - run_t0, 1),
                        'done_at': time.strftime('%H:%M:%S'),
                    })
                    del app._jobs[20:]

            t = threading.Thread(target=run_agent, daemon=True)
            t.start()

            # 等待 + 看门狗（周期性心跳保活，防止长任务连接被中间设备掐断）
            t0 = time.monotonic()
            heart = 0
            warned = False
            watch = {'deadline': t0 + app.chat_timeout_s}
            app._running_watch = watch
            while not result_box and time.monotonic() < watch['deadline']:
                t.join(0.5)
                heart += 1
                if heart % 20 == 0:
                    raw_send(b': ping\n\n')
                # 快到时先问一声：离线大模型的长任务很常见，给用户「再等 5 分钟」的机会
                if (not warned and app.chat_timeout_s >= 120
                        and time.monotonic() > watch['deadline'] - app.chat_timeout_s * 0.2):
                    warned = True
                    send('warn',
                         message=(f'本轮已运行 {int(time.monotonic() - t0)} 秒，'
                                  f'接近 {int(app.chat_timeout_s)} 秒上限；'
                                  f'内网大模型的长任务可点「再等 5 分钟」。'),
                         extendable=True)
            if not result_box:
                agent.request_stop()
                send('error',
                     message=(f'任务执行超时（{int(app.chat_timeout_s)} 秒），已强制终止。'
                              f'可在 设置 → 运行 调大「单轮超时」后重试。'),
                     limit_s=int(app.chat_timeout_s))
                t.join(2)
                return

            app._running = None
            app._running_sid = ''
            app._running_owner = ''
            app._running_watch = None

            if 'e' in result_box:
                send('error', message=result_box['e'])
                return
            r = result_box.get('r')
            if not r:
                return

            # 持久化：回复 + 产物 + 思考过程 + 本轮快照（可撤销）。
            # 用户消息已在开跑前落盘；重新生成（regenerate）时不重复记录。
            extra = {"tool_steps": r['tool_steps'], "artifacts": r['artifacts']}
            if r.get('reasoning'):
                extra['thinking'] = r['reasoning']
            if r.get('snapshot'):
                extra['snapshot'] = r['snapshot']
            if r.get('todos'):
                extra['todos'] = r['todos']
            if r.get('context'):
                extra['context'] = r['context']
            if r.get('citations'):
                extra['citations'] = r['citations']
            app.sessions.add_message(sid, 'assistant', r['response'], extra=extra)
            app.sessions.register_artifacts(sid, r['artifacts'])
            app.sessions.write_work_log(sid, message, r['tool_steps'], r['response'])
            # 后台记忆提取（独立线程；LLMClient 已是线程安全，不会干扰前台）
            # 离线单卡/单槽模型下这次提取要再占一次推理，所以：
            #   ① 用户关了「自动提取记忆」就不跑；② 会话太短（<6 条）没东西可提；
            #   ③ 上一次提取还在跑就跳过（避免排队抢槽）；④ 批量作业期间不提取。
            try:
                full = app.sessions.load(sid)
                app.maybe_extract_memory(sid, full)
            except Exception:
                pass

            send('result', response=r['response'], stopped=r['stopped'],
                 artifacts=app._artifact_metas(r['artifacts']), session_id=sid,
                 elapsed_s=round(time.monotonic() - t0, 1),
                 usage=r.get('usage') or {},
                 context=r.get('context') or {},
                 todos=r.get('todos') or [],
                 citations=r.get('citations') or [],
                 snapshot=r.get('snapshot'))
        except Exception as e:
            log.exception('聊天处理异常: %s', e)
            if self._sse_started:
                # SSE 头已发出，再写 HTTP 头会污染流 -> 只能发错误帧
                # detail：堆栈尾部（本地单机工具，供“查看详情”展开排查）
                try:
                    import traceback as _tb
                    _detail = _tb.format_exc()[-4000:]
                except Exception:
                    _detail = ''
                try:
                    self.wfile.write(f"data: {json.dumps({'type': 'error', 'message': f'{type(e).__name__}: {e}', 'detail': _detail}, ensure_ascii=False)}\n\n".encode('utf-8'))
                    self.wfile.flush()
                except Exception:
                    pass
            else:
                self._err(500, f'{type(e).__name__}: {e}')
        finally:
            release_busy()
            app._running = None
            app._running_sid = ''
            app._running_owner = ''
            app._running_watch = None
            app._pending_confirm = None
            try:
                app._queue.remove(self)
            except ValueError:
                pass

    def log_message(self, fmt, *args):
        pass  # 静默访问日志，避免刷屏


# ── 设置/测试 ───────────────────────────────────────────────
    # ── 会话历史（含手动压缩点）──────────────────────────────────
    @staticmethod
    def _session_history(session) -> list:
        """给模型的历史：手动压缩点之前的只发摘要，完整对话仍在前端展示。"""
        msgs = session.messages or []
        cp = session.compact or {}
        upto = int(cp.get('upto') or 0)
        summary = str(cp.get('summary') or '').strip()
        if summary and 0 < upto <= len(msgs):
            kept = [m for m in msgs[upto:]
                    if m.get('role') in ('user', 'assistant')]
            return [{"role": "user",
                     "content": "【早前对话已压缩·以下摘要概括了更早的内容】\n"
                                + summary}] + kept
        return [m for m in msgs if m.get('role') in ('user', 'assistant')]

    def _compact_session(self):
        """POST /api/compact：手动把本会话早期历史压成摘要。

        - 不动 messages.json（用户仍能看到完整对话，可导出/搜索）
        - 只写 compact.json，下一轮起按「摘要 + 最近 N 条」发绡模型
        - clear=true 则撤销压缩
        """
        d = json.loads(self._read_body() or b'{}')
        sid = str(d.get('session_id') or '')
        if not sid:
            return self._err(400, '缺少 session_id')
        try:
            s = self.app.sessions.load(sid)
        except Exception:
            return self._err(404, '会话不存在')
        if d.get('clear'):
            had = self.app.sessions.clear_compact(sid)
            log.info('撤销手动压缩: %s（%s）', sid, '原本有' if had else '本来就没有')
            return self._json({"ok": True, "cleared": had, "compact": None})
        if self.app.busy and self.app._running_sid == sid:
            return self._err(400, '任务正在运行，结束后再压缩')
        history = [m for m in s.messages if m.get('role') in ('user', 'assistant')]
        if len(history) < 4:
            return self._err(400, '会话太短，至少 4 条消息才需要压缩')
        keep = d.get('keep_recent')
        keep = max(2, min(20, int(keep or 6)))
        # 历史不够长时收缩保留条数：否则 4~7 条的会话压缩点为空，用户点了只会收到 400
        keep = min(keep, max(2, len(history) - 2))
        prefix, recent, cut = compact_history(history, keep)
        summary = '\n\n'.join(str(m.get('content') or '') for m in prefix).strip()
        if not summary:
            return self._err(400, '没有可压缩的内容（都是工具记录）')
        # 压缩点换算回“原始 messages 下标”，之后追新消息也不会错位
        raw_idx = [i for i, m in enumerate(s.messages)
                   if m.get('role') in ('user', 'assistant')]
        upto = raw_idx[cut] if cut < len(raw_idx) else len(s.messages)
        before_chars = sum(len(str(m.get('content') or '')) for m in history)
        after_view = [{"role": "user", "content": summary}] + recent
        after_chars = sum(len(str(m.get('content') or '')) for m in after_view)
        stats = {
            "before_msgs": len(history), "after_msgs": len(after_view),
            "before_chars": before_chars, "after_chars": after_chars,
            "est_tokens": estimate_messages(after_view),
            # 一并落盘：刷新页面后按钮标题仍能显示省了多少
            "saved_pct": (round((1 - after_chars / before_chars) * 100)
                          if before_chars else 0),
        }
        rec = self.app.sessions.set_compact(sid, summary, upto, stats)
        rec.update(stats)
        log.info('手动压缩: %s %d 条 -> %d 条（%d -> %d 字符，省 %s%%）',
                 sid, stats['before_msgs'], stats['after_msgs'],
                 before_chars, after_chars, rec['saved_pct'])
        return self._json({"ok": True, "compact": rec})

    def _settings(self):
        m = self.app.config.get('model', {}) or {}
        limits = self.app.config.get('limits', {}) or {}
        rag = self.app.config.get('rag', {}) or {}
        sec = self.app.config.get('security', {}) or {}
        key = m.get('api_key') or ''
        try:
            rag_stats = self.app.rag.stats()
        except Exception:
            rag_stats = {}
        return {
            "model": {
                "name": m.get('name', ''),
                "base_url": m.get('base_url', ''),
                "key_set": bool(self.app._api_key_plain or key),
                "key_masked": '已设置' if (self.app._api_key_plain or key) else '未设置',
                "thinking": m.get('thinking', 'collapse'),
                "max_tokens": int(m.get('max_tokens', 0) or 0),
                "context_window": int(m.get('context_window', 0) or 0),
                "vision": bool(m.get('vision', False)),
            },
            "security": {
                "allow_code_exec": bool(sec.get('allow_code_exec', True)),
                "profile": str(sec.get('profile') or 'suggest'),
                "auto_approve": {
                    "write_outputs": bool((sec.get('auto_approve') or {}).get('write_outputs', True)),
                    "write_notes": bool((sec.get('auto_approve') or {}).get('write_notes', True)),
                    "delete_move": bool((sec.get('auto_approve') or {}).get('delete_move', False)),
                    "run_other_cmds": bool((sec.get('auto_approve') or {}).get('run_other_cmds', False)),
                },
                "session_allow": list(self.app.session_allow),
            },
            "mcp": _mcp_status(),
            "limits": {
                "max_upload_mb": self.app.max_upload_mb,
                "chat_timeout_s": self.app.chat_timeout_s,
                "compress_threshold_chars": limits.get('compress_threshold_chars', 0),
                "llm_timeout_s": float(limits.get('llm_timeout_s', 300) or 300),
                "max_turns": int(limits.get('max_turns', 25) or 25),
            },                "rag": {
                    "enabled": bool(rag.get('enabled', True)),
                    "top_k": int(rag.get('top_k', 4) or 4),
                    "stats": rag_stats,
                },
                "memory": {
                    "extract": bool((self.app.config.get('memory') or {})
                                   .get('extract', True)),
                },
            "prompt": {
                "role": self.app.agent_role,
                "rules": self.app.agent_rules,
            },
            "workspace": _workspace_status(self.app),
            "server": {k: v for k, v in (self.app.config.get('server', {}) or {}).items()
                       if k != 'token'},
            "is_loopback": self._is_loopback(),
            "auth_required": self.app.auth_required,
            "token_file": str(self.app.ws.root / '.agent' / 'access_token')
            if self.app.auth_required else '',
        }

    # ── 项目指令（AGENTS.md）────────────────────────────────────
    _PROJECT_FILES = ('AGENTS.md', 'notes/AGENTS.md', '.agent/rules.md')

    def _project_files(self):
        out = []
        for rel in self._PROJECT_FILES:
            p = (self.app.ws.root / rel)
            try:
                txt = p.read_text(encoding='utf-8', errors='replace') if p.is_file() else ''
            except OSError:
                txt = ''
            out.append({"rel": rel, "content": txt})
        return out

    def _save_project_file(self, rel: str, content: str):
        if rel not in self._PROJECT_FILES:
            return self._err(400, '不支持的项目指令文件')
        if len(content) > 20000:
            return self._err(400, '项目指令过长（上限 20000 字符）')
        p = self.app.ws.root / rel
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix='.agents.', suffix='.tmp')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            os.replace(tmp, p)
        except OSError as e:
            return self._err(500, f'保存失败: {e}')
        log.info('项目指令已保存: %s（%d 字符）', rel, len(content))
        return self._json({"ok": True, "rel": rel, "chars": len(content)})

    def _test_llm(self):
        d = json.loads(self._read_body(limit=64 * 1024) or b'{}')
        if not d.get('base_url') or not d.get('model'):
            return self._err(400, '缺少 base_url 或 model')
        # SSRF 纵深：base_url 完全取自请求体。本地/局域网模型地址是本功能的
        # 正常用途（示例配置就是 127.0.0.1），回环与私网不拦，边界靠访问令牌；
        # 但云元数据（169.254/16）与不可路由地址一律不许探。http.client 不跟
        # 随重定向，解析到的 IP 即连接目标；DNS 在检查与连接之间变化的
        # TOCTOU 残留已注明（要根治需在连接层校验，见 _check_probe_url）。
        try:
            self._check_probe_url(str(d['base_url']))
        except ValueError as e:
            return self._err(400, str(e))
        test = LLMClient(d['base_url'], d.get('api_key', ''), d['model'], timeout=30)
        t0 = time.monotonic()
        try:
            r = test.chat([{"role": "user", "content": "回复 ping 即可"}])
            return self._json({"ok": True,
                               "elapsed_s": round(time.monotonic() - t0, 2),
                               "content": (r.get('content') or '')[:120]})
        except LLMError as e:
            return self._json({"ok": False, "error": str(e)}, 502)
        except Exception as e:
            return self._json({"ok": False, "error": f'{type(e).__name__}: {e}'}, 502)

    @staticmethod
    def _check_probe_url(url: str):
        """校验 _test_llm 的探测目标：只放行 http/https，且解析到的任一
        地址不得是未指定/链路本地（含云元数据 169.254/16）/组播/保留段
        或 0.0.0.0/8；解析失败直接拒绝（fail-closed）。

        回环与私网地址有意放行：本地与局域网模型是本功能的正常用途，
        其边界由访问令牌守。残留风险：DNS 在本检查与实际连接之间变化
        （TOCTOU），根治需在连接层对解析结果校验，本轮未做。
        """
        u = urlparse(url)
        if u.scheme not in ('http', 'https'):
            raise ValueError('base_url 只允许 http/https')
        host = (u.hostname or '').strip()
        if not host:
            raise ValueError('base_url 缺少主机名')
        port = u.port or (443 if u.scheme == 'https' else 80)
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError as e:
            raise ValueError(f'无法解析主机名 {host}（{e}）')
        blocked = []
        for _fam, _typ, _proto, _canon, sockaddr in infos:
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            # 回环先放行：CPython 里 ::1 的 is_reserved 为真（::/8 保留段），
            # 若先判保留段会把 IPv6 回环误杀；回环本就是允许的本地模型地址。
            if ip.is_loopback:
                continue
            if (ip.is_unspecified or ip.is_link_local or ip.is_multicast
                    or ip.is_reserved
                    or (isinstance(ip, ipaddress.IPv4Address)
                        and ip in ipaddress.ip_network('0.0.0.0/8'))):
                blocked.append(str(ip))
        if blocked:
            raise ValueError(f'base_url 解析到不可探测的地址：{", ".join(blocked)}')

    def _dir_listing(self, qs: dict) -> dict:
        """文件树（懒加载，一次一层）。root: ws=工作台 home=用户目录 drive=磁盘根。"""
        root_kind = (qs.get('root') or ['ws'])[0]
        rel = (qs.get('path') or [''])[0]
        if root_kind == 'home':
            root = os.path.expanduser('~')
        elif root_kind == 'drive':
            root = os.path.abspath(os.sep)
        else:
            root = self.app.ws.root
        try:
            base = Path(root)
            candidate = base / rel
            if Path(rel).is_absolute() or '..' in Path(rel).parts:
                raise ValueError('非法相对路径')
            current = base
            for part in Path(rel).parts:
                if part in ('', '.'):
                    continue
                current = current / part
                if current.is_symlink():
                    raise ValueError('不允许通过符号链接浏览')
            p = candidate.resolve()
            p.relative_to(base.resolve())
        except Exception:
            return {"error": '非法路径', "root": str(root), "path": rel}
        if not p.is_dir():
            return {"root": str(root), "path": rel, "items": []}
        items = []
        try:
            names = sorted(os.listdir(p), key=lambda x: (not (p / x).is_dir(), x.lower()))
        except OSError:
            names = []
        for name in names:
            if name.startswith('.'):
                continue
            fp = p / name
            try:
                if fp.is_symlink():
                    continue
                if fp.is_dir():
                    items.append({"type": "dir", "name": name})
                else:
                    st = fp.stat()
                    items.append({"type": "file", "name": name, "size": st.st_size,
                                  "mtime": int(st.st_mtime)})
            except Exception:
                pass
        return {"root": str(root), "path": rel, "items": items}

    # ── 导出/备份 ───────────────────────────────────────────────
    def _export_session(self, sid: str):
        try:
            s = self.app.sessions.load(sid)
            md = self.app.sessions.export_markdown(sid)
            body = md.encode('utf-8')
            from urllib.parse import quote as _quote
            fname = f"{s.title or '会话'}.md"
            self.send_response(200)
            self.send_header('Content-Type', 'text/markdown; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Disposition',
                             f"attachment; filename*=UTF-8''{_quote(fname)}")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._err(500, f'{type(e).__name__}: {e}')

    def _save_note(self, d: dict):
        """把 AI 回答一键存为笔记：notes/<日期>/<文件名>.md（复用写白名单目录）。"""
        content = (d.get('content') or '').strip()
        name = str(d.get('name') or '').strip() or '笔记'
        if not content:
            return self._err(400, '内容为空')
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name)[:60]
        date = time.strftime('%Y-%m-%d')
        dpath = self.app.ws.root / 'notes' / date
        try:
            dpath.mkdir(parents=True, exist_ok=True)
        except OSError:
            return self._err(500, 'notes 目录创建失败')
        p = dpath / (name if name.endswith('.md') else name + '.md')
        n = 1
        while p.exists():
            p = dpath / f"{name}_{n}.md"
            n += 1
        import tempfile as _tmp
        fd, tmp = _tmp.mkstemp(dir=str(dpath), prefix=p.name + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(f"# {name}\n\n{content}\n")
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return self._err(500, '笔记写入失败')
        rel = str(p.relative_to(self.app.ws.root)).replace('\\', '/')
        return self._json({"ok": True, "path": rel, "name": p.name})

    def _tmp_zip_path(self):
        """临时 zip 路径（.agent/tmp）：先写盘再流式发，不把整个包留在内存里。"""
        d = self.app.ws.root / '.agent' / 'tmp'
        d.mkdir(parents=True, exist_ok=True)
        return d / f'zip-{secrets.token_hex(6)}.zip'

    def _send_zip(self, tmp_path, fname: str):
        """流式发送临时 zip，无论成败都删除（几百 MB 的工作台备份不再爆内存）。"""
        from urllib.parse import quote as _quote
        try:
            size = tmp_path.stat().st_size
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Disposition',
                             f"attachment; filename*=UTF-8''{_quote(fname)}")
            self.end_headers()
            with open(tmp_path, 'rb') as f:
                while True:
                    chunk = f.read(self._CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, ValueError) as e:
            log.warning('zip 下载中断: %s', e)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return None

    def _export_artifacts_zip(self, sid: str):
        """把本会话全部产出物打包为 zip 下载（写临时文件后流式发送）。"""
        import zipfile
        try:
            s = self.app.sessions.load(sid)
        except Exception:
            return self._err(404, '会话不存在')
        root = self.app.ws.root
        tmp = self._tmp_zip_path()
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
                for rel in s.artifacts:
                    try:
                        p = (root / rel).resolve()
                        p.relative_to(root.resolve())
                        if p.is_file():
                            z.write(p, str(rel).replace('/', os.sep))
                    except Exception:
                        continue
        except OSError as e:
            return self._err(500, f'打包失败: {e}')
        return self._send_zip(tmp, f"{s.title or '会话'}-产出物.zip")

    def _backup_zip(self):
        import zipfile
        root = self.app.ws.root
        tmp = self._tmp_zip_path()
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
                for dirpath, _dirs, files in os.walk(root):
                    for name in files:
                        fp = os.path.join(dirpath, name)
                        rel = os.path.relpath(fp, root).replace('\\', '/')
                        try:
                            z.write(fp, rel)
                        except Exception:
                            continue
        except OSError as e:
            return self._err(500, f'备份失败: {e}')
        return self._send_zip(tmp, 'workbench-backup.zip')


class AgentHTTPServer(ThreadingHTTPServer):
    """端口独占的 HTTP 服务。

    Windows 上 SO_REUSEADDR 的语义是「允许抢占已绑定的端口」，不是 Unix 的
    TIME_WAIT 复用——开着它会让第二个实例静默绑定成功，两个进程抢同一个端口，
    用户看到的是「刷新后一会儿新版本一会儿旧版本」。所以 NT 上必须关掉。
    """
    allow_reuse_address = (os.name != 'nt')
    allow_reuse_port = False
    daemon_threads = True


def serve(config: dict, host: str = None, port: int = None, config_path: str = None):
    app = AgentServer(config, config_path=config_path)
    bind_host = host or config.get('server', {}).get('host', '127.0.0.1')
    bind_port = port or int(config.get('server', {}).get('port', 8080))

    # 端口冲突时自动尝试递增端口（+1..+19），而不是直接崩溃
    # WinError 10048 = 地址已被使用 / WinError 10013 = 权限拒绝（Hyper-V 端口保留）
    tried = []
    last_err = None
    for p in range(bind_port, bind_port + 20):
        try:
            server = AgentHTTPServer((bind_host, p), Handler)
            server.agent_server = app
            app._httpd = server
            app.start_exit_watchdog()
            if p != bind_port:
                print(f"[WARN] 端口 {bind_port} 被占用，已改用 {p}")
            _print_banner(app, bind_host, p)
            return server
        except OSError as e:
            last_err = e
            tried.append(p)
            continue
    raise RuntimeError(
        f"端口 {bind_port}-{bind_port + 19} 全部不可用（最后一次错误：{last_err}）。"
        f"请用 --port 指定其他端口，或关闭占用端口的程序。已尝试：{tried}")


def _print_banner(app, host: str, port: int):
    lan = host not in ('127.0.0.1', 'localhost', '::1')
    local = f"http://{'127.0.0.1' if lan else host}:{port}/"
    print("─" * 58)
    print(f"  工作台已启动   {local}")
    if lan:
        import socket as _s
        ips = []
        try:
            for info in _s.getaddrinfo(_s.gethostname(), None, _s.AF_INET):
                ip = info[4][0]
                if ip not in ips and not ip.startswith('127.'):
                    ips.append(ip)
        except Exception:
            pass
        for ip in ips:
            print(f"  局域网访问     http://{ip}:{port}/")
        if app.auth_required:
            print(f"  访问令牌       {app.access_token}")
            print(f"  （也保存在 {app.ws.root / '.agent' / 'access_token'}）")
    print(f"  工作台目录     {app.ws.root}")
    print(f"  模型           {app.config.get('model', {}).get('name', '')} @ "
          f"{app.config.get('model', {}).get('base_url', '')}")
    print("─" * 58)
