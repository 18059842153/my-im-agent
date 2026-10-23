"""命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from my_im_agent.adapters.base import IMAdapter, OutboundMessage
from my_im_agent.adapters.console import ConsoleAdapter
from my_im_agent.agent import Agent
from my_im_agent.config import Settings

_ADAPTERS: dict[str, type[IMAdapter]] = {
    "console": ConsoleAdapter,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="my-im-agent",
        description="Run the IM AI agent.",
    )
    parser.add_argument(
        "--adapter",
        choices=sorted(_ADAPTERS),
        default="console",
        help="which IM platform adapter to use (default: console)",
    )
    parser.add_argument("--model", help="override IM_AGENT_LLM_MODEL")
    parser.add_argument("--base-url", help="override IM_AGENT_LLM_BASE_URL")
    parser.add_argument("--system-prompt", help="override IM_AGENT_SYSTEM_PROMPT")
    return parser


async def _run(adapter_name: str, settings: Settings) -> int:
    agent = Agent(settings)
    adapter = _ADAPTERS[adapter_name]()

    async def on_message(message):  # noqa: ANN001 - 类型由 Handler 协议保证
        reply: OutboundMessage | None = await agent.handle(message)
        if reply is not None:
            await adapter.send(reply)
        return None

    try:
        await adapter.start(on_message)
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted.", file=sys.stderr)
    finally:
        await adapter.close()
        await agent.aclose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env(
        llm_model=args.model,
        llm_base_url=args.base_url,
        system_prompt=args.system_prompt,
    )
    if not settings.llm_enabled:
        print(
            "Warning: IM_AGENT_LLM_API_KEY not set, falling back to EchoProvider.",
            file=sys.stderr,
        )
    return asyncio.run(_run(args.adapter, settings))


if __name__ == "__main__":
    raise SystemExit(main())
