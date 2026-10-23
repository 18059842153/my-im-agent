"""终端 REPL 适配器：没有真实 IM 平台时用来本地调试 Agent。"""

from __future__ import annotations

import asyncio

from my_im_agent.adapters.base import (
    Handler,
    IMAdapter,
    InboundMessage,
    OutboundMessage,
)

_USER_ID = "local-user"
_CONVERSATION_ID = "console"


class ConsoleAdapter(IMAdapter):
    """从 stdin 读一行当作一条入站消息，把回复打印到 stdout。"""

    name = "console"

    def __init__(
        self,
        prompt: str = "you> ",
        exit_words: tuple[str, ...] = ("exit", "quit"),
    ) -> None:
        self._prompt = prompt
        self._exit_words = exit_words

    async def start(self, on_message: Handler) -> None:
        loop = asyncio.get_running_loop()
        print("Console adapter started. Type 'exit' or 'quit' to stop.", flush=True)

        while True:
            line = await loop.run_in_executor(None, lambda: input(self._prompt))
            line = line.strip()

            if line.lower() in self._exit_words:
                print("Bye.", flush=True)
                return
            if not line:
                continue

            inbound = InboundMessage(
                content=line,
                user_id=_USER_ID,
                conversation_id=_CONVERSATION_ID,
            )
            reply = await on_message(inbound)
            if reply is not None:
                await self.send(reply)

    async def send(self, message: OutboundMessage) -> None:
        print(f"bot> {message.content}", flush=True)
