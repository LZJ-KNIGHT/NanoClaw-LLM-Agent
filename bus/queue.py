"""消息总线：多渠道消息的收发队列。

MessageBus 用两条 asyncio.Queue 在渠道适配器与 Agent 之间解耦：inbound 承载用户
消息，outbound 承载回复。收发分离、互不阻塞，生产端 put() 在消费端跟不上时会挂起，
形成天然背压。
"""

import asyncio
from dataclasses import dataclass


# 用 dataclass 而非裸 dict：字段有名、带类型提示，IDE 可补全跳转，
# 且渠道与 Agent 之间传的是对象，边界清晰，不必到处 .get("xxx")。
# 入站多带 raw 供排查；出站只需知道"回哪个渠道、哪个会话、回什么"。
@dataclass
class InboundMessage:
    """一条来自某个渠道的入站消息。

    Attributes:
        channel: 来源渠道名称，如 ``feishu``、``qq``、``web``、``cli``。
        sender_id: 发送者标识。
        chat_id: 会话标识（群聊或私聊）。
        content: 消息正文。
        raw: 原始消息，调试用，无则 ``None``。
    """

    channel: str        # 渠道名：Gateway 靠它决定把会话归到哪个渠道
    sender_id: str      # 发送者 ID：与 channel 拼成 session_key（如 "feishu:ou_xxx"）
    chat_id: str        # 会话 ID：Agent 回消息时原样带回，渠道据此定位接收方
    content: str        # 纯文本正文，已完成各平台的解析与清洗
    raw: dict | None = None  # 平台原始报文（调试用），无则为 None


@dataclass
class OutboundMessage:
    """一条要发往某个渠道的出站消息。

    Attributes:
        channel: 目标渠道。
        chat_id: 目标会话。
        content: 回复正文。
        reply_to: 引用的消息 ID，可选。
    """

    channel: str    # 目标渠道名：Gateway._dispatch_outbound 据此选出对应 Channel
    chat_id: str    # 目标会话 ID：Channel.send() 用它定位发给谁
    content: str    # 要回复的正文（通常已是 Agent 处理后的文本）
    reply_to: str | None = None  # 可选：要引用的原消息 ID，None 表示不引用


class MessageBus:
    """基于 asyncio.Queue 的消息总线，入站/出站各一条队列。"""

    def __init__(self) -> None:
        # 入站队列：渠道生产、Gateway 消费
        self.inbound_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        # 出站队列：Gateway 生产、渠道消费
        self.outbound_queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """把一条入站消息放入入站队列。"""
        await self.inbound_queue.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """从入站队列取出一条消息（空则等待）。"""
        return await self.inbound_queue.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """把一条出站消息放入出站队列。"""
        await self.outbound_queue.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """从出站队列取出一条消息（空则等待）。"""
        return await self.outbound_queue.get()
