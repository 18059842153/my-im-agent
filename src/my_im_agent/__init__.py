"""my-im-agent: 接企业微信的 Python IM AI Agent（含定时闹钟能力）。"""

from __future__ import annotations

from my_im_agent.adapters.base import (
    IMAdapter,
    InboundMessage,
    OutboundMessage,
)
from my_im_agent.alarms import Alarm, AlarmScheduler, AlarmStore
from my_im_agent.config import Settings
from my_im_agent.core import Brain

__version__ = "0.2.0"

__all__ = [
    "Alarm",
    "AlarmScheduler",
    "AlarmStore",
    "Brain",
    "IMAdapter",
    "InboundMessage",
    "OutboundMessage",
    "Settings",
    "__version__",
]
