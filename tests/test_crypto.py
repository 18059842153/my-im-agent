import base64

import pytest

from my_im_agent.wecom.crypto import WeComCrypto, WeComCryptoError

# 官方文档示例风格的 EncodingAESKey（43 位）
AES_KEY = base64.b64encode(b"0123456789abcdef" * 2).decode()[:43]
TOKEN = "test-token"
CORP_ID = "ww1234567890abcdef"


def make_crypto() -> WeComCrypto:
    return WeComCrypto(TOKEN, AES_KEY, CORP_ID)


def test_encrypt_decrypt_round_trip():
    crypto = make_crypto()
    original = "<xml><Content><![CDATA[明天早上9点提醒我开会]]></Content></xml>"

    encrypted = crypto.encrypt(original)
    decrypted = crypto.decrypt(encrypted)

    assert decrypted == original


def test_decrypt_rejects_corp_id_mismatch():
    crypto = make_crypto()
    other = WeComCrypto(TOKEN, AES_KEY, "wwothercorp000000")
    encrypted = other.encrypt("hello")

    with pytest.raises(WeComCryptoError, match="corp_id"):
        crypto.decrypt(encrypted)


def test_decrypt_rejects_garbage():
    crypto = make_crypto()
    with pytest.raises(WeComCryptoError):
        crypto.decrypt(base64.b64encode(b"not-a-valid-ciphertext-at-all!!").decode())


def test_signature_check():
    crypto = make_crypto()
    timestamp, nonce = "1700000000", "abc123"
    encrypt = crypto.encrypt("hello")
    signature = crypto.sign(TOKEN, timestamp, nonce, encrypt)

    assert crypto.check_signature(timestamp, nonce, encrypt, signature)
    assert not crypto.check_signature(timestamp, nonce, encrypt, "bad-signature")
    assert not crypto.check_signature(timestamp, nonce, "tampered", signature)


def test_invalid_aes_key_rejected():
    with pytest.raises(WeComCryptoError):
        WeComCrypto(TOKEN, "too-short", CORP_ID)
