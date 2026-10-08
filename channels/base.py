"""渠道适配器的抽象基类。

不同接入渠道（飞书、QQ、Web、CLI）各自实现 Channel 子类，负责 start() 启动
并持续接收消息、send() 把回复发回渠道；上层 Gateway 只依赖本接口。
"""

from abc import ABC, abstractmethod

from bus.queue import MessageBus, OutboundMessage


class Channel(ABC):
    """渠道适配器抽象基类。

    start()/send() 为必须实现的抽象方法，stop() 提供默认空实现；
    上层 Gateway 只认这个接口，不关心具体渠道实现。

    Attributes:
        name: 渠道名称，如 ``cli``、``feishu``、``qq``。
        bus: 共享的消息总线，用于收发消息。
    """

    def __init__(self, name: str, bus: MessageBus) -> None:
        # name 用于日志与 Gateway 路由；bus 由 main.py 组合根注入，各渠道共享同一实例
        self.name = name
        self.bus = bus

    @abstractmethod
    async def start(self) -> None:
        """启动渠道，开始监听并接收消息（通常是长驻循环或长连接）。"""
        raise NotImplementedError

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """把一条回复消息发送到该渠道（由 Gateway 出站循环调用）。"""
        raise NotImplementedError

    async def stop(self) -> None:
        """停止渠道，清理资源（默认无操作，有连接的渠道按需覆盖）。"""
        pass
