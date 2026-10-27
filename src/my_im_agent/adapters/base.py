"""消息模型与 IM 平台适配器抽象。"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MessageType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    EVENT = "event"


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class InboundMessage:
    """从 IM 平台收到的消息。"""

    content: str
    user_id: str
    conversation_id: str
    message_type: MessageType = MessageType.TEXT
    message_id: str = field(default_factory=_new_id)
    timestamp: float = field(default_factory=time.time)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_text(self) -> bool:
        return self.message_type is MessageType.TEXT


@dataclass
class OutboundMessage:
    """要发回 IM 平台的消息。"""

    content: str
    conversation_id: str
    reply_to: str | None = None
    message_type: MessageType = MessageType.TEXT
    raw: dict[str, Any] = field(default_factory=dict)


# 处理过程中的进度回调：适配器可把它转成流式刷新，让用户看到「正在做什么」
ProgressFn = Callable[[str], Awaitable[None]]

# 收到消息后的处理函数签名。支持流式的适配器会额外传入 progress，
# 不支持的适配器只传 message 即可。
Handler = Callable[..., Awaitable["OutboundMessage | None"]]


class IMAdapter(ABC):
    """对接某个具体 IM 平台需要实现的接口。

    实现类只需关心两件事：
    - start(): 开始接收消息，把消息交给 on_message 处理
    - send():  把 Agent 产出的回复发回去
    """

    name: str = "base"

    @abstractmethod
    async def start(self, on_message: Handler) -> None:
        """启动监听（长连接 / 轮询 / REPL 均可）。"""

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """发送消息，失败时抛异常，由调用方决定是否重试。"""

    async def close(self) -> None:  # noqa: B027 - 可选的钩子，子类按需覆盖
        """可选：释放资源。默认什么都不做。"""
