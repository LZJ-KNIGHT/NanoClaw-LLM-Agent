"""飞书渠道：WebSocket 长连接接收消息，HTTP API 发送回复。

坑：飞书 SDK 的 WebSocket 跑在独立线程，消息回调 _on_message 不在主事件循环中，
故 start() 时保存主循环引用，回调中用 asyncio.run_coroutine_threadsafe 把协程
跨线程投回主循环才能 await；直接在子线程里 await 会报 "no running event loop"。
"""

import asyncio
import json
import re
from typing import Any

import lark_oapi as lark
import lark_oapi.api.im.v1 as im_v1

from bus.queue import InboundMessage, MessageBus, OutboundMessage
from .base import Channel


class FeishuChannel(Channel):
    """飞书渠道，通过 WebSocket 接收消息并通过 API 发送回复。"""

    def __init__(self, bus: MessageBus, app_id: str, app_secret: str):
        """初始化飞书渠道，app_id/app_secret 为飞书应用凭证。"""
        super().__init__(name="feishu", bus=bus)
        self.app_id = app_id
        self.app_secret = app_secret
        self._client: Any = None            # WebSocket 长连接客户端（收消息）
        self._lark_api_client: Any = None  # 用于发送消息的 API 客户端（发消息）
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None  # 保存主事件循环引用（跨线程投递用）

    async def start(self) -> None:
        """启动飞书 WebSocket 客户端，监听消息事件。"""
        # 保存当前事件循环引用，供 WebSocket 子线程回调使用
        self._loop = asyncio.get_running_loop()

        # 发送消息的 API 客户端：发消息走 HTTP，与收消息的 WS 分离
        self._lark_api_client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        # 事件处理器使用 P2 版本协议；register_p2_im_message_receive_v1 绑定"收到消息"回调
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_message) \
            .build()

        self._client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        self._running = True
        print("[FeishuChannel] 正在连接飞书 WebSocket...")

        # self._client.start 内部会起自己的线程跑 WS 并阻塞当前调用，故丢到线程池
        await asyncio.to_thread(self._client.start)

    def _on_message(self, data: Any) -> None:
        """处理飞书消息接收事件（P2 协议）。

        运行在 SDK 的 WebSocket 子线程中，是同步函数、不能 await，
        异步操作需通过 run_coroutine_threadsafe 投递回主事件循环。
        """
        try:
            event = data.event
            sender_id = event.sender.sender_id.open_id
            chat_id = event.message.chat_id
            message_type = event.message.message_type
            sender_type = event.sender.sender_type

            print(f"[FeishuChannel] 收到消息 sender={sender_id} type={message_type} sender_type={sender_type}")

            # 忽略 Bot 自己发送的消息
            if sender_type == "app":
                return

            # 只处理文本消息
            if message_type != "text":
                return

            # 飞书消息内容是 JSON 字符串
            content = json.loads(event.message.content)
            text = content.get("text", "")

            # 去掉 @Bot 的前缀（飞书群消息中会有 @_user_xxx 前缀）
            text = re.sub(r'@_all|@\S+', "", text).strip()

            if not text:
                return

            print(f"[FeishuChannel] 解析文本: {text}")

            message = InboundMessage(
                channel="feishu",
                sender_id=sender_id,
                chat_id=chat_id,
                content=text,
                raw={
                    "sender_id": sender_id,
                    "chat_id": chat_id,
                    "message_type": message_type,
                    "content": content,
                },
            )

            # 当前在 WS 子线程：用 run_coroutine_threadsafe 把协程投回主循环执行。
            # 参数1=待执行的协程对象；参数2=目标主循环 self._loop。
            # 返回 concurrent.futures.Future（非 asyncio.Future），这里不需要结果故忽略。
            if self._loop is not None and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.bus.publish_inbound(message),
                    self._loop,
                )
            else:
                print("[FeishuChannel] 警告: 主事件循环不可用，消息丢弃")

        except Exception as e:
            import traceback
            print(f"[FeishuChannel] 消息处理异常: {e}")
            traceback.print_exc()

    async def send(self, message: OutboundMessage) -> None:
        """发送消息到飞书（由 Gateway 出站循环调用，运行在主事件循环）。"""
        if self._lark_api_client is None:
            print("[FeishuChannel] API 客户端未初始化，无法发送消息")
            return

        try:
            # 构造"发消息"请求：receive_id_type="chat_id" 表示 receive_id 传会话 ID；
            # content 必须是 JSON 字符串，ensure_ascii=False 才能保留中文不转义
            request = im_v1.CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    im_v1.CreateMessageRequestBody.builder()
                    .receive_id(message.chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": message.content}, ensure_ascii=False))
                    .build()
                ) \
                .build()

            # 飞书 SDK 的 API 是同步阻塞的，用 asyncio.to_thread 包装避免卡事件循环
            response = await asyncio.to_thread(
                self._lark_api_client.im.v1.message.create,
                request,
            )

            # 飞书 API 业务失败不抛异常，需显式检查 success()，再打印 code/msg 便于排查
            if not response.success():
                print(f"[FeishuChannel] 发送失败: code={response.code} msg={response.msg}")

        except Exception as e:
            import traceback
            print(f"[FeishuChannel] 发送消息异常: {e}")
            traceback.print_exc()

    async def stop(self) -> None:
        """停止飞书客户端连接。"""
        self._running = False
        self._loop = None        # 清掉主循环引用，避免回调误用已关闭的循环
        if self._client is not None:
            self._client = None   # 断引用，交给 GC 回收 WS 连接
        self._lark_api_client = None
