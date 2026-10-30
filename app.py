# -*- coding: utf-8 -*-
"""加菲猫的工作室入口。

用法:
    python app.py [--port 8080] [--host 127.0.0.1] [--config config.json]

首次运行请先复制 config.example.json 为 config.json 并填写模型与工作台路径。
浏览器访问 http://127.0.0.1:8080
"""
import argparse
import json
import os
import sys
import webbrowser

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 与 config.example.json 对齐：缺的键在这里补齐（值与代码既有默认值一致，
# 改动前后行为不变）。注意不补 model.max_tokens：example 写 0 表示不限，
# 但 validate_config 要求 max_tokens > 0，补 0 会导致每次启动都校验失败；
# example 与校验器的这个口子需另行定夺（改 example 或改校验），不在本批动。
DEFAULT_CONFIG = {
    "model": {"base_url": "http://127.0.0.1:8000/v1", "api_key": "", "name": "qwen",
              "thinking": "collapse",
              "context_window": 32768},
    "workspace": {"root": os.path.join(os.path.expanduser('~'), '我的工作台')},
    "server": {"host": "127.0.0.1", "port": 8080,
               "auto_exit": True, "auto_exit_grace_s": 5},
    "limits": {"max_upload_mb": 50, "chat_timeout_s": 600,
               "llm_timeout_s": 300,
               "compress_threshold_chars": 20000},
    "rag": {"enabled": True, "top_k": 4},
    "memory": {"extract": True},
    "prompt": {"role": "", "rules": []},
}


def check_env(cfg: dict) -> bool:
    """启动环境检测：工作台目录 / 可选办公库 / 前端资源 / 模型服务连通性。

    离线机排查问题时先看这段输出（是否缺依赖、盘符是否存在、模型是否可达）。
    返回 False 表示工作台目录不可用（无法继续）。
    """
    import importlib
    import socket as _sock
    from pathlib import Path as _P
    from urllib.parse import urlparse as _up

    print("\n── 环境检测 ────────────────────────────────────")
    print(f"  Python: {sys.version.split()[0]}")

    # 1) 工作台目录（盘符不存在/不可写时给出明确提示，而不是裸 traceback）
    root = (cfg.get('workspace', {}) or {}).get('root', '')
    try:
        _P(root).mkdir(parents=True, exist_ok=True)
        print(f"  ✅ 工作台目录: {root}")
    except Exception as e:
        print(f"  ❌ 工作台目录创建失败: {root}")
        print(f"     原因: {e}")
        print("     请检查盘符是否存在（如 D: 未挂载）、目录是否可写，"
              "或修改 config.json 的 workspace.root")
        print("──────────────────────────────────────────────────────")
        return False

    # 2) 可选办公库（之前离线装过的包会被自动复用，缺的给提示）
    try:
        from agent.tools import detect_optional_deps
        deps = detect_optional_deps()
    except Exception:
        deps = {}
    labels = {'python-docx': 'Word 读写', 'openpyxl': 'Excel 读写',
              'python-pptx': 'PPT 读取', 'pypdf': 'PDF 处理', 'Pillow': '图片处理'}
    for name, avail in deps.items():
        if avail:
            print(f"  ✅ {name}（{labels.get(name, '')}）已安装")
        else:
            print(f"  ⚠️ {name}（{labels.get(name, '')}）未安装，相关工具不可用")
            print(f"     离线机安装: pip install {name}")

    # 3) 前端资源
    missing = [f for f in ('markdown-it.min.js', 'highlight.min.js', 'github.min.css')
               if not (_P(HERE) / 'web' / 'vendor' / f).is_file()]
    if missing:
        print(f"  ⚠️ 前端资源缺失: {', '.join(missing)}（web/vendor 目录被删了？）")
    else:
        print("  ✅ 前端资源完整（markdown-it / highlight.js）")

    # 4) 模型服务连通性（TCP 探测，仅提示当前状态）
    base_url = (cfg.get('model', {}) or {}).get('base_url', '')
    try:
        u = _up(base_url)
        host = u.hostname or '127.0.0.1'
        port = u.port or (443 if u.scheme == 'https' else 80)
        _sock.create_connection((host, port), timeout=3).close()
        print(f"  ✅ 模型服务可达: {base_url}")
    except Exception as e:
        print(f"  ⚠️ 模型服务当前不可达: {base_url or '(未配置)'}")
        print(f"     原因: {e}")
        print("     （内网模型未启动时属正常，启动后可在设置页点『测试连接』）")
    print("──────────────────────────────────────────────────────")
    return True


