"""Agent 编排层：把「消息 -> 大模型 -> 回复」串起来，并维护每个会话的历史。"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from typing import Protocol

import httpx

from my_im_agent.adapters.base import InboundMessage, MessageType, OutboundMessage
from my_im_agent.config import Settings

Role = dict[str, str]


class LLMProvider(Protocol):
    """大模型调用抽象。换模型只要换实现，不动上层逻辑。"""

    async def chat(self, messages: Sequence[Role]) -> str: ...


class EchoProvider:
    """没有配置 API Key 时的兜底实现，不联网，直接回显最后一条用户输入。"""

    async def chat(self, messages: Sequence[Role]) -> str:
        last_user = next(
            (m["content"] for m in reversed(list(messages)) if m["role"] == "user"),
            "",
        )
        return f"[echo] {last_user}"


class OpenAICompatProvider:
    """OpenAI 兼容的 Chat Completions 接口（也适用于多数国产大模型的兼容端点）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=settings.request_timeout,
        )

    async def chat(self, messages: Sequence[Role]) -> str:
        payload = {
            "model": self._settings.llm_model,
            "messages": list(messages),
        }
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def aclose(self) -> None:
        await self._client.aclose()


class Agent:
    """维护会话历史并调用大模型生成回复。"""

    def __init__(self, settings: Settings, provider: LLMProvider | None = None) -> None:
        self.settings = settings
        self.provider: LLMProvider = provider or self._default_provider(settings)
        self._histories: dict[str, deque[Role]] = defaultdict(
            lambda: deque(maxlen=settings.max_turns)
        )

    @staticmethod
    def _default_provider(settings: Settings) -> LLMProvider:
        if settings.llm_enabled:
            return OpenAICompatProvider(settings)
        return EchoProvider()

    def history(self, conversation_id: str) -> list[Role]:
        return list(self._histories[conversation_id])

    def reset(self, conversation_id: str) -> None:
        self._histories.pop(conversation_id, None)

    async def handle(self, message: InboundMessage) -> OutboundMessage | None:
        """处理一条入站消息，返回出站回复；返回 None 表示不需要回复。"""
        if not message.is_text or not message.content.strip():
            return None

        history = self._histories[message.conversation_id]
        history.append({"role": "user", "content": message.content})

        messages: list[Role] = [{"role": "system", "content": self.settings.system_prompt}]
        messages.extend(history)

        answer = await self.provider.chat(messages)
        history.append({"role": "assistant", "content": answer})

        return OutboundMessage(
            content=answer,
            conversation_id=message.conversation_id,
            reply_to=message.message_id,
            message_type=MessageType.TEXT,
        )

    async def aclose(self) -> None:
        close = getattr(self.provider, "aclose", None)
        if close is not None:
            await close()
