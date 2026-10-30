# -*- coding: utf-8 -*-
"""本地 Agent 工作台入口。

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

DEFAULT_CONFIG = {
    "model": {"base_url": "http://127.0.0.1:8000/v1", "api_key": "", "name": "qwen",
              "context_window": 32768},
    "workspace": {"root": os.path.join(os.path.expanduser('~'), '我的工作台')},
    "server": {"host": "127.0.0.1", "port": 8080},
    "limits": {"max_upload_mb": 50, "chat_timeout_s": 600,
               "compress_threshold_chars": 20000},
    "rag": {"enabled": True, "top_k": 4},
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


def load_config(path: str) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认
    if os.path.isfile(path):
        try:
            with open(path, encoding='utf-8') as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"⚠️ 配置解析失败（{e}），使用默认配置")
    else:
        print(f"⚠️ 未找到配置文件 {path}，已使用默认配置。"
              f"请参考 config.example.json 创建 config.json")
    return cfg


def main():
    ap = argparse.ArgumentParser(description='本地 Agent 工作台')
    ap.add_argument('--host', default=None)
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--keep-alive', action='store_true',
                    help='关闭所有网页后不退出服务（默认会在 5 秒后退出）')
    args = ap.parse_args()

    cfg = load_config(args.config)
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