def _coerce_int(value, field: str) -> int:
    """配配置里的整数：拒绝 bool/float/非数字字符串，数字字符串转 int。"""
    if isinstance(value, bool):
        raise ValueError(f'配置 {field} 必须是整数，收到布尔值')
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip('-').isdigit():
        return int(value.strip())
    raise ValueError(f'配置 {field} 必须是整数，收到 {value!r}')


def _coerce_number(value, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f'配置 {field} 必须是数字，收到布尔值')
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ValueError(f'配置 {field} 必须是数字，收到 {value!r}')


def validate_config(cfg: dict) -> dict:
    """校验合并后的配置：已知字段的类型/范围不对就抛错，绝不带病启动。

    未知 section/key 原样放过（向前兼容）；字符串型的数字只接受纯数字
    串并转回 int/float，其余一律拒绝——旧版不校验时，port 写成 "abc"
    会在绑定时崩、max_tokens 写错会在对话中途崩，报错离现场很远。
    """
    if not isinstance(cfg, dict):
        raise ValueError('配置顶层必须是对象')
    sections = {'model': dict, 'workspace': dict, 'server': dict,
                'limits': dict, 'rag': dict, 'memory': dict,
                'prompt': dict, 'mcp': dict, 'security': dict}
    for name, typ in sections.items():
        if name in cfg and not isinstance(cfg[name], typ):
            raise ValueError(f'配置 {name} 必须是对象，收到 {cfg[name]!r}')
    model = cfg.get('model') or {}
    for k in ('base_url', 'api_key', 'name'):
        if k in model and not isinstance(model[k], str):
            raise ValueError(f'配置 model.{k} 必须是字符串')
    if 'context_window' in model:
        model['context_window'] = _coerce_int(model['context_window'],
                                              'model.context_window')
        if model['context_window'] < 0:
            raise ValueError('配置 model.context_window 不能为负')
    if 'max_tokens' in model:
        model['max_tokens'] = _coerce_int(model['max_tokens'], 'model.max_tokens')
        if model['max_tokens'] <= 0:
            raise ValueError('配置 model.max_tokens 必须大于 0')
    if 'thinking' in model and not isinstance(model['thinking'], str):
        raise ValueError('配置 model.thinking 必须是字符串')
    ws = cfg.get('workspace') or {}
    if 'root' in ws:
        if not isinstance(ws['root'], str) or not ws['root'].strip():
            raise ValueError('配置 workspace.root 必须是非空字符串')
    srv = cfg.get('server') or {}
    if 'host' in srv and not isinstance(srv['host'], str):
        raise ValueError('配置 server.host 必须是字符串')
    if 'port' in srv:
        srv['port'] = _coerce_int(srv['port'], 'server.port')
        if not 1 <= srv['port'] <= 65535:
            raise ValueError(f"配置 server.port 超出范围：{srv['port']}")
    if 'auto_exit' in srv and not isinstance(srv['auto_exit'], bool):
        raise ValueError('配置 server.auto_exit 必须是布尔值')
    if 'auto_exit_grace_s' in srv:
        srv['auto_exit_grace_s'] = _coerce_number(srv['auto_exit_grace_s'],
                                                 'server.auto_exit_grace_s')
        if srv['auto_exit_grace_s'] < 0:
            raise ValueError('配置 server.auto_exit_grace_s 不能为负')
    lim = cfg.get('limits') or {}
    for k in ('max_upload_mb', 'chat_timeout_s', 'compress_threshold_chars'):
        if k in lim:
            lim[k] = _coerce_number(lim[k], f'limits.{k}')
            if lim[k] < 0:
                raise ValueError(f'配置 limits.{k} 不能为负')
    rag = cfg.get('rag') or {}
    if 'enabled' in rag and not isinstance(rag['enabled'], bool):
        raise ValueError('配置 rag.enabled 必须是布尔值')
    if 'top_k' in rag:
        rag['top_k'] = _coerce_int(rag['top_k'], 'rag.top_k')
        if rag['top_k'] <= 0:
            raise ValueError('配置 rag.top_k 必须大于 0')
    return cfg


