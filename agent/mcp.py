# -*- coding: utf-8 -*-
"""MCP 客户端（B5）：stdio 上的 JSON-RPC，纯标准库实现。

对离线内网要冷静：MCP server 多是 Node/Python 包，机器上要预置运行时与依赖。
因此这里只做最小实现：

- 配置 `mcp.servers: [{name, command, args, env, enabled, timeout}]`，按需拉起子进程；
- JSON-RPC 2.0 over stdio（按行分隔）：initialize -> tools/list -> tools/call；
- 工具名 `mcp__<server>__<tool>`，注册进 ToolRegistry，**默认逐条走审批**（见 tools.py）；
- 超时 / 进程退出自动重启一次 / 结果截断 / 不支持的 content 类型降级为文本。

没有任何第三方依赖；连接失败只影响对应 server，不影响工作台启动。
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque

from agent.logsetup import get

log = get('mcp')

PROTOCOL_VERSION = '2024-11-05'
DEFAULT_TIMEOUT = 20.0
MAX_RESULT = 12000          # 单次工具结果截断上限（字符）


class McpError(Exception):
    pass


def render_content(content) -> str:
    """MCP content 数组 -> 纯文本（不支持的类型降级，不报错）。"""
    parts = []
    for c in content or []:
        if not isinstance(c, dict):
            parts.append(str(c))
            continue
        t = c.get('type')
        if t == 'text':
            parts.append(str(c.get('text') or ''))
        elif t == 'image':
            data = c.get('data') or ''
            parts.append(f"[图片 {c.get('mimeType') or 'image'}，"
                         f"{len(data)} 字符 base64；不支持内联，已降级为文本]")
        elif t in ('resource', 'resource_link') or 'resource' in c:
            parts.append('[资源：' + json.dumps(c, ensure_ascii=False)[:400] + ']')
        else:
            parts.append(f'[不支持的内容类型: {t}]')
    return '\n'.join(p for p in parts if p)


class McpServer:
    """一个 MCP server 子进程：stdio JSON-RPC，串行调用。"""

    def __init__(self, cfg: dict):
        cfg = dict(cfg or {})
        self.name = str(cfg.get('name') or '').strip() or 'unnamed'
        self.command = str(cfg.get('command') or '').strip()
        self.args = [str(a) for a in (cfg.get('args') or [])]
        self.env = {str(k): str(v) for k, v in (cfg.get('env') or {}).items()}
        self.timeout = float(cfg.get('timeout') or DEFAULT_TIMEOUT)
        self.enabled = bool(cfg.get('enabled', True))
        self.cfg = cfg

        self.status = 'stopped'        # stopped | connected | error
        self.error = ''
        self.tools = []                # tools/list 返回的原始 schema
        self.resources = []            # resources/list（不支持则留空）
        self.prompts = []              # prompts/list（不支持则留空）
        self.proc = None
        self._gen = 0                  # 进程代号：旧 reader 不再投递到新连接
        self._id = 0
        self._q = queue.Queue()
        self._lock = threading.RLock()
        self._stderr_tail = deque(maxlen=20)
        self._reader = None

    # ── 进程生命周期 ─────────────────────────────────────────
    def start(self) -> bool:
        """拉起子进程并完成握手。失败时置 status='error' 并返回 False。"""
        if not self.command:
            self.status, self.error = 'error', '未配置 command'
            return False
        self.stop()
        # stop() 会把状态归为 stopped；拉起前先标“连接中”，
        # 否则界面在 Popen 前查询会看到“未启动 0 个工具”
        self.status, self.error = 'starting', ''
        env = dict(os.environ)
        env.update(self.env)
        env.setdefault('PYTHONIOENCODING', 'utf-8')
        # Windows 上 npx 实际是 npx.CMD，CreateProcess 不会自动补 PATHEXT，
        # 直接用 "npx" 会报 WinError 2；先用 which 解析成真实可执行文件/脚本。
        exe = shutil.which(self.command) or self.command
        try:
            self.proc = subprocess.Popen(
                [exe] + self.args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace',
                env=env, cwd=os.getcwd())
        except (OSError, ValueError) as e:
            self.status, self.error = 'error', f'启动失败: {e}'
            log.warning('MCP %s 启动失败: %s', self.name, e)
            return False
        # 握手完成前不能报“已连接”：否则界面显示已连接但工具列表是空的
        self.status, self.error = 'starting', ''
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f'mcp-{self.name}')
        self._reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        try:
            self._request('initialize', {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {},
                'clientInfo': {'name': 'local-agent-workbench', 'version': '1.0'},
            }, self.timeout)
            self._notify('notifications/initialized')
            r = self._request('tools/list', {}, self.timeout)
            self.tools = list((r or {}).get('tools') or [])
            # resources / prompts 是可选能力：server 不支持就留空，不影响连接
            self.resources = self._try_list('resources/list', 'resources')
            self.prompts = self._try_list('prompts/list', 'prompts')
            self.status = 'connected'
            log.info('MCP %s 已连接（%d 个工具）', self.name, len(self.tools))
            return True
        except McpError as e:
            self.error = str(e)
            if '超时' in self.error:
                self.error += '（首次运行可能在下载依赖，稍后点「重连」重试）'
            self.status = 'error'
            log.warning('MCP %s 握手失败: %s', self.name, e)
            self.stop(silent=True)
            return False
        except Exception as e:
            # 任何意外异常都不能把状态留在 starting（界面会永远转圈）
            self.error = f'初始化异常: {type(e).__name__}: {e}'
            self.status = 'error'
            log.exception('MCP %s 初始化异常', self.name)
            self.stop(silent=True)
            return False

    def _try_list(self, method: str, key: str) -> list:
        try:
            r = self._request(method, {}, min(self.timeout, 8.0))
            return list((r or {}).get(key) or [])
        except McpError:
            return []

    def read_resource(self, uri: str) -> str:
        """resources/read -> 文本（blob 降级为说明）。"""
        r = self._request('resources/read', {'uri': uri}, self.timeout)
        out = []
        for c in (r or {}).get('contents') or []:
            if not isinstance(c, dict):
                continue
            if c.get('text'):
                out.append(str(c['text']))
            elif c.get('blob'):
                out.append(f"[二进制资源 {c.get('mimeType') or ''}，"
                           f"{len(str(c['blob']))} 字符 base64，已降级为文本]")
        text = '\n'.join(out) or '（资源内容为空）'
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + f'\n…（已截断，共 {len(text)} 字符）'
        return text

    def get_prompt(self, name: str, arguments: dict = None) -> str:
        """prompts/get -> 拼成一段可直接填给模型的文本。"""
        r = self._request('prompts/get',
                          {'name': name, 'arguments': arguments or {}},
                          self.timeout)
        parts = []
        for m in (r or {}).get('messages') or []:
            c = (m or {}).get('content') or {}
            if isinstance(c, dict):
                if c.get('type') == 'text':
                    parts.append(str(c.get('text') or ''))
                else:
                    parts.append(f"[{c.get('type') or 'content'}]")
            else:
                parts.append(str(c))
        text = '\n\n'.join(p for p in parts if p)
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + '\n…（已截断）'
        return text

    def stop(self, silent: bool = False):
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._gen += 1                 # 先作废旧 reader，再清残留响应
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        if self.status != 'error':
            self.status = 'stopped'
        if not silent:
            self.tools = []

    def _read_loop(self):
        proc, gen = self.proc, self._gen
        if proc is None:
            return
        def put(x):
            if gen == self._gen:          # 只投递给同一代连接
                self._q.put(x)
        try:
            for line in iter(proc.stdout.readline, ''):
                if line.strip():
                    put(line)
        except (OSError, ValueError):
            pass
        # EOF：进程退出，唤醒所有等待者
        put(None)
        if self.proc is proc and gen == self._gen:
            if self.status == 'connected':
                self.status = 'error'
                self.error = self.error or '进程已退出'

    def _drain_stderr(self):
        proc = self.proc
        if proc is None:
            return
        try:
            for line in iter(proc.stderr.readline, ''):
                self._stderr_tail.append(line.rstrip())
        except (OSError, ValueError):
            pass

    # ── JSON-RPC ─────────────────────────────────────────────
    def _notify(self, method: str, params: dict = None):
        msg = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        self._write(msg)

    def _write(self, msg: dict):
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise McpError(f'MCP {self.name} 未连接')
        try:
            proc.stdin.write(json.dumps(msg, ensure_ascii=False) + '\n')
            proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise McpError(f'MCP {self.name} 写入失败: {e}')

    def _request(self, method: str, params: dict, timeout: float):
        self._id += 1
        mid = self._id
        self._write({'jsonrpc': '2.0', 'id': mid, 'method': method,
                     'params': params or {}})
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise McpError(f'MCP {self.name} 超时（{timeout:.0f}s，{method}）')
            try:
                line = self._q.get(timeout=min(remain, 1.0))
            except queue.Empty:
                if self.proc is None:
                    raise McpError(f'MCP {self.name} 进程已停止')
                continue
            if line is None:
                # deque 不支持切片（[-3:] 会抛 TypeError），先转 list 再取尾部
                tail = '; '.join(list(self._stderr_tail)[-3:])
                raise McpError(f'MCP {self.name} 进程已退出'
                               + (f'（stderr: {tail}）' if tail else ''))
            try:
                obj = json.loads(line)
            except ValueError:
                continue                       # 非 JSON 行（日志）直接忽略
            if obj.get('id') != mid:
                continue                       # 通知 / 其它请求的响应
            if 'error' in obj:
                err = obj['error'] or {}
                raise McpError(f"MCP {self.name} 返回错误 "
                               f"{err.get('code')}: {err.get('message')}")
            return obj.get('result') or {}

    # ── 对外调用 ─────────────────────────────────────────────
    def call(self, tool: str, arguments: dict) -> str:
        """调用工具并把结果渲染成文本；进程挂了自动重启一次再试。"""
        with self._lock:
            if self.status != 'connected' or self.proc is None:
                if not self.start():
                    raise McpError(f"MCP {self.name} 连接失败: {self.error}")
            try:
                r = self._request('tools/call',
                                  {'name': tool, 'arguments': arguments or {}},
                                  self.timeout)
            except McpError as e:
                if '超时' in str(e):
                    raise                      # 超时不重启（可能真在长跑）
                if not self.start():
                    raise McpError(f'{e}；重启失败: {self.error}')
                r = self._request('tools/call',
                                  {'name': tool, 'arguments': arguments or {}},
                                  self.timeout)
            text = render_content(r.get('content'))
            if r.get('isError'):
                text = '❌ MCP 返回错误：' + text
            if len(text) > MAX_RESULT:
                text = text[:MAX_RESULT] + f'\n…（已截断，共 {len(text)} 字符）'
            return text

    def status_dict(self) -> dict:
        return {'name': self.name, 'command': self.command,
                'args': self.args, 'env': dict(self.env),
                'enabled': self.enabled,
                'status': self.status, 'error': self.error,
                'timeout': self.timeout,
                'tools': [{'name': t.get('name', ''),
                           'description': (t.get('description') or '')[:200]}
                          for t in self.tools],
                'resources': [{'uri': r.get('uri', ''), 'name': r.get('name', ''),
                               'mimeType': r.get('mimeType', '')}
                              for r in self.resources],
                'prompts': [{'name': p.get('name', ''),
                             'description': (p.get('description') or '')[:120]}
                            for p in self.prompts]}


class McpManager:
    """进程级 MCP 管理：按配置启停、对外提供工具注册表。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._servers = {}        # name -> McpServer

    def configure(self, servers_cfg) -> list:
        """按配置重建连接：enabled 的拉起，去掉的停掉，已连的保持。"""
        servers_cfg = list(servers_cfg or [])
        with self._lock:
            want = {}
            for c in servers_cfg:
                c = dict(c or {})
                nm = str(c.get('name') or '').strip()
                if nm:
                    want[nm] = c
            for nm in list(self._servers):
                if nm not in want:
                    self._servers.pop(nm).stop()
            for nm, c in want.items():
                old = self._servers.get(nm)
                sig = (c.get('command'), tuple(c.get('args') or ()),
                       json.dumps(c.get('env') or {}, sort_keys=True))
                if old is not None:
                    old_sig = (old.command, tuple(old.args),
                               json.dumps(old.env, sort_keys=True))
                    if old_sig == sig:
                        old.enabled = bool(c.get('enabled', True))
                        old.timeout = float(c.get('timeout') or DEFAULT_TIMEOUT)
                        if not old.enabled:
                            old.stop()          # 关掉就停进程，不残留子进程
                        elif old.status != 'connected':
                            threading.Thread(target=old.start, daemon=True).start()
                        continue
                    old.stop()
                s = McpServer(c)
                self._servers[nm] = s
                if s.enabled:
                    s.status = 'starting'   # 先置“连接中”：线程调度前查询也不会误报未启动
                    threading.Thread(target=s.start, daemon=True).start()
            return list(self._servers.values())

    def status(self) -> list:
        with self._lock:
            out = [s.status_dict() for s in self._servers.values()]
        out.sort(key=lambda x: x['name'])
        return out

    def tool_specs(self) -> list:
        """给 ToolRegistry 用：[(name, description, parameters, fn)]。"""
        specs = []
        with self._lock:
            servers = [s for s in self._servers.values()
                       if s.enabled and s.status == 'connected']
        for s in servers:
            for t in s.tools:
                tname = str(t.get('name') or '')
                if not tname:
                    continue
                full = f'mcp__{s.name}__{tname}'
                desc = (t.get('description') or f'MCP 工具 {tname}')[:500]
                params = t.get('inputSchema') or {'type': 'object', 'properties': {}}
                specs.append((full, desc, params, self._mk(s, tname)))
        return specs

    @staticmethod
    def _mk(server: McpServer, tool: str):
        def _fn(args: dict):
            from agent.tools import ToolResult
            try:
                return ToolResult(server.call(tool, args or {}))
            except McpError as e:
                return ToolResult(f'❌ {e}')
        return _fn

    def read_resource(self, server: str, uri: str) -> str:
        with self._lock:
            s = self._servers.get(server)
        if s is None or s.status != 'connected':
            raise McpError(f'MCP {server} 未连接')
        return s.read_resource(uri)

    def get_prompt(self, server: str, name: str) -> str:
        with self._lock:
            s = self._servers.get(server)
        if s is None or s.status != 'connected':
            raise McpError(f'MCP {server} 未连接')
        return s.get_prompt(name)

    def restart(self, name: str = '') -> bool:
        """手动重连一个（或全部）server；返回是否拉起成功。"""
        with self._lock:
            targets = [s for s in self._servers.values()
                       if (not name or s.name == name) and s.enabled]
        ok = True
        for s in targets:
            ok = bool(s.start()) and ok
        return ok

    def stop_all(self):
        with self._lock:
            for s in self._servers.values():
                s.stop()
            self._servers.clear()


# 进程级单例：AgentServer 启动时 configure，ToolRegistry 注册时读取
manager = McpManager()
