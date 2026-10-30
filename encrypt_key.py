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


def _key_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".local_agent_v2.key")


def _read_key_file(path: str):
    """读密钥文件原文：文件不存在返回 None；存在但读不出或为空则抛错。

    存在≠可读时绝不返回 None——旧版把"读失败"与"文件不存在"混成同一
    个 None，后续 O_TRUNC 覆写会把历史密文的唯一密钥永久销毁。
    """
    if not os.path.isfile(path):
        return None
    try:
        raw = open(path, "rb").read().strip()
    except OSError as e:
        raise OSError(f"机器密钥文件无法读取: {path} ({e})")
    if not raw:
        raise OSError(f"机器密钥文件为空: {path}")
    return raw


def _master_key() -> bytes:
    """返回机器级主密钥（32 字节）。env AGENT_KEY 优先，否则读/生成密钥文件。

    两条路径统一为 sha256(原文) 派生：把文件原文设为 AGENT_KEY 时，
    两台机器得到同一主密钥、能互相解密。

    失败语义一律抛错、绝不静默降级：密钥文件存在但读不出时不覆写；
    写失败时不生成临时密钥（否则本轮加密的密文下次谁也解不开）。
    """
    env = os.environ.get("AGENT_KEY", "").strip()
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    path = _key_path()
    data = _read_key_file(path)
    if data is not None:
        return hashlib.sha256(data).digest()
    # 文件不存在：独占创建（双进程同时创建时，后来者读先到者写好的文件）
    data = base64.urlsafe_b64encode(os.urandom(32))
    fd = None
    try:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            data2 = _read_key_file(path)
            if data2 is None:
                raise OSError(f"机器密钥文件创建冲突且无法读取: {path}")
            return hashlib.sha256(data2).digest()
        except OSError as e:
            raise OSError(f"机器密钥文件无法写入: {path} ({e})")
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data)
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # 写失败只剩半截文件时删掉：否则下次会把半截内容当成密钥，
        # 与本次失败前加密的密文（无）对不上，反而制造"能加密不能解密"。
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
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
        # 打印密钥文件原文：把它设为 AGENT_KEY 可在别处得到同一主密钥。
        # 旧版打印的是 sha256(原文) 的派生值，填回 AGENT_KEY 会得到另
        # 一个主密钥、旧密文解不开——误导性输出，已修正。
        # AGENT_KEY 已设置时生效密钥由它派生，文件内容不是当前密钥：
        # 只提示备份环境变量本身，不回显。
        if os.environ.get("AGENT_KEY", "").strip():
            print("当前 AGENT_KEY 环境变量已设置，生效的主密钥由它派生。")
            print("提示: 备份该环境变量的值即可在别处解密同一批密文；不要发到聊天记录/仓库。")
            return
        _master_key()  # 确保密钥文件存在（不存在则新建）
        try:
            content = open(_key_path(), "rb").read().strip().decode("ascii")
        except OSError as e:
            print(f"❌ 无法读取密钥文件: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"机器密钥: {content}")
        print(f"密钥文件: {_key_path()}")
        print("提示: 在别的机器上把该值设为环境变量 AGENT_KEY，可解密同一批密文；请离线保存，不要发到聊天记录/仓库。")
    elif cmd == "encrypt":
        if len(sys.argv) < 3:
            print("用法: python encrypt_key.py encrypt \"你的key\"")
            return
        try:
            print(PREFIX + encrypt_key(sys.argv[2]))
        except OSError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "decrypt":
        if len(sys.argv) < 3:
            print("用法: python encrypt_key.py decrypt \"ENC:...\"")
            return
        try:
            print(decrypt_key(sys.argv[2]))
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            print(f"❌ {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"未知命令: {cmd}\n{__doc__}")
        sys.exit(1)


if __name__ == "__main__":
    _cli()