def load_config(path: str) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认
    if os.path.isfile(path):
        try:
            with open(path, encoding='utf-8') as f:
                user = json.load(f)
        except Exception as e:
            # 配置文件存在但解析失败：绝不能静默用默认配置启动——默认的
            # 工作台目录（~/我的工作台）可能根本不是用户的真实工作台，
            # agent 会在错误的目录里读写。直接报错退出，让用户修配置。
            raise ValueError(f'配置文件解析失败 {path}：{e}（请修复后重启）')
        if not isinstance(user, dict):
            raise ValueError(f'配置文件顶层必须是对象：{path}')
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    else:
        print(f"⚠️ 未找到配置文件 {path}，已使用默认配置。"
              f"请参考 config.example.json 创建 config.json")
    return validate_config(cfg)


def main():
    ap = argparse.ArgumentParser(description='加菲猫的工作室')
    ap.add_argument('--host', default=None)
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--keep-alive', action='store_true',
                    help='关闭所有网页后不退出服务（默认会在 5 秒后退出）')
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ValueError as e:
        print(f"\n❌ {e}")
        sys.exit(1)
    if args.host:
        cfg.setdefault('server', {})['host'] = args.host
    if args.port:
        cfg.setdefault('server', {})['port'] = args.port
    if args.keep_alive:
        cfg.setdefault('server', {})['auto_exit'] = False

    # API Key 自动解密：兼容老项目 ENC: 前缀约定
    api_key = cfg.get('model', {}).get('api_key') or ''
    if api_key.startswith('ENC:'):
        try:
            from encrypt_key import decrypt_key
            cfg['model']['api_key'] = decrypt_key(api_key)
            print('🔓 API Key 已从配置文件解密')
        except Exception as e:
            print(f"⚠️  API Key 解密失败（{e}），将以空 key 启动，请在设置/配置中重新加密")
            cfg['model']['api_key'] = ''

    # 启动环境检测：工作台目录不可用（盘符缺失等）时明确提示并退出，避免裸 traceback
    if not check_env(cfg):
        sys.exit(1)

    from server.httpd import serve
    try:
        server = serve(cfg, host=args.host, port=args.port, config_path=args.config)
    except OSError as e:
        print(f"\n❌ 启动失败: {e}")
        print("   原因: 端口被其他程序占用")
        print("   解决方法:")
        print(f"     1. 换端口启动: python app.py --port 8081")
        print(f"     2. 查看谁占了端口: netstat -ano | findstr :8080")
        print(f"     3. 关闭占用端口的程序")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 启动失败: {e}")
        sys.exit(1)
    host, port = server.server_address[:2]
    # 0.0.0.0 不能直接点开，浏览器统一用回环地址打开
    open_host = '127.0.0.1' if host in ('0.0.0.0', '', '::') else host
    url = f'http://{open_host}:{port}'
    print('\n   按 Ctrl+C 停止\n')
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')
        server.server_close()


if __name__ == '__main__':
    main()
