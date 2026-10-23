"""模型侧职责：意图识别 + 聊天回复生成。

按 Agently 的 owner 划分：
- 模型（ModelRequest）负责：从用户话语中识别意图、抽取闹钟时间与内容、生成聊天回复
- 宿主（host）负责：时间解析与合法性校验、闹钟 id 的成员校验、调度与发消息等副作用

意图契约用 Pydantic 表达，由 Agently 做结构化输出与校验重试。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from my_im_agent.config import Settings

IntentName = Literal["chat", "set_alarm", "list_alarms", "cancel_alarm"]
RepeatRule = Literal["none", "daily", "weekly", "workdays"]

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


class AlarmBrief(BaseModel):
    """提供给模型做取消决策的闹钟投影（host 下发的可选键集合）。"""

    id: int
    run_at: str = Field(description="ISO8601 本地时间")
    message: str
    repeat: str


class IntentResult(BaseModel):
    """一次用户消息的结构化判定结果。"""

    intent: IntentName = Field(description="用户意图，四选一")
    answer: str = Field(
        default="",
        description="intent=chat 时给用户的完整回复（简洁直接，可结合对话历史）；其他意图必须为空字符串",
    )
    alarm_at: str | None = Field(
        default=None,
        description=(
            "ISO8601 本地时间（如 2026-10-24T09:00:00），基于 input.now 推算；"
            "intent=set_alarm 时必填"
        ),
    )
    alarm_message: str | None = Field(
        default=None,
        description="闹钟提醒内容（保留用户原意的简短名词短语）；intent=set_alarm 时必填",
    )
    repeat: RepeatRule = Field(
        default="none",
        description=(
            "重复规则：none 一次性 / daily 每天 / weekly 每周 / workdays 每个工作日；"
            "intent=set_alarm 时使用"
        ),
    )
    cancel_id: int | None = Field(
        default=None,
        description="要取消的闹钟 id，必须取自 input.alarms 中已提供的 id；intent=cancel_alarm 时必填",
    )


class IntentEngine(Protocol):
    """意图路由抽象：让核心逻辑可以脱离真实模型测试。"""

    async def route(
        self,
        *,
        now: datetime,
        user_text: str,
        history: list[dict[str, str]],
        alarms: list[AlarmBrief],
    ) -> IntentResult: ...


class AgentlyIntentEngine:
    """用 Agently ModelRequest 承载意图识别与聊天回复。

    注意：插件 settings 必须按键逐个设置——对整个 namespace 赋 dict 会覆盖掉
    插件默认的 path_mapping / content_mapping。
    """

    def __init__(self, settings: Settings) -> None:
        from agently import Agently

        Agently.set_settings(
            "plugins.ModelRequester.OpenAICompatible.base_url", settings.llm_base_url
        )
        Agently.set_settings("plugins.ModelRequester.OpenAICompatible.model", settings.llm_model)
        Agently.set_settings(
            "plugins.ModelRequester.OpenAICompatible.auth",
            {"api_key": settings.llm_api_key},
        )
        Agently.set_settings(
            "plugins.ModelRequester.OpenAICompatible.request_options",
            {"max_tokens": settings.max_tokens},
        )
        self._system_prompt = settings.system_prompt

    async def route(
        self,
        *,
        now: datetime,
        user_text: str,
        history: list[dict[str, str]],
        alarms: list[AlarmBrief],
    ) -> IntentResult:
        from agently import Agently

        request = (
            Agently.create_request()
            .input(
                {
                    "now": f"{now:%Y-%m-%d %H:%M:%S} {_WEEKDAYS[now.weekday()]}",
                    "user_text": user_text,
                    "history": history,
                    "alarms": [a.model_dump() for a in alarms],
                }
            )
            .instruct(
                {
                    "role": self._system_prompt,
                    "rules": [
                        "从 user_text 判断用户意图，"
                        "只能从 chat / set_alarm / list_alarms / cancel_alarm 中选一个。",
                        "chat：普通问答或闲聊。answer 是给用户的完整回复，简洁、直接，可结合 history。",
                        "set_alarm：用户想在将来某个时间被提醒。根据 input.now 推算出绝对时间填入 alarm_at"
                        "（ISO8601，精确到分钟，如 2026-10-24T09:00:00）；alarm_message 提取提醒内容；"
                        "用户说「每天」「每周」「工作日」时对应设置 repeat，否则为 none；answer 留空。",
                        "list_alarms：用户想查看已有闹钟。answer 留空，由程序生成列表。",
                        "cancel_alarm：用户想取消闹钟。"
                        "cancel_id 必须取自 input.alarms 里真实存在的 id；answer 留空。",
                        "拿不准意图时按 chat 处理。",
                    ],
                }
            )
            .output(IntentResult, format="json")
        )
        return await request.async_get_data_object()


class EchoIntentEngine:
    """未配置 LLM Key 时的兜底实现：不联网，全部按 chat 回显。"""

    async def route(
        self,
        *,
        now: datetime,
        user_text: str,
        history: list[dict[str, str]],
        alarms: list[AlarmBrief],
    ) -> IntentResult:
        return IntentResult(intent="chat", answer=f"[echo] {user_text}")
