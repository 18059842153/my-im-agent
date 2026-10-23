"""核心编排：一条消息进来，经过意图路由，产生回复或副作用。

owner 划分：
- 模型：意图判定、闹钟时间/内容抽取、聊天回复文本（见 llm.py）
- host：时间解析与合法性校验、闹钟 id 成员校验、入库/取消/列举、历史维护
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta

from my_im_agent.adapters.base import InboundMessage, MessageType, OutboundMessage
from my_im_agent.alarms import MAX_FUTURE, REPEAT_RULES, Alarm, AlarmStore
from my_im_agent.config import Settings
from my_im_agent.llm import AlarmBrief, IntentEngine, IntentResult

_REPEAT_LABELS = {
    "none": "一次性",
    "daily": "每天",
    "weekly": "每周",
    "workdays": "工作日",
}

# 模型输出与真实触发之间的允许误差（模型可能输出「下一分钟」这种临近时间）
_PAST_TOLERANCE = timedelta(seconds=30)


def parse_local_time(raw: str) -> datetime:
    """解析模型输出的 ISO8601 本地时间；带时区的转换为本地 naive。"""
    parsed = datetime.fromisoformat(raw.strip())
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


class Brain:
    """会话历史 + 意图路由 + 闹钟副作用的统一入口。"""

    def __init__(
        self,
        engine: IntentEngine,
        alarms: AlarmStore,
        settings: Settings,
    ) -> None:
        self.engine = engine
        self.alarms = alarms
        self.settings = settings
        self._histories: dict[str, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=settings.max_turns)
        )

    # --- 对外 ---

    async def handle(self, message: InboundMessage) -> OutboundMessage | None:
        if not message.is_text or not message.content.strip():
            return None

        conv = message.conversation_id
        history = self._histories[conv]
        briefs = [
            AlarmBrief(
                id=a.id,
                run_at=a.run_at.isoformat(timespec="minutes"),
                message=a.message,
                repeat=a.repeat,
            )
            for a in self.alarms.list(conv)
        ]

        result = await self.engine.route(
            now=datetime.now(),
            user_text=message.content.strip(),
            history=list(history),
            alarms=briefs,
        )
        reply_text = self._apply(message, result)

        history.append({"role": "user", "content": message.content.strip()})
        history.append({"role": "assistant", "content": reply_text})

        return OutboundMessage(
            content=reply_text,
            conversation_id=conv,
            reply_to=message.message_id,
            message_type=MessageType.TEXT,
            raw={"user_id": message.user_id},
        )

    def reset(self, conversation_id: str) -> None:
        self._histories.pop(conversation_id, None)

    # --- host 侧效果（确定性，全部可测） ---

    def _apply(self, message: InboundMessage, result: IntentResult) -> str:
        if result.intent == "chat":
            return result.answer.strip() or "（我这次没生成出回复，请再说一遍）"
        if result.intent == "list_alarms":
            return self._format_alarms(message.conversation_id)
        if result.intent == "cancel_alarm":
            return self._cancel_alarm(message.conversation_id, result.cancel_id)
        if result.intent == "set_alarm":
            return self._set_alarm(message, result)
        return "我暂时没理解这句话，换个说法试试？"

    def _set_alarm(self, message: InboundMessage, result: IntentResult) -> str:
        if not result.alarm_at:
            return "我没听清时间。比如：「明天早上9点提醒我开会」「10分钟后提醒我喝水」。"
        try:
            run_at = parse_local_time(result.alarm_at)
        except ValueError:
            return "时间格式有问题，换个说法试试，比如「明天早上9点提醒我开会」。"

        now = datetime.now()
        if run_at < now - _PAST_TOLERANCE:
            return f"{run_at:%Y-%m-%d %H:%M} 已经过去了，给个将来的时间吧。"
        if run_at > now + MAX_FUTURE:
            return "最远只能设置一年以内的闹钟。"

        if result.repeat not in REPEAT_RULES:
            repeat = "none"
        else:
            repeat = result.repeat
        alarm_message = (result.alarm_message or "").strip() or "提醒"

        alarm: Alarm = self.alarms.add(
            conversation_id=message.conversation_id,
            user_id=message.user_id,
            run_at=run_at,
            message=alarm_message,
            repeat=repeat,
        )
        return (
            f"⏰ 已设好闹钟 #{alarm.id}："
            f"{alarm.run_at:%Y-%m-%d %H:%M}（{_REPEAT_LABELS[repeat]}）{alarm.message}\n"
            "回复「查看闹钟」可管理。"
        )

    def _cancel_alarm(self, conversation_id: str, cancel_id: int | None) -> str:
        if cancel_id is None:
            return "你想取消哪一个？先对我说「查看闹钟」看看编号。"
        alarm = self.alarms.get(conversation_id, cancel_id)
        if alarm is None:
            return f"没有找到 #{cancel_id} 这个闹钟。回复「查看闹钟」确认编号。"
        self.alarms.remove(conversation_id, cancel_id)
        return f"已取消闹钟 #{cancel_id}（{alarm.run_at:%Y-%m-%d %H:%M} {alarm.message}）。"

    def _format_alarms(self, conversation_id: str) -> str:
        items = self.alarms.list(conversation_id)
        if not items:
            return "当前没有闹钟。比如对我说「明天早上9点提醒我开会」。"
        lines = ["当前闹钟："]
        for a in items:
            lines.append(
                f"#{a.id}  {a.run_at:%Y-%m-%d %H:%M}  {_REPEAT_LABELS[a.repeat]}  {a.message}"
            )
        lines.append("取消请说「取消闹钟 N」。")
        return "\n".join(lines)
