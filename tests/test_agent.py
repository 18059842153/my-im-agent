import pytest

from my_im_agent.adapters.base import InboundMessage, MessageType
from my_im_agent.agent import Agent
from my_im_agent.config import Settings


def make_settings(**overrides) -> Settings:
    base = Settings(
        llm_base_url="https://example.invalid/v1",
        llm_api_key="",
        llm_model="test-model",
        system_prompt="sys",
        max_turns=20,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class FakeProvider:
    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages) -> str:
        self.calls.append(list(messages))
        return self.reply


def inbound(text: str, conv: str = "c1") -> InboundMessage:
    return InboundMessage(content=text, user_id="u1", conversation_id=conv)


@pytest.mark.asyncio
async def test_handle_returns_reply_and_records_history():
    provider = FakeProvider("hello")
    agent = Agent(make_settings(), provider=provider)

    reply = await agent.handle(inbound("hi"))

    assert reply is not None
    assert reply.content == "hello"
    assert reply.conversation_id == "c1"
    assert agent.history("c1") == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


@pytest.mark.asyncio
async def test_system_prompt_is_prepended():
    provider = FakeProvider()
    agent = Agent(make_settings(system_prompt="be brief"), provider=provider)

    await agent.handle(inbound("hi"))

    assert provider.calls[0][0] == {"role": "system", "content": "be brief"}


@pytest.mark.asyncio
async def test_blank_message_is_ignored():
    provider = FakeProvider()
    agent = Agent(make_settings(), provider=provider)

    assert await agent.handle(inbound("   ")) is None
    assert provider.calls == []


@pytest.mark.asyncio
async def test_non_text_message_is_ignored():
    provider = FakeProvider()
    agent = Agent(make_settings(), provider=provider)

    msg = InboundMessage(
        content="",
        user_id="u1",
        conversation_id="c1",
        message_type=MessageType.IMAGE,
    )

    assert await agent.handle(msg) is None


@pytest.mark.asyncio
async def test_history_is_capped_by_max_turns():
    provider = FakeProvider()
    agent = Agent(make_settings(max_turns=2), provider=provider)

    for i in range(3):
        await agent.handle(inbound(f"m{i}"))

    assert len(agent.history("c1")) == 2


@pytest.mark.asyncio
async def test_conversations_are_isolated():
    provider = FakeProvider()
    agent = Agent(make_settings(), provider=provider)

    await agent.handle(inbound("a", conv="room1"))
    await agent.handle(inbound("b", conv="room2"))

    assert len(agent.history("room1")) == 2
    assert len(agent.history("room2")) == 2
    assert agent.history("room1")[0]["content"] == "a"

    agent.reset("room1")
    assert agent.history("room1") == []
