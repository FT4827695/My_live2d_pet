# secret_store.py
"""
Windows DPAPI 加解密封装。
"""

import base64

try:
    import win32crypt
    _DPAPI_AVAILABLE = True
except Exception:
    win32crypt = None
    _DPAPI_AVAILABLE = False


_ENTROPY = None

_DESCRIPTION = "xllama-secret"


def is_available() -> bool:
    return _DPAPI_AVAILABLE


def encrypt_text(plaintext: str) -> str:
    if not _DPAPI_AVAILABLE:
        raise RuntimeError("DPAPI 不可用")
    if plaintext is None:
        plaintext = ""
    data = plaintext.encode("utf-8")
    try:
        blob = win32crypt.CryptProtectData(
            data,
            _DESCRIPTION,
            _ENTROPY,
            None,
            None,
            0,
        )
    except Exception as e:
        raise RuntimeError(f"DPAPI 加密失败: {e}")
    return base64.b64encode(blob).decode("ascii")


def decrypt_text(token: str) -> str:
    if not _DPAPI_AVAILABLE:
        return ""
    if not token:
        return ""
    try:
        blob = base64.b64decode(token.encode("ascii"), validate=True)
    except Exception:
        return ""
    try:
        _, data = win32crypt.CryptUnprotectData(
            blob,
            _ENTROPY,
            None,
            None,
            0,
        )
    except Exception:
        return ""
    try:
        return data.decode("utf-8")
    except Exception:
        return ""
