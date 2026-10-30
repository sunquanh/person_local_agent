# -*- coding: utf-8 -*-
"""API Key 加解密（纯标准库，零第三方依赖，离线机可用）。

用法:
    python encrypt_key.py genkey                      # 生成/查看机器密钥
    python encrypt_key.py encrypt "sk-你的key"         # 加密 -> 输出 ENC:...
    python encrypt_key.py decrypt "ENC:gAAAA..."      # 解密 -> 输出明文

约定（与老项目 agent_fixed/agent_gui 一致）:
    - 配置文件里存 ``"api_key": "ENC:<密文>"``
    - 程序启动时检测 ``ENC:`` 前缀并自动调用 ``decrypt_key`` 解密
    - 本模块提供 ``encrypt_key(plain) -> str`` 与 ``decrypt_key(token) -> str``
      （token 可带或不带 ENC: 前缀）

安全说明:
    - 密钥存于机器级密钥文件（默认 ``~/.local_agent_v2.key``），可用环境变量
      ``AGENT_KEY`` 覆盖（如放 U 盘随身携带）。
    - 加密使用 PBKDF2-HMAC-SHA256 派生密钥 + HMAC-SHA256 CTR 流式加密 + 完整性标签，
      用于防止配置文件中的 key 被直接阅读；密钥文件丢失后加密内容无法解密，
      请先备份密钥文件或用 ``AGENT_KEY`` 记住密钥。
"""
import base64
import hashlib
import hmac
import os
import struct
import sys

PREFIX = "ENC:"
_VERSION = b"v1"
_PBKDF2_ITER = 200_000
_SALT_LEN = 16
_IV_LEN = 16
_TAG_LEN = 32


def _master_key() -> bytes:
    """返回机器级主密钥（32 字节）。env AGENT_KEY 优先，否则读/生成 ~/.local_agent_v2.key。

    注意：两条路径必须统一为 sha256(落盘内容)，否则首次生成后跨进程解密必然不一致。
    """
    env = os.environ.get("AGENT_KEY", "").strip()
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    path = os.path.join(os.path.expanduser("~"), ".local_agent_v2.key")
    data = None
    if os.path.isfile(path):
        try:
            raw = open(path, "rb").read().strip()
            if raw:
                data = raw
        except OSError:
            pass
    if data is None:
        # 首次使用：生成新密钥并落盘（权限 600 尽量防止他用户读取）
        data = base64.urlsafe_b64encode(os.urandom(32))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except OSError:
            pass  # 密钥文件写不了就用进程内随机密钥（每次启动变化，仅本次会话可用）
    return hashlib.sha256(data).digest()


def _xor_stream(data: bytes, dk: bytes, iv: bytes) -> bytes:
    """HMAC-SHA256 CTR 密钥流 XOR（纯标准库流式加密）。"""
    out = bytearray()
    ctr = 0
    for i in range(0, len(data), 32):
        block = hmac.new(dk, iv + struct.pack(">Q", ctr), hashlib.sha256).digest()
        out += bytes(b ^ block[j % 32] for j, b in enumerate(data[i:i + 32]))
        ctr += 1
    return bytes(out)


def encrypt_key(plain: str) -> str:
    """加密明文，返回不带 ENC: 前缀的密文字符串。"""
    master = _master_key()
    data = str(plain or "").encode("utf-8")
    salt = os.urandom(_SALT_LEN)
    iv = os.urandom(_IV_LEN)
    dk = hashlib.pbkdf2_hmac("sha256", master, salt, _PBKDF2_ITER, dklen=32)
    ct = _xor_stream(data, dk, iv)
    tag = hmac.new(dk, salt + iv + ct, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(_VERSION + salt + iv + ct + tag).decode("ascii")
    return token


def decrypt_key(token: str) -> str:
    """解密 ENC: 密文（token 可带或不带 ENC: 前缀），失败抛 ValueError。"""
    s = str(token or "").strip()
    if s.startswith(PREFIX):
        s = s[len(PREFIX):]
    raw = base64.urlsafe_b64decode(s.encode("ascii"))
    if not raw.startswith(_VERSION):
        raise ValueError("无法识别的密文格式")
    body = raw[len(_VERSION):]
    if len(body) < _SALT_LEN + _IV_LEN + _TAG_LEN:
        raise ValueError("密文长度不正确")
    salt = body[:_SALT_LEN]
    iv = body[_SALT_LEN:_SALT_LEN + _IV_LEN]
    ct = body[_SALT_LEN + _IV_LEN:-_TAG_LEN]
    tag = body[-_TAG_LEN:]
    master = _master_key()
    dk = hashlib.pbkdf2_hmac("sha256", master, salt, _PBKDF2_ITER, dklen=32)
    expect = hmac.new(dk, salt + iv + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, tag):
        raise ValueError("解密失败：密钥不匹配或密文被篡改")
    return _xor_stream(ct, dk, iv).decode("utf-8", errors="replace")


def _cli():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "genkey":
        import base64 as _b64
        k = _b64.urlsafe_b64encode(_master_key())
        print(f"机器密钥(HMAC-SHA256 派生源): {k.decode()}")
        print(f"密钥文件: {os.path.join(os.path.expanduser('~'), '.local_agent_v2.key')}")
        print("提示: 可用环境变量 AGENT_KEY 覆盖（export AGENT_KEY=xxx）")
    elif cmd == "encrypt":
        if len(sys.argv) < 3:
            print("用法: python encrypt_key.py encrypt \"你的key\"")
            return
        print(PREFIX + encrypt_key(sys.argv[2]))
    elif cmd == "decrypt":
        if len(sys.argv) < 3:
            print("用法: python encrypt_key.py decrypt \"ENC:...\"")
            return
        try:
            print(decrypt_key(sys.argv[2]))
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"未知命令: {cmd}\n{__doc__}")
        sys.exit(1)


if __name__ == "__main__":
    _cli()
