"""my-im-agent: a Python-based IM (instant messaging) AI agent."""

from __future__ import annotations

from my_im_agent.adapters.base import (
    IMAdapter,
    InboundMessage,
    OutboundMessage,
)
from my_im_agent.agent import Agent
from my_im_agent.config import Settings

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "IMAdapter",
    "InboundMessage",
    "OutboundMessage",
    "Settings",
    "__version__",
]
