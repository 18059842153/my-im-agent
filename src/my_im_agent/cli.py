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
        choices=["console", "wecom", "wecom-bot"],
        default="console",
        help="console=终端调试 / wecom=自建应用回调 / wecom-bot=智能机器人长连接（流式）",
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
    parser.add_argument(
        "--news-now",
        action="store_true",
        help="立即抓取并推送一次新闻日报（忽略今日已发状态）",
    )
    parser.add_argument(
        "--news-dry-run",
        action="store_true",
        help="配合 --news-now：只在终端打印，不发送、不写状态",
    )
    return parser


def build_engine(settings: Settings) -> IntentEngine:
    if settings.llm_enabled:
        return AgentlyIntentEngine(settings)
    return EchoIntentEngine()


def build_adapter(name: str, settings: Settings) -> IMAdapter:
    if name == "wecom":
        return WeComAdapter(settings)
    if name == "wecom-bot":
        from my_im_agent.wecom.bot_adapter import WeComBotAdapter

        return WeComBotAdapter(settings)
    return ConsoleAdapter()


# --- 新闻推送装配 ---


def build_summarizer(settings: Settings):
    from my_im_agent.news import AgentlyNewsSummarizer, EchoNewsSummarizer

    if settings.llm_enabled:
        return AgentlyNewsSummarizer(settings, chunk_size=settings.news_summary_chunk)
    return EchoNewsSummarizer()


def build_news_scheduler(settings: Settings, notify) -> tuple:
    from my_im_agent.news import NewsDigestService, NewsFetcher, NewsScheduler, NewsStateStore

    state = NewsStateStore(Path(settings.data_dir) / "news_state.json")
    fetcher = NewsFetcher(timeout=settings.news_fetch_timeout)
    service = NewsDigestService(
        settings=settings,
        state=state,
        fetcher=fetcher,
        summarizer=build_summarizer(settings),
        notify=notify,
    )
    return NewsScheduler(service, settings), fetcher


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

    print("\n=== 新闻推送 ===")
    await _check_news(settings, report)

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

    print("\n=== 智能机器人（长连接，流式） ===")
    report("WECOM_BOT_ID", bool(settings.wecom_bot_id), _mask(settings.wecom_bot_id, 6))
    report("WECOM_BOT_SECRET", bool(settings.wecom_bot_secret), _mask(settings.wecom_bot_secret))
    if settings.wecom_bot_enabled:
        print("     连接地址：wss://openws.work.weixin.qq.com（服务主动外连，无需公网 IP）")
    else:
        print("     [·] 未配置智能机器人；如需流式过程展示，见 docs/SETUP_WECOM.md 的「智能机器人」一节")

    # 接口凭据（CorpID / AgentId / Secret）与回调配置（Token / AESKey）分开检查：
    # 配到哪一步就能验到哪一步，不用等全配齐。
    api_ready = bool(settings.wecom_corp_id and settings.wecom_agent_id and settings.wecom_secret)
    callback_ready = bool(settings.wecom_token and settings.wecom_encoding_aes_key)

    if not api_ready:
        print("\nCorpID / AgentId / Secret 不完整，跳过连通性检查。参考 docs/SETUP_WECOM.md 第 1、2 步。")
        return False

    if callback_ready:
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
    else:
        print("\n[·] Token / EncodingAESKey 还没填，跳过加解密自检（手册第 6 步）")

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


async def _check_news(settings: Settings, report) -> None:
    """新闻推送体检：开关、时区、目标、源可达性、今日状态。"""
    from datetime import datetime, timedelta

    from my_im_agent.news import SECTIONS, NewsFetcher, NewsStateStore, parse_at

    at = parse_at(settings.news_at)
    offset = datetime.now().astimezone().utcoffset()
    hours = (offset.total_seconds() / 3600) if offset else 0.0
    report(
        "开关",
        settings.news_enabled,
        "已开启" if settings.news_enabled else "已关闭（IM_AGENT_NEWS_ENABLED=0）",
    )
    report("服务器时区", offset == timedelta(hours=8), f"UTC{hours:+.2f}".replace(".00", ":00"))
    report("推送时间", True, f"每天 {at:%H:%M}，补发窗口 {settings.news_catchup_minutes} 分钟")
    report("接收者", bool(settings.news_target_user), settings.news_target_user or "(空)")

    if not settings.news_enabled:
        return

    fetcher = NewsFetcher(timeout=settings.news_fetch_timeout)
    try:
        for section in SECTIONS:
            result = await fetcher.fetch(section)
            detail = f"{len(result.items)} 条"
            if result.errors:
                detail += "；" + " | ".join(result.errors)
            report(f"{section.label}源（{len(section.feeds)} 个）", bool(result.items), detail)
    finally:
        await fetcher.aclose()

    state = NewsStateStore(Path(settings.data_dir) / "news_state.json").load()
    today = datetime.now().date().isoformat()
    if state.last_date == today:
        report("今日是否已发", True, f"status={state.last_status} at={state.last_at}")
    else:
        report("今日是否已发", True, f"尚未发送（等 {at:%H:%M}）")


