"""命令行入口：装配 Brain + 闹钟调度器 + 指定 IM 适配器。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from my_im_agent.adapters.base import IMAdapter, OutboundMessage
from my_im_agent.adapters.console import ConsoleAdapter
from my_im_agent.alarms import AlarmScheduler, AlarmStore
from my_im_agent.config import Settings
from my_im_agent.core import Brain
from my_im_agent.llm import AgentlyIntentEngine, EchoIntentEngine, IntentEngine
from my_im_agent.wecom.adapter import WeComAdapter
from my_im_agent.wecom.api import WeComAPI
from my_im_agent.wecom.crypto import WeComCrypto


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="my-im-agent",
        description="Run the IM AI agent.",
    )
    parser.add_argument(
        "--adapter",
        choices=["console", "wecom"],
        default="console",
        help="which IM platform adapter to use (default: console)",
    )
    parser.add_argument("--model", help="override IM_AGENT_LLM_MODEL")
    parser.add_argument("--base-url", help="override IM_AGENT_LLM_BASE_URL")
    parser.add_argument("--system-prompt", help="override IM_AGENT_SYSTEM_PROMPT")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--check",
        action="store_true",
        help="体检模式：校验配置、连通企业微信接口、可选发一条测试消息",
    )
    parser.add_argument("--send-to", help="配合 --check：给该成员 UserID 发一条测试消息")
    parser.add_argument("--probe-llm", action="store_true", help="配合 --check：真实调用一次模型")
    return parser


def build_engine(settings: Settings) -> IntentEngine:
    if settings.llm_enabled:
        return AgentlyIntentEngine(settings)
    return EchoIntentEngine()


def build_adapter(name: str, settings: Settings) -> IMAdapter:
    if name == "wecom":
        return WeComAdapter(settings)
    return ConsoleAdapter()


# --- 体检 ---

_ERRCODE_HINT = {
    40001: "Secret 不对，或不是「自建应用」的 Secret（注意不是通讯录 Secret）",
    40013: "CorpID 不对，去「我的企业 → 企业信息」核对",
    40014: "access_token 无效，通常会自动重试一次；持续出现请检查 Secret",
    60020: "出口 IP 不在「企业可信 IP」白名单里，去应用后台把本机公网 IP 加上",
    82001: "指定的成员 UserID 不在本应用的可见范围内，去应用后台设置可见范围",
    301002: "无权限访问该成员，检查应用可见范围是否包含此人",
}


def _mask(value: str, keep: int = 4) -> str:
    if not value:
        return "(空)"
    if len(value) <= keep * 2:
        return f"{value[:keep]}***"
    return f"{value[:keep]}...{value[-keep:]}"


async def _check(settings: Settings, send_to: str | None, probe_llm: bool) -> bool:
    """逐项体检，返回是否全部通过。"""
    ok_overall = True

    def report(label: str, ok: bool, detail: str = "") -> None:
        nonlocal ok_overall
        mark = "✓" if ok else "✗"
        print(f"[{mark}] {label}" + (f"  {detail}" if detail else ""))
        if not ok:
            ok_overall = False

    print("=== 配置 ===")
    report("LLM Base URL", bool(settings.llm_base_url), settings.llm_base_url)
    report("LLM Model", bool(settings.llm_model), settings.llm_model)
    report("LLM API Key", settings.llm_enabled, _mask(settings.llm_api_key))
    report(
        "数据目录",
        bool(settings.data_dir),
        settings.data_dir + ("（未配置 Key 时走 Echo，不会联网）" if not settings.llm_enabled else ""),
    )

    print("\n=== 企业微信配置 ===")
    report("WECOM_CORP_ID", bool(settings.wecom_corp_id), settings.wecom_corp_id or "(空)")
    report("WECOM_AGENT_ID", bool(settings.wecom_agent_id), str(settings.wecom_agent_id or "(空)"))
    report("WECOM_SECRET", bool(settings.wecom_secret), _mask(settings.wecom_secret))
    report("WECOM_TOKEN", bool(settings.wecom_token), _mask(settings.wecom_token))
    report(
        "WECOM_ENCODING_AES_KEY",
        bool(settings.wecom_encoding_aes_key),
        _mask(settings.wecom_encoding_aes_key),
    )
    if settings.wecom_encoding_aes_key:
        length = len(settings.wecom_encoding_aes_key)
        print(f"     长度={length}（应为 43）" + ("" if length == 43 else "  ⚠ 长度不对"))
    print(f"     回调地址应为：http(s)://<你的公网域名>{settings.wecom_callback_path}")
    print(f"     本地监听：{settings.wecom_callback_host}:{settings.wecom_callback_port}")

    if not settings.wecom_enabled:
        print("\n企业微信配置不完整，跳过连通性检查。参考 docs/SETUP_WECOM.md 补齐。")
        return False

    print("\n=== 加解密自检 ===")
    try:
        crypto = WeComCrypto(
            settings.wecom_token,
            settings.wecom_encoding_aes_key,
            settings.wecom_corp_id,
        )
        echo = "verify-123456"
        got = crypto.decrypt(crypto.encrypt(echo))
        report("EncodingAESKey 可用（加解密往返）", got == echo)
    except Exception as exc:  # noqa: BLE001 - 体检要把原因打出来
        report("EncodingAESKey 可用（加解密往返）", False, str(exc))

    print("\n=== 企业微信接口连通性 ===")
    api = WeComAPI(settings.wecom_corp_id, settings.wecom_secret, settings.wecom_agent_id)
    try:
        token = await api.access_token()
        report("gettoken 成功", True, _mask(token, 6))
    except Exception as exc:  # noqa: BLE001
        errcode = getattr(exc, "errcode", None)
        hint = _ERRCODE_HINT.get(errcode, "")
        detail = str(exc)
        if hint:
            detail += f"\n     → {hint}"
        report("gettoken 成功", False, detail)
        await api.aclose()
        return False

    if send_to:
        try:
            await api.send_text(send_to, "my-im-agent 自检消息：配置正常 ✅")
            report(f"给 {send_to} 发测试消息", True, "去企业微信看看有没有收到")
        except Exception as exc:  # noqa: BLE001
            hint = _ERRCODE_HINT.get(getattr(exc, "errcode", None), "")
            report(f"给 {send_to} 发测试消息", False, str(exc) + (f"\n     → {hint}" if hint else ""))
    else:
        print("[·] 未指定 --send-to，跳过发消息测试")

    await api.aclose()

    if probe_llm and settings.llm_enabled:
        print("\n=== 模型连通性 ===")
        try:
            from datetime import datetime

            result = await AgentlyIntentEngine(settings).route(
                now=datetime.now(), user_text="只回复两个字：可用", history=[], alarms=[]
            )
            report("模型可用", True, f"intent={result.intent} answer={result.answer[:30]!r}")
        except Exception as exc:  # noqa: BLE001
            report("模型可用", False, str(exc))
    elif probe_llm:
        report("模型可用", False, "未配置 IM_AGENT_LLM_API_KEY")

    print("\n" + ("全部通过 ✅" if ok_overall else "存在失败项 ❌，按上面提示修"))
    return ok_overall


async def _run(adapter_name: str, settings: Settings) -> int:
    engine = build_engine(settings)
    store = AlarmStore(Path(settings.data_dir) / "alarms.json")
    brain = Brain(engine, store, settings)
    adapter = build_adapter(adapter_name, settings)

    async def notify(user_id: str, conversation_id: str, text: str) -> None:
        """闹钟到点时的主动推送通道。"""
        await adapter.send(
            OutboundMessage(
                content=text,
                conversation_id=conversation_id,
                raw={"user_id": user_id},
            )
        )

    scheduler = AlarmScheduler(store, notify)

    try:
        await asyncio.gather(scheduler.start(), adapter.start(brain.handle))
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted.", file=sys.stderr)
    finally:
        scheduler.stop()
        await adapter.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env(
        llm_model=args.model,
        llm_base_url=args.base_url,
        system_prompt=args.system_prompt,
    )
    if not settings.llm_enabled:
        print(
            "Warning: IM_AGENT_LLM_API_KEY not set, falling back to EchoIntentEngine.",
            file=sys.stderr,
        )
    if args.check:
        return 0 if asyncio.run(_check(settings, args.send_to, args.probe_llm)) else 1

    if args.adapter == "wecom":
        try:
            settings.require_wecom()
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    return asyncio.run(_run(args.adapter, settings))


if __name__ == "__main__":
    raise SystemExit(main())
