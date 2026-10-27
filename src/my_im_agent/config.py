"""配置加载：优先读环境变量，其次读项目根目录的 .env。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SYSTEM_PROMPT = "你是一个接入企业微信的 AI 助手，回答请简洁、直接、可执行。"


def _as_bool(raw: str, default: bool = False) -> bool:
    """环境变量布尔值解析。

    直接 bool("0") 会得到 True，所以必须走白名单。
    """
    value = (raw or "").strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


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

    # --- 企业微信智能机器人（长连接模式，走流式） ---
    wecom_bot_id: str = ""
    wecom_bot_secret: str = ""
    wecom_bot_name: str = ""

    # --- 数据 ---
    data_dir: str = "data"

    # --- 每日新闻推送 ---
    news_enabled: bool = True
    news_at: str = "09:30"
    news_target_user: str = "ChenKeCheng"
    news_catchup_minutes: int = 90
    news_top_n: int = 15
    news_max_bytes: int = 3800
    news_fetch_timeout: float = 15.0
    news_total_timeout: float = 180.0
    news_summary_chunk: int = 5

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
            wecom_bot_id=get_wecom("BOT_ID", ""),
            wecom_bot_secret=get_wecom("BOT_SECRET", ""),
            wecom_bot_name=get_wecom("BOT_NAME", ""),
            data_dir=get("DATA_DIR", cls.data_dir),
            news_enabled=_as_bool(get("NEWS_ENABLED", "1"), True),
            news_at=get("NEWS_AT", cls.news_at),
            news_target_user=get("NEWS_TARGET_USER", cls.news_target_user),
            news_catchup_minutes=int(get("NEWS_CATCHUP_MINUTES", str(cls.news_catchup_minutes))),
            news_top_n=int(get("NEWS_TOP_N", str(cls.news_top_n))),
            news_max_bytes=int(get("NEWS_MAX_BYTES", str(cls.news_max_bytes))),
            news_fetch_timeout=float(get("NEWS_FETCH_TIMEOUT", str(cls.news_fetch_timeout))),
            news_total_timeout=float(get("NEWS_TOTAL_TIMEOUT", str(cls.news_total_timeout))),
            news_summary_chunk=int(get("NEWS_SUMMARY_CHUNK", str(cls.news_summary_chunk))),
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

    @property
    def wecom_bot_enabled(self) -> bool:
        """智能机器人（长连接）所需配置是否齐全。"""
        return bool(self.wecom_bot_id and self.wecom_bot_secret)

    def require_wecom_bot(self) -> None:
        """智能机器人模式启动前的快速失败检查。"""
        if not self.wecom_bot_enabled:
            missing = [
                name
                for name, value in [
                    ("WECOM_BOT_ID", self.wecom_bot_id),
                    ("WECOM_BOT_SECRET", self.wecom_bot_secret),
                ]
                if not value
            ]
            raise RuntimeError(
                "企业微信智能机器人配置缺失："
                + ", ".join(missing)
                + "。后台：智能机器人 → 配置 → 开启「API 模式」→ 选「长连接」，"
                "拿 BotID 和长连接专用 Secret（注意不是回调模式的 Token/AESKey）。"
            )

    @property
    def news_configured(self) -> bool:
        return bool(self.news_enabled and self.news_target_user)

    def require_news(self) -> None:
        """新闻推送启动前的快速失败检查。"""
        if not self.news_target_user:
            raise RuntimeError(
                "新闻推送缺少接收者：请设置 IM_AGENT_NEWS_TARGET_USER（企业微信成员 UserID）。"
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
