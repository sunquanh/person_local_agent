# -*- coding: utf-8 -*-
"""打包：PyInstaller -> dist/LocalAgent.exe（Windows）或 dist/LocalAgent（Linux）。

入口是 local_agent.spec（start.py，单文件、带控制台）。
输出先改 UTF-8：Windows 控制台默认 cp1252，中文打印会报 UnicodeEncodeError
（FileCompare v1.0 打包就是这么失败的，R16 修过一次，这里直接按修好的写法）。
用法：python tools/build_release.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    spec = ROOT / "local_agent.spec"
    if not spec.exists():
        print(f"找不到打包配置：{spec}")
        return 1
    cmd = [sys.executable, "-m", "PyInstaller", str(spec), "--noconfirm"]
    print("执行：" + " ".join(cmd))
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    if rc != 0:
        print("打包失败")
        return rc or 1
    name = "LocalAgent.exe" if sys.platform.startswith("win") else "LocalAgent"
    exe = ROOT / "dist" / name
    if not exe.exists():
        print(f"PyInstaller 退出了 0，但没找到 {exe}")
        return 1
    size = exe.stat().st_size / (1024 * 1024)
    print(f"打包完成：{exe}（{size:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
