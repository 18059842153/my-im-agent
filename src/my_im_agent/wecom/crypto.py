"""企业微信回调消息的加解密与签名校验。

协议要点（官方「消息加解密」文档）：
- AES-256-CBC，key = Base64Decode(EncodingAESKey + "=")，iv = key 前 16 字节
- 明文结构：random(16B) + msg_len(4B, 大端) + msg + corp_id
- PKCS7 填充，块大小 32
- 签名：sha1(sort(token, timestamp, nonce, encrypt) 拼接)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct

from Crypto.Cipher import AES

_BLOCK_SIZE = 32


class WeComCryptoError(Exception):
    """解密 / 校验失败。"""


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        self.token = token
        try:
            key = base64.b64decode(encoding_aes_key + "=")
        except Exception as exc:  # noqa: BLE001 - 统一成业务异常
            raise WeComCryptoError(f"EncodingAESKey 不是合法的 base64：{exc}") from exc
        if len(key) != 32:
            raise WeComCryptoError("EncodingAESKey 必须是 43 个字符")
        self._key = key
        self._iv = key[:16]
        self._corp_id = corp_id.encode("utf-8")

    # --- 签名 ---

    @staticmethod
    def sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
        items = sorted([token, str(timestamp), str(nonce), encrypt])
        return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()

    def check_signature(self, timestamp: str, nonce: str, encrypt: str, signature: str) -> bool:
        return hmac.compare_digest(
            self.sign(self.token, timestamp, nonce, encrypt), signature or ""
        )

    # --- 解密 ---

    def decrypt(self, encrypt_b64: str) -> str:
        try:
            cipher = AES.new(self._key, AES.MODE_CBC, self._iv)
            plain = cipher.decrypt(base64.b64decode(encrypt_b64))
        except Exception as exc:  # noqa: BLE001
            raise WeComCryptoError(f"解密失败：{exc}") from exc

        pad = plain[-1]
        if not 1 <= pad <= _BLOCK_SIZE:
            raise WeComCryptoError("PKCS7 填充不合法")
        plain = plain[:-pad]
        if len(plain) < 20:
            raise WeComCryptoError("明文长度不合法")

        content = plain[16:]  # 跳过 16 字节随机串
        (msg_len,) = struct.unpack(">I", content[:4])
        msg = content[4 : 4 + msg_len]
        corp_id = content[4 + msg_len :]
        if corp_id != self._corp_id:
            raise WeComCryptoError("corp_id 不匹配，消息不是发给本应用的")
        return msg.decode("utf-8")

    # --- 加密（URL 验证测试与被动回复用） ---

    def encrypt(self, msg: str, nonce: bytes | None = None) -> str:
        msg_bytes = msg.encode("utf-8")
        random16 = nonce or os.urandom(16)
        if len(random16) != 16:
            raise ValueError("nonce must be 16 bytes")
        plain = random16 + struct.pack(">I", len(msg_bytes)) + msg_bytes + self._corp_id

        pad = _BLOCK_SIZE - (len(plain) % _BLOCK_SIZE)
        plain += bytes([pad]) * pad

        cipher = AES.new(self._key, AES.MODE_CBC, self._iv)
        return base64.b64encode(cipher.encrypt(plain)).decode("ascii")
