"""企业微信「智能机器人」适配器：WebSocket 长连接 + 流式进度。

和自建应用的区别：
- **不用公网 IP、不用回调地址、不用加解密**（服务主动外连 wss://openws.work.weixin.qq.com）
- **流式回复**：同一条气泡被反复刷新，用户能看到「正在理解 → 正在设置闹钟 → 结果」
- 闹钟到点用 `aibot_send_msg` 主动推送（不需要用户先说话）

协议由官方 SDK `wecom-aibot-python-sdk` 承载，本文件只做业务映射。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from my_im_agent.adapters.base import (
    Handler,
    IMAdapter,
    InboundMessage,
    MessageType,
    OutboundMessage,
)
from my_im_agent.config import Settings

logger = logging.getLogger(__name__)

_SEEN_CAPACITY = 500


def strip_group_mention(content: str, bot_name: str | None = None) -> str:
    """群聊里 @机器人 会把「@名字」带进正文，去掉它。

    机器人名字可能包含空格（比如「IM Agent」），所以优先用配置的名字精确匹配；
    没配时退化成「去掉第一个 @xxx 片段」。
    """
    text = content.lstrip()
    if bot_name and text.startswith(f"@{bot_name}"):
        return text[len(bot_name) + 1 :].lstrip()
    if text.startswith("@"):
        parts = text.split(" ", 1)
        if len(parts) == 2:
            return parts[1].lstrip()
    return text


def build_inbound(body: dict[str, Any], bot_name: str | None = None) -> InboundMessage | None:
    """把企业微信的 aibot_msg_callback.body 映射成 InboundMessage。

    非文本消息返回 None（当前只处理文字）。
    """
    if body.get("msgtype") != "text":
        return None

    content = strip_group_mention((body.get("text") or {}).get("content", ""), bot_name)
    if not content:
        return None

    user_id = (body.get("from") or {}).get("userid", "")
    chat_type = body.get("chattype", "single")
    # 单聊没有 chatid，用 userid 当会话 id；群聊用 chatid
    conversation_id = body.get("chatid") or user_id

    return InboundMessage(
        content=content,
        user_id=user_id,
        conversation_id=conversation_id,
        message_type=MessageType.TEXT,
        message_id=body.get("msgid", ""),
        raw={"chattype": chat_type},
    )


class WeComBotAdapter(IMAdapter):
    """智能机器人长连接适配器。"""

    name = "wecom-bot"

    def __init__(self, settings: Settings) -> None:
        settings.require_wecom_bot()
        self.settings = settings
        self._client: Any = None
        self._seen_ids: deque[str] = deque(maxlen=_SEEN_CAPACITY)
        self._seen_set: set[str] = set()
        # 供 --news-now 等需要"等连接就绪"的场景使用
        self.ready = asyncio.Event()

    # --- 生命周期 ---

    async def start(self, on_message: Handler) -> None:
        from aibot import WSClient, WSClientOptions

        client = WSClient(
            WSClientOptions(
                bot_id=self.settings.wecom_bot_id,
                secret=self.settings.wecom_bot_secret,
                max_reconnect_attempts=-1,  # 无限重连：服务器常驻
            )
        )
        self._client = client

        @client.on("message")
        async def _on_message(frame: dict[str, Any]) -> None:
            await self._dispatch(frame, on_message)

        @client.on("authenticated")
        def _on_auth() -> None:
            self.ready.set()
            logger.info("WeCom bot authenticated (long connection ready)")

        @client.on("error")
        def _on_error(error: Any) -> None:
            logger.error("WeCom bot error: %s", error)

        @client.on("disconnected")
        def _on_disconnected(reason: Any) -> None:
            logger.warning("WeCom bot disconnected: %s", reason)

        await client.connect()
        logger.info("WeCom bot long connection established")
        try:
            await asyncio.Event().wait()  # 常驻，SDK 内部负责心跳与重连
        finally:
            client.disconnect()

    async def send(self, message: OutboundMessage) -> None:
        """主动推送（闹钟到点用）。单聊 chatid 就是 userid。"""
        if self._client is None:
            raise RuntimeError("WeCom bot adapter not started")
        target = message.conversation_id or message.raw.get("user_id", "")
        await self._client.send_message(
            target,
            {"msgtype": "markdown", "markdown": {"content": message.content}},
        )

    async def close(self) -> None:
        if self._client is not None:
            self._client.disconnect()

    # --- 消息处理 ---

    async def _dispatch(self, frame: dict[str, Any], on_message: Handler) -> None:
        from aibot import generate_req_id

        body = frame.get("body") or {}
        msg_id = body.get("msgid", "")

        if msg_id and self._is_duplicate(msg_id):
            return

        inbound = build_inbound(body, self.settings.wecom_bot_name or None)
        stream_id = generate_req_id("stream")

        if inbound is None:
            # 图片/文件/语音暂不支持，明确回一句，别让用户干等
            await self._reply(frame, stream_id, "目前只支持文字消息哦。", finish=True)
            return

        steps: list[str] = []

        async def progress(text: str) -> None:
            """把每一步追加成一行，整段刷新同一条气泡。"""
            steps.append(text)
            await self._reply(frame, stream_id, "\n".join(steps), finish=False)

        try:
            reply = await on_message(inbound, progress)
        except Exception:
            logger.exception("failed to handle bot message %s", msg_id)
            await self._reply(frame, stream_id, "处理出错了，请稍后再试。", finish=True)
            return

        if reply is None:
            return
        await self._reply(frame, stream_id, reply.content, finish=True)

    async def _reply(self, frame: dict[str, Any], stream_id: str, content: str, *, finish: bool) -> None:
        try:
            await self._client.reply_stream(frame, stream_id, content, finish=finish)
        except Exception:
            # 流式刷新失败不该拖垮主流程：最终结果会走 finish=True 再试一次
            logger.warning("stream reply failed (finish=%s)", finish, exc_info=True)

    def _is_duplicate(self, msg_id: str) -> bool:
        if msg_id in self._seen_set:
            return True
        if len(self._seen_ids) == _SEEN_CAPACITY:
            self._seen_set.discard(self._seen_ids[0])
        self._seen_ids.append(msg_id)
        self._seen_set.add(msg_id)
        return False
