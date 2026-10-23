"""配置加载：优先读环境变量，其次读项目根目录的 .env。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SYSTEM_PROMPT = "你是一个接入企业微信的 AI 助手，回答请简洁、直接、可执行。"


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
    """运行配置。所有字段都能通过环境变量覆盖。

    - LLM 相关用 IM_AGENT_* 前缀
    - 企业微信相关用 WECOM_* 前缀
    """

    # --- LLM ---
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_turns: int = 20
    max_tokens: int = 4000
    request_timeout: float = 60.0

    # --- 企业微信自建应用 ---
    wecom_corp_id: str = ""
    wecom_agent_id: int = 0
    wecom_secret: str = ""
    wecom_token: str = ""
    wecom_encoding_aes_key: str = ""
    wecom_callback_host: str = "0.0.0.0"
    wecom_callback_port: int = 8666
    wecom_callback_path: str = "/wecom/callback"

    # --- 数据 ---
    data_dir: str = "data"

    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        _bootstrap_dotenv()

        def get(name: str, default: str) -> str:
            return os.environ.get(f"IM_AGENT_{name}", default)

        def get_wecom(name: str, default: str) -> str:
            return os.environ.get(f"WECOM_{name}", default)

        settings = cls(
            llm_base_url=get("LLM_BASE_URL", cls.llm_base_url).rstrip("/"),
            llm_api_key=get("LLM_API_KEY", ""),
            llm_model=get("LLM_MODEL", cls.llm_model),
            system_prompt=get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            max_turns=int(get("MAX_TURNS", str(cls.max_turns))),
            max_tokens=int(get("MAX_TOKENS", str(cls.max_tokens))),
            request_timeout=float(get("REQUEST_TIMEOUT", str(cls.request_timeout))),
            wecom_corp_id=get_wecom("CORP_ID", ""),
            wecom_agent_id=int(get_wecom("AGENT_ID", "0") or "0"),
            wecom_secret=get_wecom("SECRET", ""),
            wecom_token=get_wecom("TOKEN", ""),
            wecom_encoding_aes_key=get_wecom("ENCODING_AES_KEY", ""),
            wecom_callback_host=get_wecom("CALLBACK_HOST", cls.wecom_callback_host),
            wecom_callback_port=int(get_wecom("CALLBACK_PORT", str(cls.wecom_callback_port))),
            wecom_callback_path=get_wecom("CALLBACK_PATH", cls.wecom_callback_path),
            data_dir=get("DATA_DIR", cls.data_dir),
        )

        for key, value in overrides.items():
            if value is not None and hasattr(settings, key):
                setattr(settings, key, value)
        return settings

    @property
    def llm_enabled(self) -> bool:
        """是否配置了真实的大模型 Key。"""
        return bool(self.llm_api_key)

    @property
    def wecom_enabled(self) -> bool:
        """企业微信自建应用所需配置是否齐全。"""
        return all(
            [
                self.wecom_corp_id,
                self.wecom_agent_id,
                self.wecom_secret,
                self.wecom_token,
                self.wecom_encoding_aes_key,
            ]
        )

    def require_wecom(self) -> None:
        """企业微信模式启动前的快速失败检查。"""
        if not self.wecom_enabled:
            missing = [
                name
                for name, value in [
                    ("WECOM_CORP_ID", self.wecom_corp_id),
                    ("WECOM_AGENT_ID", self.wecom_agent_id),
                    ("WECOM_SECRET", self.wecom_secret),
                    ("WECOM_TOKEN", self.wecom_token),
                    ("WECOM_ENCODING_AES_KEY", self.wecom_encoding_aes_key),
                ]
                if not value
            ]
            raise RuntimeError(
                "企业微信配置缺失："
                + ", ".join(missing)
                + "。请在 .env 或环境变量中补齐（参考 .env.example）。"
            )


@lru_cache(maxsize=1)
def get_settings(**overrides: object) -> Settings:
    return Settings.from_env(**overrides)