async def _news_now(settings: Settings, adapter_name: str, *, dry_run: bool) -> int:
    """立即抓取并推送一次，不等到点。"""
    from datetime import datetime

    from my_im_agent.news import NewsDigestService, NewsFetcher, NewsStateStore

    fetcher = NewsFetcher(timeout=settings.news_fetch_timeout)
    state = NewsStateStore(Path(settings.data_dir) / "news_state.json")
    adapter = None
    task: asyncio.Task | None = None

    if dry_run or adapter_name == "console":
        async def notify(user_id: str, conversation_id: str, text: str) -> None:
            print(text)
            print("-" * 60)
    else:
        adapter = build_adapter(adapter_name, settings)

        async def notify(user_id: str, conversation_id: str, text: str) -> None:
            await adapter.send(
                OutboundMessage(
                    content=text,
                    conversation_id=conversation_id,
                    raw={"user_id": user_id, "markdown": True},
                )
            )

        if hasattr(adapter, "ready"):
            task = asyncio.create_task(adapter.start(lambda *a, **k: None))
            try:
                await asyncio.wait_for(adapter.ready.wait(), timeout=30)
            except TimeoutError:
                print("等待长连接就绪超时，请检查 BotID/Secret", file=sys.stderr)
                if adapter is not None:
                    await adapter.close()
                await fetcher.aclose()
                return 2

    service = NewsDigestService(
        settings=settings,
        state=state,
        fetcher=fetcher,
        summarizer=build_summarizer(settings),
        notify=notify,
    )
    try:
        result = await service.deliver(datetime.now(), force=True, persist=not dry_run)
    finally:
        await fetcher.aclose()
        if task is not None:
            task.cancel()
        if adapter is not None:
            await adapter.close()

    if result.errors:
        print("问题：" + "；".join(result.errors), file=sys.stderr)
    print(f"结果：status={result.status} 消息数={len(result.messages)}")
    return 0 if result.ok else 1


async def _run(adapter_name: str, settings: Settings) -> int:
    engine = build_engine(settings)
    store = AlarmStore(Path(settings.data_dir) / "alarms.json")
    brain = Brain(engine, store, settings)
    adapter = build_adapter(adapter_name, settings)

    async def notify(user_id: str, conversation_id: str, text: str) -> None:
        """闹钟到点时的主动推送通道（纯文本）。"""
        await adapter.send(
            OutboundMessage(
                content=text,
                conversation_id=conversation_id,
                raw={"user_id": user_id},
            )
        )

    async def notify_md(user_id: str, conversation_id: str, text: str) -> None:
        """新闻推送通道（markdown，支持加粗与链接）。"""
        await adapter.send(
            OutboundMessage(
                content=text,
                conversation_id=conversation_id,
                raw={"user_id": user_id, "markdown": True},
            )
        )

    scheduler = AlarmScheduler(store, notify)

    news_scheduler = None
    news_fetcher = None
    if settings.news_enabled:
        news_scheduler, news_fetcher = build_news_scheduler(settings, notify_md)

    coros = [scheduler.start(), adapter.start(brain.handle)]
    if news_scheduler is not None:
        coros.insert(0, news_scheduler.start())

    try:
        await asyncio.gather(*coros)
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted.", file=sys.stderr)
    finally:
        scheduler.stop()
        if news_scheduler is not None:
            news_scheduler.stop()
        if news_fetcher is not None:
            await news_fetcher.aclose()
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

    if args.news_now:
        return asyncio.run(_news_now(settings, args.adapter, dry_run=args.news_dry_run))

    if args.adapter == "wecom":
        try:
            settings.require_wecom()
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    elif args.adapter == "wecom-bot":
        try:
            settings.require_wecom_bot()
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    return asyncio.run(_run(args.adapter, settings))


if __name__ == "__main__":
    raise SystemExit(main())
