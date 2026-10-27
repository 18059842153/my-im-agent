from datetime import datetime, timedelta
from pathlib import Path

import pytest

from my_im_agent.adapters.base import InboundMessage
from my_im_agent.alarms import AlarmStore
from my_im_agent.config import Settings
from my_im_agent.core import Brain, parse_local_time
from my_im_agent.llm import AlarmBrief, IntentResult


def make_settings() -> Settings:
    return Settings(
        llm_base_url="https://example.invalid/v1",
        llm_api_key="",
        llm_model="test-model",
        system_prompt="sys",
        max_turns=20,
    )


class FakeEngine:
    """返回预设的 IntentResult，让 host 逻辑可以脱离真实模型测试。"""

    def __init__(self, result: IntentResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def route(self, *, now, user_text, history, alarms) -> IntentResult:
        self.calls.append(
            {"now": now, "user_text": user_text, "history": history, "alarms": alarms}
        )
        return self.result


def make_brain(tmp_path: Path, result: IntentResult) -> tuple[Brain, FakeEngine]:
    engine = FakeEngine(result)
    store = AlarmStore(tmp_path / "alarms.json")
    return Brain(engine, store, make_settings()), engine


def inbound(text: str, conv: str = "c1", user: str = "u1") -> InboundMessage:
    return InboundMessage(content=text, user_id=user, conversation_id=conv)


class TestParseLocalTime:
    def test_naive_iso(self):
        assert parse_local_time("2026-10-24T09:00:00") == datetime(2026, 10, 24, 9, 0)

    def test_aware_iso_converts_to_local_naive(self):
        parsed = parse_local_time("2026-10-24T09:00:00+08:00")
        assert parsed.tzinfo is None

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_local_time("明天早上九点")


class TestChat:
    @pytest.mark.asyncio
    async def test_chat_answer_passthrough(self, tmp_path):
        brain, engine = make_brain(tmp_path, IntentResult(intent="chat", answer="你好呀"))

        reply = await brain.handle(inbound("你好"))

        assert reply is not None and reply.content == "你好呀"
        assert engine.calls[0]["user_text"] == "你好"

    @pytest.mark.asyncio
    async def test_empty_answer_gets_fallback(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="chat", answer="  "))

        reply = await brain.handle(inbound("你好"))

        assert reply is not None and "再说" in reply.content

    @pytest.mark.asyncio
    async def test_history_accumulates_and_is_passed_to_engine(self, tmp_path):
        brain, engine = make_brain(tmp_path, IntentResult(intent="chat", answer="ok"))

        await brain.handle(inbound("第一句"))
        await brain.handle(inbound("第二句"))

        history = engine.calls[1]["history"]
        assert history[-2:] == [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "ok"},
        ]

    @pytest.mark.asyncio
    async def test_blank_message_ignored(self, tmp_path):
        brain, engine = make_brain(tmp_path, IntentResult(intent="chat", answer="x"))

        assert await brain.handle(inbound("   ")) is None
        assert engine.calls == []


class TestSetAlarm:
    @pytest.mark.asyncio
    async def test_valid_alarm_is_scheduled_and_confirmed(self, tmp_path):
        future = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        brain, _ = make_brain(
            tmp_path,
            IntentResult(intent="set_alarm", alarm_at=future, alarm_message="开会"),
        )

        reply = await brain.handle(inbound("提醒我开会"))

        assert reply is not None
        assert "已设好闹钟 #1" in reply.content
        assert "开会" in reply.content
        assert len(brain.alarms.list("c1")) == 1

    @pytest.mark.asyncio
    async def test_past_time_rejected_without_side_effect(self, tmp_path):
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        brain, _ = make_brain(
            tmp_path,
            IntentResult(intent="set_alarm", alarm_at=past, alarm_message="开会"),
        )

        reply = await brain.handle(inbound("提醒我"))

        assert reply is not None and "已经过去" in reply.content
        assert brain.alarms.list() == []

    @pytest.mark.asyncio
    async def test_missing_time_gets_guidance(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="set_alarm", alarm_at=None))

        reply = await brain.handle(inbound("提醒我"))

        assert reply is not None and "没听清时间" in reply.content

    @pytest.mark.asyncio
    async def test_invalid_time_format_rejected(self, tmp_path):
        brain, _ = make_brain(
            tmp_path,
            IntentResult(intent="set_alarm", alarm_at="下周三下午", alarm_message="开会"),
        )

        reply = await brain.handle(inbound("提醒我"))

        assert reply is not None and "格式" in reply.content
        assert brain.alarms.list() == []

    @pytest.mark.asyncio
    async def test_too_far_future_rejected(self, tmp_path):
        far = (datetime.now() + timedelta(days=400)).isoformat(timespec="seconds")
        brain, _ = make_brain(
            tmp_path,
            IntentResult(intent="set_alarm", alarm_at=far, alarm_message="开会"),
        )

        reply = await brain.handle(inbound("提醒我"))

        assert reply is not None and "一年" in reply.content


