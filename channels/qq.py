"""QQ 渠道：通过 QQ 开放平台 Bot（botpy SDK，WebSocket 长连接）收发消息。

start() 里对启动失败做了重试：botpy 登录阶段需解析 DNS，偶发失败会导致启动直接失败。
"""

import asyncio
import re
from typing import Any

import botpy
from botpy.message import GroupMessage, C2CMessage

from bus.queue import InboundMessage, MessageBus, OutboundMessage
from .base import Channel

# 启动失败后的重试间隔（秒）
RETRY_DELAY = 5


class QQBotClient(botpy.Client):
    """QQ Bot 客户端，处理消息接收事件。"""

    def __init__(self, channel: "QQChannel", *args, **kwargs):
        """初始化 QQ Bot 客户端，反向持有 channel 以便回调里发布消息到总线。"""
        super().__init__(*args, **kwargs)
        self._channel = channel

    async def on_group_at_message_create(self, message: GroupMessage):
        """处理群内 @消息事件。"""
        try:
            sender_id = message.author.member_openid  # 群成员在群内的 openid
            chat_id = message.group_openid            # 群会话 ID，回复时原样带回
            content = message.content.strip()

            # 去掉 @Bot 的前缀（形如 <@!123456>）
            content = re.sub(r"<@!\d+>", "", content).strip()

            if not content:
                return

            print(f"[QQChannel] 群消息: sender={sender_id} chat={chat_id} content={content}")

            # raw 保留原始字段，便于排查
            inbound = InboundMessage(
                channel="qq",
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                raw={
                    "message_id": message.id,
                    "group_openid": chat_id,
                    "msg_type": "group",
                    "timestamp": message.timestamp,
                },
            )

            # qq 回调运行在 botpy 的事件循环中，可直接 await
            await self._channel.bus.publish_inbound(inbound)

        except Exception as e:
            import traceback
            print(f"[QQChannel] 群消息处理异常: {e}")
            traceback.print_exc()

    async def on_c2c_message_create(self, message: C2CMessage):
        """处理 C2C 单聊消息事件。"""
        try:
            sender_id = message.author.user_openid
            chat_id = sender_id  # 单聊的 chat_id 就是对方的 openid（见 send 里的推断逻辑）
            content = message.content.strip()

            if not content:
                return

            print(f"[QQChannel] C2C消息: sender={sender_id} content={content}")

            inbound = InboundMessage(
                channel="qq",
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                raw={
                    "message_id": message.id,
                    "user_openid": sender_id,
                    "msg_type": "c2c",
                    "timestamp": message.timestamp,
                },
            )

            await self._channel.bus.publish_inbound(inbound)

        except Exception as e:
            import traceback
            print(f"[QQChannel] C2C消息处理异常: {e}")
            traceback.print_exc()


class QQChannel(Channel):
    """QQ 渠道，通过 QQ Bot API 接收和发送消息。"""

    def __init__(self, bus: MessageBus, app_id: str, app_secret: str):
        """初始化 QQ 渠道，app_id/app_secret 为 QQ Bot 应用凭证。"""
        super().__init__(name="qq", bus=bus)
        self.app_id = app_id
        self.app_secret = app_secret
        self._bot: QQBotClient | None = None   # 当前 botpy 客户端实例
        self._running = False                  # 运行标志：stop() 置 False 以终止重试循环

    async def start(self) -> None:
        """启动 QQ Bot 客户端，监听消息事件。

        botpy 登录阶段需要解析 bots.qq.com 的 DNS，偶发 DNS 解析失败会导致启动
        直接失败，故加一层重试：失败后等待片刻重启，直到成功或被 stop()/cancel 打断。
        """
        self._running = True
        print("[QQChannel] 正在启动 QQ Bot...")

        retry = 0
        while self._running:
            # 只订阅需要的事件，避免收到无关推送
            intents = botpy.Intents(
                public_messages=True,  # 群/C2C 消息（含 on_group_at_message_create 和 on_c2c_message_create）
            )

            # 每次重试都新建客户端，避免复用已损坏的连接状态
            self._bot = QQBotClient(channel=self, intents=intents)

            try:
                # async with 负责连接的生命周期（进入时初始化、退出时关闭）
                async with self._bot:
                    await self._bot.start(appid=self.app_id, secret=self.app_secret)
                # 正常退出（stop 触发关闭），不再重试
                break
            except Exception as e:
                # 常见为 DNS 解析失败：清空实例、计数、等待后重试
                self._bot = None
                retry += 1
                print(f"[QQChannel] Bot 启动失败（第 {retry} 次），{RETRY_DELAY}s 后重试: {e}")
                await asyncio.sleep(RETRY_DELAY)

    async def send(self, message: OutboundMessage) -> None:
        """发送消息到 QQ（由 Gateway 出站循环调用）。"""
        if self._bot is None or not hasattr(self._bot, 'api'):
            print("[QQChannel] Bot 未初始化，无法发送消息")
            return

        try:
            # 已知局限：OutboundMessage 里没有 raw，无法直接知道这条回复该走"群"还是"单聊"，
            # 目前只能"先试 C2C，失败再试群"——每次群回复都会先发一次注定失败的 C2C 请求。
            # 改进方向：在 InboundMessage→OutboundMessage 链路里透传 msg_type。

            # 先尝试按 C2C（单聊）发送
            try:
                await self._bot.api.post_c2c_message(
                    openid=message.chat_id,
                    msg_type=0,  # 文本消息
                    msg_id="",
                    content=message.content,
                )
                print(f"[QQChannel] C2C消息已发送到 {message.chat_id}")
                return
            except Exception:
                # 单聊发送失败很正常（那它多半是群消息），静默忽略继续往下试群
                pass

            # 再尝试按群消息发送
            await self._bot.api.post_group_message(
                group_openid=message.chat_id,
                msg_type=0,  # 文本消息
                msg_id="",
                content=message.content,
            )
            print(f"[QQChannel] 群消息已发送到 {message.chat_id}")

        except Exception as e:
            import traceback
            print(f"[QQChannel] 发送消息异常: {e}")
            traceback.print_exc()

    async def stop(self) -> None:
        """停止 QQ Bot 客户端连接。"""
        self._running = False   # 置 False 让 start() 的重试循环退出
        if self._bot is not None:
            self._bot = None    # 断引用，交由 GC/async with 清理连接
            print("[QQChannel] Bot 已停止")
