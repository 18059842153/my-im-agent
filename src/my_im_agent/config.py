"""配置加载：优先读环境变量，其次读项目根目录的 .env。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SYSTEM_PROMPT = "你是一个接入即时通讯工具的 AI 助手，回答请简洁、直接、可执行。"


def _bootstrap_dotenv() -> None:
    """向上查找 .env，最多找 3 层，避免污染其他目录。"""
    here = Path(__file__).resolve()
    for parent in here.parents[:3]:
        candidate = parent / ".env"
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


@dataclass
class Settings:
    """运行配置。所有字段都能通过 IM_AGENT_* 环境变量覆盖。"""

    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_turns: int = 20
    request_timeout: float = 60.0
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        _bootstrap_dotenv()

        def get(name: str, default: str) -> str:
            return os.environ.get(f"IM_AGENT_{name}", default)

        settings = cls(
            llm_base_url=get("LLM_BASE_URL", cls.llm_base_url).rstrip("/"),
            llm_api_key=get("LLM_API_KEY", ""),
            llm_model=get("LLM_MODEL", cls.llm_model),
            system_prompt=get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            max_turns=int(get("MAX_TURNS", str(cls.max_turns))),
            request_timeout=float(get("REQUEST_TIMEOUT", str(cls.request_timeout))),
        )

        # 允许显式覆盖（CLI 参数、测试用）
        for key, value in overrides.items():
            if value is not None and hasattr(settings, key):
                setattr(settings, key, value)
        return settings

    @property
    def llm_enabled(self) -> bool:
        """是否配置了真实的大模型 Key。"""
        return bool(self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings(**overrides: object) -> Settings:
    return Settings.from_env(**overrides)
