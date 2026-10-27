"""企业微信自建应用适配器：回调收消息 + 队列解耦 + 主动回复。

时序（对照架构图里的「响应消息占位 → 消除 5 秒时限」）：
1. 企业微信 POST 加密消息到回调地址
2. 签名校验 + 解密 → 放入内存队列 → 立即返回 success（不做任何耗时操作）
3. 后台 worker 消费队列：交给 Brain 处理，结果通过主动发消息 API 推回去
   —— 彻底绕开被动回复 5 秒时限

MsgId 去重：企业微信未收到 success 会重试，同一消息可能投递多次。
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from collections import deque

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from my_im_agent.adapters.base import (
    Handler,
    IMAdapter,
    InboundMessage,
    MessageType,
    OutboundMessage,
)
from my_im_agent.config import Settings
from my_im_agent.wecom.api import WeComAPI
from my_im_agent.wecom.crypto import WeComCrypto, WeComCryptoError

logger = logging.getLogger(__name__)

_SEEN_CAPACITY = 500


def _xml_field(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


class WeComAdapter(IMAdapter):
    name = "wecom"

    def __init__(self, settings: Settings) -> None:
        settings.require_wecom()
        self.settings = settings
        self.crypto = WeComCrypto(
            settings.wecom_token,
            settings.wecom_encoding_aes_key,
            settings.wecom_corp_id,
        )
        self.api = WeComAPI(
            settings.wecom_corp_id,
            settings.wecom_secret,
            settings.wecom_agent_id,
        )
        self.queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._seen_ids: deque[str] = deque(maxlen=_SEEN_CAPACITY)
        self._seen_set: set[str] = set()
        self._on_message: Handler | None = None
        self.app = self._build_app()

    # --- HTTP 层 ---

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="my-im-agent wecom adapter")
        path = self.settings.wecom_callback_path

        @app.get(path)
        async def verify_url(
            msg_signature: str, timestamp: str, nonce: str, echostr: str
        ) -> PlainTextResponse:
            """企业微信保存回调配置时的 URL 验证。"""
            if not self.crypto.check_signature(timestamp, nonce, echostr, msg_signature):
                raise HTTPException(status_code=403, detail="invalid signature")
            try:
                plain = self.crypto.decrypt(echostr)
            except WeComCryptoError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            return PlainTextResponse(plain)

        @app.post(path)
        async def receive_message(
            request: Request, msg_signature: str, timestamp: str, nonce: str
        ) -> PlainTextResponse:
            body = await request.body()
            try:
                outer = ET.fromstring(body.decode("utf-8"))
            except ET.ParseError as exc:
                raise HTTPException(status_code=400, detail="invalid xml") from exc

            encrypt = _xml_field(outer, "Encrypt")
            if not encrypt:
                raise HTTPException(status_code=400, detail="missing Encrypt")
            if not self.crypto.check_signature(timestamp, nonce, encrypt, msg_signature):
                raise HTTPException(status_code=403, detail="invalid signature")

            try:
                inner_xml = self.crypto.decrypt(encrypt)
                inner = ET.fromstring(inner_xml)
            except (WeComCryptoError, ET.ParseError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            msg_type = _xml_field(inner, "MsgType")
            msg_id = _xml_field(inner, "MsgId") or f"{timestamp}-{nonce}"
            from_user = _xml_field(inner, "FromUserName")
            content = _xml_inner_content(inner)

            if msg_type == "text" and from_user and content and not self._is_duplicate(msg_id):
                await self.queue.put(
                    InboundMessage(
                        content=content,
                        user_id=from_user,
                        conversation_id=from_user,  # 单聊：以成员 id 作为会话 id
                        message_type=MessageType.TEXT,
                        message_id=msg_id,
                        raw={"agent_id": _xml_field(inner, "AgentID")},
                    )
                )
            # 无论消息类型是否被处理，都立即回 success，避免企业微信重试
            return PlainTextResponse("success")

        @app.get("/healthz")
        async def healthz() -> dict:
            return {"ok": True, "queue": self.queue.qsize()}

        return app

    def _is_duplicate(self, msg_id: str) -> bool:
        if msg_id in self._seen_set:
            return True
        if len(self._seen_ids) == _SEEN_CAPACITY:
            self._seen_set.discard(self._seen_ids[0])
        self._seen_ids.append(msg_id)
        self._seen_set.add(msg_id)
        return False

    # --- IMAdapter 接口 ---

    async def start(self, on_message: Handler) -> None:
        self._on_message = on_message
        worker = asyncio.create_task(self._work(), name="wecom-worker")
        server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host=self.settings.wecom_callback_host,
                port=self.settings.wecom_callback_port,
                log_level="info",
            )
        )
        logger.info(
            "WeCom callback listening on http://%s:%s%s",
            self.settings.wecom_callback_host,
            self.settings.wecom_callback_port,
            self.settings.wecom_callback_path,
        )
        try:
            await server.serve()
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    async def send(self, message: OutboundMessage) -> None:
        user_id = message.raw.get("user_id") or message.conversation_id
        if message.raw.get("markdown"):
            await self.api.send_markdown(user_id, message.content)
        else:
            await self.api.send_text(user_id, message.content)

    async def close(self) -> None:
        await self.api.aclose()

    # --- 后台 worker ---

    async def _work(self) -> None:
        assert self._on_message is not None
        while True:
            message = await self.queue.get()
            try:
                reply = await self._on_message(message)
                if reply is not None:
                    await self.send(reply)
            except Exception:
                logger.exception("failed to handle wecom message %s", message.message_id)
                # 出错也给用户一个明确反馈，而不是无声失败
                try:
                    await self.api.send_text(message.user_id, "处理出错了，请稍后再试。")
                except Exception:
                    logger.exception("failed to send error notice")


def _xml_inner_content(root: ET.Element) -> str:
    return _xml_field(root, "Content")
