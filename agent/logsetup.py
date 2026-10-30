# -*- coding: utf-8 -*-
"""统一日志（纯标准库）：控制台 + 工作台 .agent/logs/app.log 滚动文件。

离线机排障的核心：控制台一关就没了的场景，日志必须落盘。
- RotatingFileHandler：单文件 2MB，保留 3 份，编码 utf-8（Windows 下必须显式指定）
- 全局只初始化一次；重复调用直接返回同一个 logger
- tail() 供 /api/logs 与设置页「查看日志」使用
"""
import logging
import logging.handlers
import threading
from pathlib import Path

ROOT_NAME = 'agent'
_lock = threading.Lock()
_ready = False
_file_path = None

_FMT = logging.Formatter('%(asctime)s %(levelname)-5s [%(name)s] %(message)s',
                         '%Y-%m-%d %H:%M:%S')


def setup(root=None, level=logging.INFO):
    """初始化日志。root=工作台根目录（日志落 <root>/.agent/logs/app.log）。"""
    global _ready, _file_path
    with _lock:
        lg = logging.getLogger(ROOT_NAME)
        if _ready:
            return lg
        lg.setLevel(level)
        lg.propagate = False
        sh = logging.StreamHandler()
        sh.setFormatter(_FMT)
        sh.setLevel(logging.WARNING)      # 控制台只留警告以上，避免刷屏
        lg.addHandler(sh)
        if root:
            try:
                d = Path(root) / '.agent' / 'logs'
                d.mkdir(parents=True, exist_ok=True)
                p = d / 'app.log'
                fh = logging.handlers.RotatingFileHandler(
                    p, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
                fh.setFormatter(_FMT)
                lg.addHandler(fh)
                _file_path = p
            except Exception as e:      # 日志不可用绝不能影响主流程
                lg.warning('日志文件初始化失败: %s', e)
        _ready = True
        return lg


def get(name: str = ''):
    """取子 logger（自动挂到 agent.* 下）。"""
    return logging.getLogger(ROOT_NAME + ('.' + name if name else ''))


def path(root=None):
    if _file_path is not None:
        return _file_path
    if root:
        return Path(root) / '.agent' / 'logs' / 'app.log'
    return None


def tail(root=None, n: int = 300):
    """读日志末尾 n 行（设置页「查看日志」用）。"""
    p = path(root)
    if not p or not Path(p).is_file():
        return []
    try:
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
        return lines[-max(1, int(n)):]
    except OSError:
        return []
