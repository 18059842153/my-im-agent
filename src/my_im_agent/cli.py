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
    return parser


def build_engine(settings: Settings) -> IntentEngine:
    if settings.llm_enabled:
        return AgentlyIntentEngine(settings)
    return EchoIntentEngine()


def build_adapter(name: str, settings: Settings) -> IMAdapter:
    if name == "wecom":
        return WeComAdapter(settings)
    return ConsoleAdapter()


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
    if args.adapter == "wecom":
        try:
            settings.require_wecom()
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    return asyncio.run(_run(args.adapter, settings))


if __name__ == "__main__":
    raise SystemExit(main())
