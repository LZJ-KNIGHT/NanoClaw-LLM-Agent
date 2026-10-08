"""Web 渠道：用 FastAPI + WebSocket 提供网页版聊天入口。

GET / 返回前端页面，/ws 收发消息，内部用 uvicorn 起服务。
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

from bus.queue import InboundMessage, MessageBus, OutboundMessage
from .base import Channel


class WebChannel(Channel):
    """Web 渠道，通过 WebSocket 接收和发送消息。

    用连接路由表 _connections 维护 client_id → WebSocket，出站时按 chat_id
    （= 建连时生成的 client_id）反查连接直接推送，无需再走点对点队列。
    """

    def __init__(self, bus: MessageBus, host: str = "0.0.0.0", port: int = 8080):
        """初始化 Web 渠道，监听 host:port。"""
        super().__init__(name="web", bus=bus)
        self.host = host
        self.port = port
        self._connections: dict[str, WebSocket] = {}  # 连接路由表：client_id -> WebSocket 对象
        self._app: FastAPI | None = None              # FastAPI 应用实例
        self._server: uvicorn.Server | None = None    # uvicorn 服务器实例（用于优雅停止）

    async def start(self) -> None:
        """启动 FastAPI + WebSocket 服务。"""
        self._app = FastAPI(title="NanoClaw Web")

        self._register_routes()

        print(f"[WebChannel] 正在启动 Web 服务: http://{self.host}:{self.port}")

        # 用 uvicorn.Server.serve() 异步启动，不阻塞事件循环
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="info",
        )
        self._server = uvicorn.Server(config)
        # serve() 是长驻协程，直到 should_exit=True 才返回（见 stop()）
        await self._server.serve()

    def _register_routes(self) -> None:
        """注册 FastAPI 路由。"""
        if self._app is None:
            return

        # GET / -> 返回 index.html
        @self._app.get("/")
        async def index():
            """返回 Web UI 页面。"""
            index_path = Path(__file__).parent / "web_ui" / "index.html"
            if index_path.exists():
                return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
            else:
                return HTMLResponse(content="<h1>index.html not found</h1>")

        # WebSocket /ws
        @self._app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            """WebSocket 连接处理。"""
            # 三次握手完成后必须 accept，否则无法收发
            await websocket.accept()

            # 生成 client_id 作为本连接唯一标识，并登记到连接路由表
            client_id = str(uuid.uuid4())
            self._connections[client_id] = websocket

            print(f"[WebChannel] 新连接: client_id={client_id}")

            try:
                # receive_text() 会挂起等待，直到该连接发来数据
                while True:
                    data = await websocket.receive_text()

                    if not data.strip():
                        continue

                    print(f"[WebChannel] 收到消息: client_id={client_id} content={data[:50]}...")

                    # chat_id = client_id，Agent 回复时会原样带回，send() 据此找回这条连接
                    message = InboundMessage(
                        channel="web",
                        sender_id=client_id,
                        chat_id=client_id,
                        content=data,
                        raw={
                            "client_id": client_id,
                        },
                    )

                    await self.bus.publish_inbound(message)

            except Exception as e:
                # 连接断开：WebSocketDisconnect 等异常都会走到这里
                print(f"[WebChannel] 连接断开: client_id={client_id} reason={e}")

            finally:
                # 无论正常/异常退出都要清理连接表，防止内存泄漏和向死连接推送
                self._connections.pop(client_id, None)

    async def send(self, message: OutboundMessage) -> None:
        """发送消息到 WebSocket 客户端（由 Gateway 出站循环调用）。"""
        # 通过 chat_id（= 建连时生成的 client_id）找回对应的 WebSocket 连接
        ws = self._connections.get(message.chat_id)

        if ws is None:
            # 连接不存在（用户已断开）：直接丢弃，不报错
            print(f"[WebChannel] 连接不存在: chat_id={message.chat_id}")
            return

        try:
            await ws.send_text(message.content)
            print(f"[WebChannel] 消息已发送: chat_id={message.chat_id}")

        except Exception as e:
            # 发送失败往往意味着连接已死，顺手把它从连接表移除
            print(f"[WebChannel] 发送失败: chat_id={message.chat_id} reason={e}")
            self._connections.pop(message.chat_id, None)

    async def stop(self) -> None:
        """停止 Web 服务并关闭所有连接。"""
        if self._server is not None:
            # 设置 should_exit 让 await serve() 那个长驻协程返回
            self._server.should_exit = True
            self._server = None

        # 用 list(...) 先拷贝一份再遍历，避免迭代过程中集合被修改
        for client_id, ws in list(self._connections.items()):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()

        print("[WebChannel] 服务已停止")
