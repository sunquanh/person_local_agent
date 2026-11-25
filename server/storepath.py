# -*- coding: utf-8 -*-
"""选定工作目录是会话和配置的根。

相对路径相对程序目录解析，不使用进程当前目录。
程序目录里的 config.json 只作为旧入口；正文在
``<工作目录>/.agent/config.json``。旧文件不删除。
"""
import json
import os
import shutil


class StorePathError(Exception):
    pass


def _defaults():
    return {
        "model": {
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "",
            "name": "",
            "thinking": "collapse",
            "max_tokens": 0,
            "context_window": 32768,
        },
        "workspace": {"root": ""},
        "server": {
            "host": "127.0.0.1",
            "port": 8080,
            "auto_exit": True,
            "auto_exit_grace_s": 10,
        },
        "limits": {
            "max_upload_mb": 50,
            "chat_timeout_s": 600,
            "llm_timeout_s": 300,
        },
        "memory": {"extract": True},
        "rag": {"enabled": True, "top_k": 4},
        "prompt": {"role": "", "rules": []},
    }


class Launch:
    def __init__(self, workspace_root, config_path, config, explicit_config_path):
        self.workspace_root = workspace_root
        self.config_path = config_path
        self.config = config
        self.explicit_config_path = explicit_config_path


def abspath_from_program(program_dir, path):
    """绝对路径原样规范化；相对路径挂在程序目录下，而不是进程 cwd。"""
    text = os.path.expanduser(str(path or '').strip())
    if not text:
        raise StorePathError('工作目录为空')
    if os.path.isabs(text):
        return os.path.abspath(text)
    return os.path.abspath(os.path.join(os.path.abspath(program_dir), text))


def portable_path(program_dir, root):
    """落盘形态：工作目录在程序目录内就存相对路径，整个文件夹拷走仍有效；
    在程序目录外存绝对路径（跨盘相对路径不可靠）。"""
    prog = os.path.abspath(program_dir)
    target = os.path.abspath(root)
    try:
        rel = os.path.relpath(target, prog)
    except ValueError:
        return target
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return target
    return rel


def _stored_config(config, program_dir, root):
    """写盘用的 config 副本：workspace.root 换成 portable 形态，
    内存里的 config 保持绝对路径不变。"""
    stored = json.loads(json.dumps(config))
    stored.setdefault('workspace', {})['root'] = portable_path(program_dir, root)
    return stored


def pointer_path(program_dir):
    return os.path.join(os.path.abspath(program_dir), '.workspace-root')


def read_pointer(program_dir):
    path = pointer_path(program_dir)
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        text = f.read().strip()
    if not text:
        return None
    return abspath_from_program(program_dir, text)


def write_pointer(program_dir, workspace_root):
    """只写路径（程序目录内为相对路径），不写配置正文，避免把密钥留在程序目录。"""
    path = pointer_path(program_dir)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(portable_path(program_dir, workspace_root) + '\n')
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise StorePathError(f'无法读取配置 {path}：{e}') from e
    if not isinstance(data, dict):
        raise StorePathError(f'配置不是对象: {path}')
    return data


def _write_json(path, data):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _ensure_workspace(root):
    try:
        os.makedirs(root, exist_ok=True)
        agent = os.path.join(root, '.agent')
        os.makedirs(agent, exist_ok=True)
        probe = os.path.join(agent, '.write_probe')
        with open(probe, 'w', encoding='utf-8') as f:
            f.write('ok')
        os.remove(probe)
    except OSError as e:
        raise StorePathError(
            f'工作目录不可用，已停止，不会改用程序目录: {root}（{e}）') from e


def _legacy_config(program_dir):
    path = os.path.join(os.path.abspath(program_dir), 'config.json')
    return path if os.path.isfile(path) else None