class TestListAndCancel:
    @pytest.mark.asyncio
    async def test_list_empty(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="list_alarms"))

        reply = await brain.handle(inbound("查看闹钟"))

        assert reply is not None and "没有闹钟" in reply.content

    @pytest.mark.asyncio
    async def test_list_shows_existing(self, tmp_path):
        brain, engine = make_brain(tmp_path, IntentResult(intent="list_alarms"))
        brain.alarms.add(
            conversation_id="c1",
            user_id="u1",
            run_at=datetime(2026, 10, 24, 9, 0),
            message="开会",
        )

        reply = await brain.handle(inbound("查看闹钟"))

        assert reply is not None
        assert "#1" in reply.content and "开会" in reply.content
        # host 把闹钟投影提供给模型（取消决策用）
        assert engine.calls[0]["alarms"] == [
            AlarmBrief(id=1, run_at="2026-10-24T09:00", message="开会", repeat="none")
        ]

    @pytest.mark.asyncio
    async def test_cancel_valid_id(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="cancel_alarm", cancel_id=1))
        brain.alarms.add(
            conversation_id="c1",
            user_id="u1",
            run_at=datetime(2026, 10, 24, 9, 0),
            message="开会",
        )

        reply = await brain.handle(inbound("取消闹钟1"))

        assert reply is not None and "已取消" in reply.content
        assert brain.alarms.list() == []

    @pytest.mark.asyncio
    async def test_cancel_unknown_id_rejected(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="cancel_alarm", cancel_id=99))

        reply = await brain.handle(inbound("取消闹钟99"))

        assert reply is not None and "没有找到" in reply.content

    @pytest.mark.asyncio
    async def test_cannot_cancel_other_conversations_alarm(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="cancel_alarm", cancel_id=1))
        brain.alarms.add(
            conversation_id="someone-else",
            user_id="u2",
            run_at=datetime(2026, 10, 24, 9, 0),
            message="别人的",
        )

        reply = await brain.handle(inbound("取消闹钟1", conv="c1"))

        assert reply is not None and "没有找到" in reply.content
        assert len(brain.alarms.list("someone-else")) == 1  # 未被误删

    @pytest.mark.asyncio
    async def test_cancel_without_id_asks_to_list(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="cancel_alarm", cancel_id=None))

        reply = await brain.handle(inbound("取消闹钟"))

        assert reply is not None and "查看闹钟" in reply.content


class TestProgress:
    """流式适配器传入的 progress 回调：要按阶段给出「正在做什么」。"""

    @pytest.mark.asyncio
    async def test_progress_reports_understanding_then_step(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="chat", answer="你好"))
        seen: list[str] = []

        async def progress(text: str) -> None:
            seen.append(text)

        await brain.handle(inbound("你好"), progress)

        assert seen == ["🔍 正在理解你的意思…", "💬 正在组织回答…"]

    @pytest.mark.asyncio
    async def test_progress_step_differs_per_intent(self, tmp_path):
        future = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        brain, _ = make_brain(
            tmp_path,
            IntentResult(intent="set_alarm", alarm_at=future, alarm_message="开会"),
        )
        seen: list[str] = []

        async def progress(text: str) -> None:
            seen.append(text)

        await brain.handle(inbound("提醒我开会"), progress)

        assert seen[-1] == "⏰ 正在设置闹钟…"

    @pytest.mark.asyncio
    async def test_progress_failure_does_not_break_handling(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="chat", answer="ok"))

        async def broken_progress(text: str) -> None:
            raise RuntimeError("stream gone")

        reply = await brain.handle(inbound("你好"), broken_progress)

        assert reply is not None and reply.content == "ok"

    @pytest.mark.asyncio
    async def test_no_progress_callback_still_works(self, tmp_path):
        brain, _ = make_brain(tmp_path, IntentResult(intent="chat", answer="ok"))
        reply = await brain.handle(inbound("你好"))
        assert reply is not None and reply.content == "ok"
