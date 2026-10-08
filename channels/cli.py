"""命令行交互渠道。

CLIChannel 直接读写终端：从 stdin 读用户输入、把回复打印到 stdout。
它不依赖 Agent，只通过消息总线收发消息；tool_names 与 _clear_callback
由 main.py 在创建后注入。
"""

import asyncio
from typing import Callable

from bus.queue import InboundMessage, MessageBus, OutboundMessage
from channels.base import Channel


class CLIChannel(Channel):
    """基于终端的命令行渠道。"""

    def __init__(self, bus: MessageBus) -> None:
        super().__init__(name="cli", bus=bus)
        # 答复同步事件：send() 里 set()、start() 里 wait()，实现"发一条等一条"
        self._response_event = asyncio.Event()
        # 以下两项由 main.py 在创建后注入：工具名列表与清空历史回调
        self.tool_names: list[str] = []
        self._clear_callback: Callable[[], None] | None = None

    async def start(self) -> None:
        while True:
            # input() 会阻塞，放到线程池执行；await 期间事件循环可继续跑 Agent 等任务
            try:
                user_input = await asyncio.to_thread(input, "👤 你> ")
            except (KeyboardInterrupt, EOFError):
                # Ctrl+C 或 Ctrl+D 视为退出
                print("\n👋 再见！")
                break

            user_input = user_input.strip()
            if not user_input:
                # 空行直接忽略，不打扰 Agent
                continue

            # 内置命令：/exit 退出
            if user_input == "/exit":
                print("👋 再见！")
                break

            # 内置命令：/clear 清空会话历史（回调由 main.py 注入）
            if user_input == "/clear":
                if self._clear_callback is not None:
                    self._clear_callback()
                print("🧹 已清空历史。")
                continue

            # 内置命令：/tools 列出当前可用工具
            if user_input == "/tools":
                print("🔧 可用工具：")
                for name in self.tool_names:
                    print(f"  {name}")
                continue

            # 易错点：先 clear() 再发消息，保证本轮 wait() 等的是本轮回复。
            # 若在 wait() 前遗漏 clear，Event 仍是 set 状态会立即放行，导致读不到回复。
            self._response_event.clear()
            await self.bus.publish_inbound(
                InboundMessage(
                    channel="cli",
                    sender_id="local",     # CLI 只有本地一个用户
                    chat_id="direct",      # 会话固定为 direct
                    content=user_input,
                )
            )
            # 等 Agent 处理完并通过 send() 通知，再进入下一轮输入
            await self._response_event.wait()

    async def send(self, message: OutboundMessage) -> None:
        """把 Agent 的回复打印到终端，并唤醒 start() 的等待。"""
        print(f"🤖 {message.content}")
        # 唤醒 start() 里 await self._response_event.wait() 的地方，形成一问一答闭环
        self._response_event.set()
