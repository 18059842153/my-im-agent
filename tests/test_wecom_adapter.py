import base64

import pytest
from fastapi.testclient import TestClient

from my_im_agent.adapters.base import OutboundMessage
from my_im_agent.config import Settings
from my_im_agent.wecom.adapter import WeComAdapter
from my_im_agent.wecom.crypto import WeComCrypto

AES_KEY = base64.b64encode(b"0123456789abcdef" * 2).decode()[:43]
TOKEN = "test-token"
CORP_ID = "ww1234567890abcdef"


def make_settings() -> Settings:
    return Settings(
        wecom_corp_id=CORP_ID,
        wecom_agent_id=1000002,
        wecom_secret="secret",
        wecom_token=TOKEN,
        wecom_encoding_aes_key=AES_KEY,
    )


def make_adapter() -> WeComAdapter:
    return WeComAdapter(make_settings())


def make_signed_query(crypto: WeComCrypto, encrypt: str, timestamp="1700000000", nonce="abc"):
    return {
        "msg_signature": crypto.sign(TOKEN, timestamp, nonce, encrypt),
        "timestamp": timestamp,
        "nonce": nonce,
    }


def build_message_xml(content: str, msg_id: str = "msg-1", from_user: str = "ZhangSan") -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        "<AgentID>1000002</AgentID>"
        "</xml>"
    )


def wrap_outer_xml(encrypt: str) -> bytes:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        "<AgentID><![CDATA[1000002]]></AgentID>"
        "</xml>"
    ).encode()


def post_message(client: TestClient, crypto: WeComCrypto, content: str, msg_id: str):
    encrypt = crypto.encrypt(build_message_xml(content, msg_id=msg_id))
    return client.post(
        "/wecom/callback",
        params=make_signed_query(crypto, encrypt),
        content=wrap_outer_xml(encrypt),
    )


class TestUrlVerification:
    def test_valid_echostr_is_decrypted_and_returned(self):
        adapter = make_adapter()
        client = TestClient(adapter.app)
        echostr = adapter.crypto.encrypt("random-echo-string")

        resp = client.get(
            "/wecom/callback",
            params={**make_signed_query(adapter.crypto, echostr), "echostr": echostr},
        )

        assert resp.status_code == 200
        assert resp.text == "random-echo-string"

    def test_bad_signature_rejected(self):
        adapter = make_adapter()
        client = TestClient(adapter.app)
        echostr = adapter.crypto.encrypt("x")

        resp = client.get(
            "/wecom/callback",
            params={"msg_signature": "bad", "timestamp": "1", "nonce": "n", "echostr": echostr},
        )

        assert resp.status_code == 403


class TestReceiveMessage:
    def test_valid_message_goes_to_queue_and_returns_success(self):
        adapter = make_adapter()
        client = TestClient(adapter.app)

        resp = post_message(client, adapter.crypto, "明天早上9点提醒我开会", "msg-1")

        assert resp.status_code == 200
        assert resp.text == "success"
        assert adapter.queue.qsize() == 1

        msg = adapter.queue.get_nowait()
        assert msg.content == "明天早上9点提醒我开会"
        assert msg.user_id == "ZhangSan"
        assert msg.conversation_id == "ZhangSan"
        assert msg.message_id == "msg-1"

    def test_bad_signature_rejected(self):
        adapter = make_adapter()
        client = TestClient(adapter.app)
        encrypt = adapter.crypto.encrypt(build_message_xml("hi"))

        resp = client.post(
            "/wecom/callback",
            params={"msg_signature": "bad", "timestamp": "1", "nonce": "n"},
            content=wrap_outer_xml(encrypt),
        )

        assert resp.status_code == 403
        assert adapter.queue.qsize() == 0

    def test_duplicate_msg_id_deduplicated(self):
        adapter = make_adapter()
        client = TestClient(adapter.app)

        post_message(client, adapter.crypto, "hi", "msg-dup")
        post_message(client, adapter.crypto, "hi", "msg-dup")

        assert adapter.queue.qsize() == 1

    def test_tampered_body_rejected(self):
        adapter = make_adapter()
        client = TestClient(adapter.app)
        good = adapter.crypto.encrypt(build_message_xml("hi", msg_id="m"))
        other = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
        encrypt_other = other.encrypt("<xml><Content><![CDATA[tampered]]></Content></xml>")
        # 用 good 的签名 + 别的密文
        resp = client.post(
            "/wecom/callback",
            params=make_signed_query(adapter.crypto, good),
            content=wrap_outer_xml(encrypt_other),
        )

        assert resp.status_code == 403


class TestConfigGate:
    def test_missing_wecom_config_fails_fast(self):
        with pytest.raises(RuntimeError, match="WECOM_"):
            WeComAdapter(Settings())


class TestSend:
    @pytest.mark.asyncio
    async def test_send_uses_user_id_from_raw(self):
        adapter = make_adapter()
        sent: list[tuple[str, str]] = []

        async def fake_send(user_id: str, content: str) -> None:
            sent.append((user_id, content))

        adapter.api.send_text = fake_send  # type: ignore[method-assign]

        await adapter.send(
            OutboundMessage(content="回复内容", conversation_id="ZhangSan", raw={"user_id": "ZhangSan"})
        )

        assert sent == [("ZhangSan", "回复内容")]