def prepare_launch(program_dir, config_arg=None, workspace_arg=None):
    """决定工作目录和本轮要读的配置。

    工作目录优先级：--workspace、显式 --config 里的 workspace.root、
    .workspace-root、程序目录旧 config.json、程序目录下的 work。
    """
    program_dir = os.path.abspath(program_dir)
    explicit_path = None
    explicit_cfg = None
    if config_arg:
        explicit_path = abspath_from_program(program_dir, config_arg)
        if not os.path.isfile(explicit_path):
            raise StorePathError(f'找不到指定的配置文件: {explicit_path}')
        explicit_cfg = _read_json(explicit_path)

    if workspace_arg:
        root = abspath_from_program(program_dir, workspace_arg)
    elif explicit_cfg and (explicit_cfg.get('workspace') or {}).get('root'):
        root = abspath_from_program(program_dir, explicit_cfg['workspace']['root'])
    else:
        pointed = read_pointer(program_dir)
        if pointed:
            root = pointed
        else:
            legacy = _legacy_config(program_dir)
            rel = ''
            if legacy:
                rel = str((_read_json(legacy).get('workspace') or {}).get('root') or '')
            root = (abspath_from_program(program_dir, rel) if rel
                    else os.path.join(program_dir, 'work'))

    _ensure_workspace(root)
    live = os.path.join(root, '.agent', 'config.json')
    legacy = _legacy_config(program_dir)

    if explicit_cfg is not None:
        config = explicit_cfg
        if not os.path.isfile(live):
            copied = json.loads(json.dumps(explicit_cfg))
            copied.setdefault('workspace', {})['root'] = \
                portable_path(program_dir, root)
            _write_json(live, copied)
    elif os.path.isfile(live):
        config = _read_json(live)
        if legacy:
            with open(legacy, 'rb') as f:
                legacy_bytes = f.read()
            with open(live, 'rb') as f:
                live_bytes = f.read()
            if legacy_bytes != live_bytes:
                backup_dir = os.path.join(root, '.agent', 'import-backup')
                os.makedirs(backup_dir, exist_ok=True)
                note = os.path.join(backup_dir, 'config.conflict.txt')
                if not os.path.exists(note):
                    with open(note, 'w', encoding='utf-8') as f:
                        f.write('程序目录的 config.json 与工作区配置不同，'
                                '已保留工作区文件，未覆盖。\n')
    elif legacy:
        backup_dir = os.path.join(root, '.agent', 'import-backup')
        os.makedirs(backup_dir, exist_ok=True)
        backup = os.path.join(backup_dir, 'config.json')
        if not os.path.exists(backup):
            shutil.copy2(legacy, backup)
        if not os.path.exists(live):
            shutil.copy2(legacy, live)
        config = _read_json(live)
    else:
        config = _defaults()
        config['workspace']['root'] = root
        _write_json(live, _stored_config(config, program_dir, root))

    config.setdefault('workspace', {})['root'] = root
    if explicit_cfg is None:
        _write_json(live, _stored_config(config, program_dir, root))
    write_pointer(program_dir, root)
    return Launch(root, live, config, explicit_path)


_UI_KEYS = ('theme', 'last_session', 'model_overrides', 'panel_widths')


def _blank_ui():
    return {
        'theme': 'light',
        'last_session': '',
        'model_overrides': {},
        'panel_widths': {},
    }


def load_ui(workspace_root):
    path = os.path.join(workspace_root, '.agent', 'ui.json')
    base = _blank_ui()
    if not os.path.isfile(path):
        return base
    try:
        data = _read_json(path)
    except StorePathError:
        return base
    for key in _UI_KEYS:
        if key in data:
            base[key] = data[key]
    return base


def save_ui(workspace_root, patch):
    # 保留未知字段：读原始 JSON，只覆盖 patch 里的已知键。
    # 文件损坏时 _read_json 抛 StorePathError，绝不静默覆盖成空白。
    path = os.path.join(workspace_root, '.agent', 'ui.json')
    if os.path.isfile(path):
        current = _read_json(path)
    else:
        current = {}
    for key, val in _blank_ui().items():
        current.setdefault(key, val)
    for key in _UI_KEYS:
        if key in (patch or {}):
            current[key] = patch[key]
    _write_json(path, current)
    return current
