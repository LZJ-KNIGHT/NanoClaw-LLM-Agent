"""多渠道路由网关。

把消息总线、渠道适配器与 Agent 串联成可运行的路由中枢：渠道只管收发，Agent 只管
思考，Gateway 负责在两者之间转接。两条常驻循环（_process_inbound 入站、
_dispatch_outbound 出站）各自独立、靠消息总线解耦；_agents 按 session_key 缓存
Agent，_channel_map 按渠道名索引渠道供出站 O(1) 定位。
"""

import asyncio
from typing import Callable

from agent.loop import AgentLoop
from bus.queue import InboundMessage, MessageBus, OutboundMessage
from channels.base import Channel


class Gateway:
    """渠道与 Agent 之间的路由中枢。

    Attributes:
        bus: 共享消息总线。
        channels: 所有已启用的渠道实例。
        agent_factory: Agent 工厂函数，入参 session_key，返回 Agent 实例。
        _agents: 按 session_key 缓存的 Agent，避免每条消息都重建。
        _channel_map: 按渠道名索引的渠道，供出站分发快速查找。
    """

    def __init__(
        self,
        bus: MessageBus,
        channels: list[Channel],
        agent_factory: Callable[[str], AgentLoop],
    ) -> None:
        """装配路由中枢所需的三方依赖，并建好缓存与索引。"""
        self.bus = bus
        self.channels = channels
        self.agent_factory = agent_factory
        # 按 session_key 缓存 Agent 实例，避免每次消息都重建
        self._agents: dict[str, AgentLoop] = {}
        # 按渠道名索引渠道，供出站分发时快速查找
        self._channel_map: dict[str, Channel] = {ch.name: ch for ch in channels}

    async def run(self) -> None:
        """并发启动所有渠道与两条消费循环；任一任务退出则取消其余并调 shutdown()。"""
        # 渠道 + 入站循环 + 出站循环一起并发跑
        tasks = [ch.start() for ch in self.channels] + [
            self._process_inbound(),
            self._dispatch_outbound(),
        ]
        try:
            # 等待所有任务（通常会阻塞直到某个渠道退出）
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            # 取消所有未完成的任务
            for task in tasks:
                if not task.done():
                    task.cancel()
            # 等待任务取消完成（忽略 CancelledError）
            await asyncio.gather(*tasks, return_exceptions=True)
            # 关闭所有渠道
            await self.shutdown()

    async def _process_inbound(self) -> None:
        """入站消费循环：取消息 → 按 session_key 懒加载 Agent → 发布回复。

        单条消息处理失败时把异常信息也当成一条回复发回，避免渠道侧一直等。
        """
        while True:
            msg = await self.bus.consume_inbound()
            # 会话键 = 渠道 + 发送者，保证同一用户在不同渠道各是一段独立会话
            session_key = f"{msg.channel}:{msg.sender_id}"

            agent = self._agents.get(session_key)
            if agent is None:
                agent = self.agent_factory(session_key)
                self._agents[session_key] = agent

            try:
                # 跑完 ReAct 循环拿到最终文本，再放进出站队列交给渠道发送
                reply = await agent.run(msg.content)
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=reply,
                    )
                )
            # 单条消息处理失败不能拖垮整条循环，转成提示消息回给用户
            except Exception as exc:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"⚠️ 处理消息时出错：{exc}",
                    )
                )

    async def _dispatch_outbound(self) -> None:
        """出站分发循环：取回复 → 按渠道交给对应 Channel.send。"""
        while True:
            msg = await self.bus.consume_outbound()
            # 渠道名 → Channel 的映射，O(1) 定位目标渠道
            channel = self._channel_map.get(msg.channel)
            if channel is None:
                # 未知渠道只告警不抛异常，保证分发循环持续存活
                print(f"⚠️ 未知渠道：{msg.channel}，无法发送回复")
                continue
            await channel.send(msg)

    async def shutdown(self) -> None:
        """停止所有渠道并清空 Agent 缓存。"""
        # 依次停掉每个渠道，释放各自资源（WebSocket、HTTP 连接等）
        for channel in self.channels:
            await channel.stop()
        # 清空会话级 Agent 缓存，下次启动从头创建
        self._agents.clear()
