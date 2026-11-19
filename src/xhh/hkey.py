"""小黑盒 Web 端动态签名 (hkey / _time / nonce) 实现."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHARSET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
WEB_VERSION = "999.0.4"
RND_V16_KEY_B64 = "HFFSTsqmvG3uGxPNXDQW+1VARfqgG4nT6LS1PX7F8NQ="
RND_V16_AAD = b"heybox-rnd:v16"


def _r3(val: int) -> int:
    return ((val << 1) ^ 27) & 255 if (val & 128) else (val << 1)


def _dc(val: int) -> int:
    return _r3(val) ^ val


def _sf(val: int) -> int:
    return _dc(_r3(val))


def _hh(val: int) -> int:
    return _sf(_dc(_r3(val)))


def _fg(val: int) -> int:
    return _hh(val) ^ _sf(val) ^ _dc(val)


def _mix_columns(arr: list[int]) -> list[int]:
    """类 AES MixColumns 变换：原地更新前 4 个字节并保留末尾字节."""
    out = [
        _fg(arr[0]) ^ _hh(arr[1]) ^ _sf(arr[2]) ^ _dc(arr[3]),
        _dc(arr[0]) ^ _fg(arr[1]) ^ _hh(arr[2]) ^ _sf(arr[3]),
        _sf(arr[0]) ^ _dc(arr[1]) ^ _fg(arr[2]) ^ _hh(arr[3]),
        _hh(arr[0]) ^ _sf(arr[1]) ^ _dc(arr[2]) ^ _fg(arr[3]),
    ]
    arr[:4] = out
    return arr


def _map_slice(text: str, table: str, limit: int) -> str:
    sub = table[:limit]
    return "".join(sub[ord(ch) % len(sub)] for ch in text)


def _map_full(text: str, table: str) -> str:
    return "".join(table[ord(ch) % len(table)] for ch in text)


def _interleave(parts: list[str]) -> str:
    max_len = max((len(p) for p in parts), default=0)
    buf: list[str] = []
    for idx in range(max_len):
        for part in parts:
            if idx < len(part):
                buf.append(part[idx])
    return "".join(buf)


def compute_hkey(path: str, adjusted_time: int, nonce: str) -> str:
    """根据规范化路径、偏移时间戳与 nonce 计算 hkey."""
    segments = [seg for seg in path.split("/") if seg]
    norm_path = f"/{'/'.join(segments)}/" if segments else "/"

    part_time = _map_slice(str(adjusted_time), CHARSET, -2)
    part_path = _map_full(norm_path, CHARSET)
    part_nonce = _map_full(nonce, CHARSET)

    interleaved = _interleave([part_time, part_path, part_nonce])[:20]
    digest = hashlib.md5(interleaved.encode("utf-8")).hexdigest()

    tail_codes = [ord(ch) for ch in digest[-6:]]
    suffix_num = sum(_mix_columns(tail_codes)) % 100
    prefix = _map_slice(digest[:5], CHARSET, -4)
    return f"{prefix}{suffix_num:02d}"


def generate_hkey_params(path: str, timestamp: int | None = None) -> dict[str, str | int]:
    """生成小黑盒 Web API 请求所需的公共参数与动态签名."""
    t = timestamp if timestamp is not None else int(time.time())
    seed = f"{t}{time.time()}{random.random()}"
    nonce = hashlib.md5(seed.encode("utf-8")).hexdigest().upper()
    # 对应前端 dP.g(e, t, n) = Tr(e, t + 1, n)
    hkey = compute_hkey(path, t + 1, nonce)
    return {
        "os_type": "web",
        "app": "heybox",
        "version": WEB_VERSION,
        "x_client_type": "web",
        "x_os_type": "Mac",
        "x_app": "heybox",
        "x_client_version": "999.999.999",
        "hkey": hkey,
        "_time": t,
        "nonce": nonce,
    }


def compute_rnd_v16(
    path: str,
    timestamp: int | str,
    nonce: str,
    *,
    iv: bytes | None = None,
) -> str:
    """生成小黑盒 Web 端 v16 _rnd (AES-256-GCM) 请求签名."""
    segments = [seg for seg in path.split("/") if seg]
    norm_path = f"/{'/'.join(segments)}/" if segments else "/"
    key = base64.b64decode(RND_V16_KEY_B64)
    nonce_iv = iv if iv is not None else os.urandom(12)
    if len(nonce_iv) != 12:
        raise ValueError("RND16 IV 长度必须为 12 字节")
    plaintext = json.dumps(
        [str(timestamp), str(nonce), norm_path],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    encrypted = AESGCM(key).encrypt(nonce_iv, plaintext, RND_V16_AAD)
    token = base64.urlsafe_b64encode(nonce_iv + encrypted).decode("ascii").rstrip("=")
    return f"16:{token}"
