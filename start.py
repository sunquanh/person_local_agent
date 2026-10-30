# -*- coding: utf-8 -*-
"""加菲猫的工作室 · 一键启动器。

平时双击「启动.bat」就行，这个文件是它背后干活的脚本。
也可以在命令行直接用：

    python start.py               按 config.json 启动
    python start.py 8081          换个端口启动
    python start.py lan           开局域网（同事用本机 IP 访问）
    python start.py 8081 lan      换端口 + 局域网
    python start.py keep          关掉网页后不退出（默认 5 秒后自动退出）
    python start.py --no-browser  不自动打开浏览器

它比直接跑 app.py 多做四件事：

  1. 没有 config.json 时从示例生成一份，并引导先填模型地址
     （顺带把示例里写死的 D:\\我的工作台 换成盘符确实存在的路径）
  2. 端口被占用时先分辨是谁占的：
       本工作台 → 说明已经开着，直接帮你开浏览器，不再起第二个
       别的程序 → 自动往后找一个空闲端口，而不是报错退出
  3. 出错不闪退，把原因留在窗口里（双击场景看不到命令行历史）
  4. 启动参数交给 app.py，沿用它的环境自检和启动横幅

「关网页即退」由 server/httpd.py 实现，这里只负责开关和提示。
"""
import json
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, 'config.json')
EXAMPLE_PATH = os.path.join(HERE, 'config.example.json')
TITLE = '加菲猫的工作室'
DEFAULT_PORT = 8080

HELP = """
加菲猫的工作室 · 启动器

  python start.py               按 config.json 启动
  python start.py 8081          指定端口
  python start.py lan           开局域网（0.0.0.0，同事可访问）
  python start.py 8081 lan      端口 + 局域网
  python start.py keep          关掉网页后不退出（默认会自动退出）
  python start.py --no-browser  不自动打开浏览器

  默认行为：关掉所有网页标签后等 5 秒自动停止服务，cmd 窗口一起关闭。
  Ctrl+C 也可以立即停止；直接关掉这个窗口同样能停。
"""


# ── 控制台 ──────────────────────────────────────────────────────

def setup_console():
    """统一 UTF-8 输出（配合 bat 里的 chcp 65001），并把窗口标题改成中文。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(TITLE)
        except Exception:
            pass


def say(msg=''):
    print(msg, flush=True)


def line(ch='─', n=58):
    say(ch * n)


# ── 配置 ────────────────────────────────────────────────────────

def read_config():
    """读 config.json；不存在返回 None，读坏了抛错（不交给 app.py 用默认值兜底，
    默认工作台目录可能不是用户的真实目录，静默兜底会在错误目录里读写）。"""
    if not os.path.isfile(CFG_PATH):
        return None
    with open(CFG_PATH, encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('config.json 顶层必须是对象')
    return data


def _drive_missing(path):
    """D:\\xxx 这种路径，判断盘符在不在（离线机换机器最常见的就是这个坑）。"""
    drive = os.path.splitdrive(path or '')[0]
    if not drive or os.name != 'nt':
        return False
    return not os.path.isdir(drive + os.sep)


def bootstrap_config():
    """首次运行：从示例生成 config.json，并把不可用的盘符路径换成用户目录。"""
    if not os.path.isfile(EXAMPLE_PATH):
        say('❌ 找不到 config.example.json，无法自动生成配置。')
        say('   请确认项目文件完整，或手动创建 config.json。')
        return False
    try:
        import shutil
        shutil.copyfile(EXAMPLE_PATH, CFG_PATH)
    except Exception as e:
        say('❌ 生成 config.json 失败：%s' % e)
        return False

    # 示例里工作台路径写的是 D:\我的工作台，换台机器可能根本没 D 盘
    try:
        with open(CFG_PATH, encoding='utf-8') as f:
            cfg = json.load(f)
        root = ((cfg.get('workspace') or {}).get('root') or '')
        if _drive_missing(root):
            fallback = os.path.join(os.path.expanduser('~'), '我的工作台')
            cfg.setdefault('workspace', {})['root'] = fallback
            with open(CFG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            say('   示例里的工作台路径 %s 所在盘符不存在，已改为：%s' % (root, fallback))
    except Exception:
        pass

    say()
    say('📝 首次运行：已根据 config.example.json 生成 config.json')
    say('   只需要填两处：')
    say('     model.base_url  内网模型的地址，例如 http://127.0.0.1:8000/v1')
    say('     model.api_key   模型服务的 Key（本地模型随便填，如 sk-none）')
    say('   其余项先不用管，启动后在「设置」页也能改。')
    say()

    try:
        ans = input('现在用记事本打开 config.json 填写吗？[Y/n] ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = 'n'
    if ans in ('', 'y', 'yes'):
        try:
            import subprocess
            subprocess.Popen(['notepad.exe', CFG_PATH])
            input('改完保存并关闭记事本，然后按回车继续启动…')
        except Exception as e:
            say('⚠️  打不开记事本（%s），请手动编辑：%s' % (e, CFG_PATH))
    return True


# ── 端口 ────────────────────────────────────────────────────────

def tcp_open(port, timeout=0.5):
    """端口上有没有人在听。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def looks_like_workbench(port):
    """占着端口的是不是同一个工作台（看 /api/health 的特征字段）。

    只认字段、不认状态码：别的程序占端口时也可能返回 200 的 HTML，
    这里要求能解析出 JSON 且带 auth_required / uptime_s，才不会误判。
    """
    try:
        import urllib.request
        # 环境里若设了 HTTP_PROXY，直连 127.0.0.1 会被代理拦成 502
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:%d/api/health' % port, timeout=2.0) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
        return (isinstance(data, dict) and data.get('ok') is True
                and 'auth_required' in data and 'uptime_s' in data)
    except Exception:
        return False


def choose_port(want):
    """返回 (端口, 状态)，状态取值 free / already / moved / none。"""
    if not tcp_open(want):
        return want, 'free'
    if looks_like_workbench(want):
        return want, 'already'
    for p in range(want + 1, min(want + 31, 65536)):
        if not tcp_open(p):
            return p, 'moved'
    return None, 'none'


# ── 主流程 ──────────────────────────────────────────────────────

def parse_args(argv):
    want_port = None
    lan = False
    no_browser = False
    keep = False
    for a in argv:
        low = a.lower()
        if low in ('lan', '--lan', '-lan'):
            lan = True
        elif low in ('keep', '--keep', '--keep-alive', '-k'):
            keep = True
        elif low in ('--no-browser', '-n', '--nobrowser', 'nobrowser'):
            no_browser = True
        elif a.isdigit():
            want_port = int(a)
        elif low in ('-h', '--help', 'help', '/?', '-?'):
            say(HELP)
            return 'help', None, False, False, False
        else:
            say('⚠️  忽略未知参数：%s（可用：端口号 / lan / keep / --no-browser）' % a)
    return 'run', want_port, lan, no_browser, keep


def main():
    setup_console()
    action, want_port, lan, no_browser, keep = parse_args(sys.argv[1:])
    if action == 'help':
        return 0

    line('═')
    say('  %s · 一键启动' % TITLE)
    line('═')

    try:
        cfg = read_config()
    except ValueError as e:
        say('❌ %s，启动已停止，请修复后重试。' % e)
        return 1
    if cfg is None:
        if not bootstrap_config():
            return 1
        cfg = read_config() or {}

    model = cfg.get('model') or {}
    srv = cfg.get('server') or {}
    ws_root = (cfg.get('workspace') or {}).get('root', '')
    base_url = (model.get('base_url') or '').strip()

    say('  配置文件 : %s' % CFG_PATH)
    say('  模型     : %s  @  %s' % (model.get('name') or '(未设置)',
                                   base_url or '(未设置)'))
    say('  工作台   : %s' % (ws_root or '(未设置)'))
    if not base_url:
        say('  ⚠️  还没填模型服务地址，启动后无法对话（可在设置页补）')
    line()

    # 端口：命令行 > config.json > 8080
    try:
        port_want = want_port or int(srv.get('port') or DEFAULT_PORT)
    except (TypeError, ValueError):
        say('❌ config.json 里 server.port 不是合法端口，启动已停止，请修复后重试。')
        return 1
    if not (1 <= port_want <= 65535):
        say('⚠️  端口 %d 不合法，改用 %d' % (port_want, DEFAULT_PORT))
        port_want = DEFAULT_PORT

    port, state = choose_port(port_want)

    if state == 'already':
        url = 'http://127.0.0.1:%d' % port
        say('✅ 工作台已经在运行（端口 %d），不用再起一个。' % port)
        say('   %s' % url)
        if not no_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        say('   如需再开一个实例：启动.bat 8081')
        line('═')
        return 0

    if state == 'none':
        say('❌ %d 往后 30 个端口全被占用，没法启动。' % port_want)
        say('   先关掉一些程序，或用「启动.bat 9000」指定一个空闲端口。')
        return 1

    if state == 'moved':
        say('⚠️  端口 %d 被其他程序占用，自动改用 %d' % (port_want, port))
    else:
        say('✅ 端口 %d 可用' % port)

    if lan:
        say('   局域网模式：同事用 http://本机IP:%d 访问' % port)
        say('   访问令牌会打印在下面的启动横幅里')

    # 关网页即退：默认开启，`keep` 参数或 config 里的 auto_exit 可以关掉
    auto_exit = bool(srv.get('auto_exit', True)) and not keep
    if auto_exit:
        try:
            grace = int(float(srv.get('auto_exit_grace_s', 5)))
        except (TypeError, ValueError):
            grace = 5
        say('   关掉所有网页标签后，%d 秒内没有新连接就自动停止服务' % grace)
    else:
        say('   关闭网页后服务继续运行（想自动退出就去掉 keep 参数）')

    say()
    line('═')

    # 输出被重定向到文件时默认是块缓冲，日志会憋到最后才出现；
    # 切成行缓冲，双击窗口和 `> log.txt` 都能实时看到进度
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # 交给 app.py：环境自检、启动横幅、自动开浏览器都在那边
    sys.argv = [os.path.join(HERE, 'app.py'), '--config', CFG_PATH,
                '--port', str(port)]
    if lan:
        sys.argv += ['--host', '0.0.0.0']
    if keep:
        sys.argv.append('--keep-alive')
    if no_browser:
        sys.argv.append('--no-browser')

    sys.path.insert(0, HERE)
    try:
        import app as appmod
    except Exception:
        import traceback
        say('❌ 加载 app.py 失败，项目文件可能不完整：')
        traceback.print_exc()
        return 1

    try:
        appmod.main()
    except SystemExit as e:
        # app.py 遇到端口占用/目录不可用会 sys.exit(1)，把码透出去给 bat 判断
        return e.code if isinstance(e.code, int) else 0
    except KeyboardInterrupt:
        say('\n已停止。')
        return 0
    return 0


if __name__ == '__main__':
    try:
        rc = main()
    except KeyboardInterrupt:
        say('\n已停止。')
        rc = 0
    except Exception:
        import traceback
        say()
        say('❌ 启动过程中出现未预期的错误：')
        traceback.print_exc()
        rc = 1
    sys.exit(rc)
