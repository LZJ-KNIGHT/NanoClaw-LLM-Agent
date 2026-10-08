"""MCP 客户端集成模块。

通过 MCPClientManager 管理多个 MCP Server 的 stdio 连接，把远程工具包装成本地
MCPTool 注册进 ToolRegistry，供 Agent 统一调用。

两个容易踩的坑：

1. 生命周期必须手动管理：stdio_client / ClientSession 这些上下文管理器要跨多个
   函数长期持有（连接期一直存活），没法用 async with 包住一段局部代码，所以手动
   __aenter__ 建立、在 shutdown() 里 __aexit__ 关闭。
2. 超时要用 asyncio.timeout 而不是 asyncio.wait_for：wait_for 会在内部新建一个
   task 来跑被等待的协程，导致 stdio_client 里 anyio 的 cancel scope 进入与退出
   发生在不同 task，等到 shutdown 时跨 task 退出 cancel scope 会报错，子进程清理
   不掉；asyncio.timeout 则是在同一个 task 内实现超时，cancel scope 的进出保持
   在一条 task 上，关闭时才能正确回收子进程。
"""

import asyncio
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.tools.base import Tool


class MCPTool(Tool):
    """把一个远程 MCP 工具包装成本地 Tool。"""

    def __init__(
        self,
        client_manager: "MCPClientManager",
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict[str, Any],
    ) -> None:
        self._client_manager = client_manager
        self._server_name = server_name
        self._tool_name = tool_name
        self._description = description
        self._input_schema = input_schema

    @property
    def name(self) -> str:
        # 用 server 前缀避免不同 MCP Server 的工具重名冲突
        return f"{self._server_name}__{self._tool_name}"

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._input_schema

    async def execute(self, **kwargs: Any) -> str:
        # 本地只是个壳：真正的执行转发给 manager，由它通过已建立的
        # ClientSession 调用远程 MCP Server 上的工具
        return await self._client_manager.call_tool(
            self._server_name, self._tool_name, kwargs
        )

    def to_function_definition(self) -> dict[str, Any]:
        """直接用 MCP 工具的 inputSchema 组装 OpenAI function 描述。

        MCP 的 inputSchema 本身就是标准 JSON Schema，和 OpenAI function
        的 parameters 格式兼容，所以直接透传即可，无需转换。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._input_schema,
            },
        }


class MCPClientManager:
    """管理多个 MCP Server 的 stdio 连接与工具调用。

    设计上把「连接」和「调用」拆开：connect_all() 一次性把所有 Server 连好并加载
    工具，之后的 call_tool() 复用长连接，最后 shutdown() 统一清理。连接建立后需要
    贯穿整个进程生命周期，所以上下文管理器必须手动持有，不能用 async with 局部包
    起来。
    """

    def __init__(self, mcp_config: dict[str, Any]) -> None:
        # mcp_config 形如 {"server_name": {"command":..., "args": [...], ...}}
        self._mcp_config = mcp_config or {}
        self._context_managers: dict[str, Any] = {}   # stdio_client 上下文管理器
        self._session_managers: dict[str, Any] = {}   # ClientSession 上下文管理器
        self._sessions: dict[str, ClientSession] = {}  # 已连接的 session
        self._tools: list[MCPTool] = []               # MCPTool 列表

    async def _connect_one(self, server_name: str, server_config: dict[str, Any]) -> None:
        """连接单个 MCP Server：起子进程 → 建会话 → 初始化 → 拉取工具列表。

        Args:
            server_name: 服务器名，作为工具前缀与连接索引。
            server_config: 该服务器的配置（command / args / env / cwd）。
        """
        command = server_config.get("command")
        if not command:
            print(f"[MCP][!] 服务器 '{server_name}' 缺少 command，跳过")
            return

        # 若配置里写的是 "python" / "python3"，解析为「当前正在运行本项目的解释器」。
        # 这样配置文件不必硬编码某台机器的绝对路径；也避免 PATH 里另一个 Python
        # 抢先生效，导致子进程找不到项目依赖（依赖只装在运行本项目的环境里）。
        if command in ("python", "python3"):
            command = sys.executable

        # StdioServerParameters 描述「怎么把 MCP Server 子进程拉起来」
        params = StdioServerParameters(
            command=command,
            args=server_config.get("args", []),
            env=server_config.get("env"),
            cwd=server_config.get("cwd"),
        )

        # 手动 __aenter__：stdio_client 返回的是异步上下文管理器，正常应配
        # async with；但连接要长期存活，不能用局部 with，故手动进入并保存。
        ctx = stdio_client(params)
        session_ctx = None
        try:
            read, write = await ctx.__aenter__()
            self._context_managers[server_name] = ctx

            # ClientSession 同样手动 __aenter__，它会启动内部收发消息循环；
            # 若不进入就直接 initialize()，会永久阻塞拿不到响应。
            session_ctx = ClientSession(read, write)
            session = await session_ctx.__aenter__()
            self._session_managers[server_name] = session_ctx

            # MCP 握手：协商协议版本与能力
            await session.initialize()

            # 拉取该 Server 暴露的工具清单，逐个包装成本地 MCPTool
            result = await session.list_tools()
            self._sessions[server_name] = session

            for tool in result.tools:
                self._tools.append(
                    MCPTool(
                        client_manager=self,
                        server_name=server_name,
                        tool_name=tool.name,
                        description=tool.description or "",
                        input_schema=tool.inputSchema,
                    )
                )
            print(f"[MCP] 已连接服务器 '{server_name}'，加载 {len(result.tools)} 个工具")
        except BaseException:
            # 连接失败或被取消时，清理已建立的资源后重新抛出。
            # 回滚必须「先关 session 再关 stdio 进程」，顺序反了会导致子进程
            # 清理不干净；这里逐层 try 兜底，保证异常原样往上抛。
            if session_ctx is not None:
                try:
                    await session_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                self._session_managers.pop(server_name, None)
            if self._context_managers.pop(server_name, None) is not None:
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass
            raise

    async def connect_all(self, timeout: float = 30.0) -> None:
        """依次连接配置里的所有 Server，单个失败/超时不影响其它。"""
        # 用 asyncio.timeout 而非 asyncio.wait_for：wait_for 会新建 task，
        # 导致 stdio_client 内部 anyio cancel scope 的进入/退出跨 task，
        # shutdown 时无法正确清理子进程。asyncio.timeout 在同一 task 内实现超时。
        # 逐个串行连接（而非并发），失败只打印日志并 continue，
        # 保证一个坏 Server 不会拖垮整个启动流程
        for name, cfg in self._mcp_config.items():
            try:
                async with asyncio.timeout(timeout):
                    await self._connect_one(name, cfg)
            except TimeoutError:
                print(f"[MCP][!] 连接服务器 '{name}' 超时（>{timeout}s），跳过")
            except Exception as exc:
                print(f"[MCP][!] 连接服务器 '{name}' 失败：{exc}")

    def get_tools(self) -> list[MCPTool]:
        """返回已加载的所有 MCP 工具，供上层注册进 ToolRegistry。"""
        return self._tools

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        """通过已有 session 调用远程工具，并把结果转成字符串。

        Args:
            server_name: 目标服务器名。
            tool_name: 目标工具名。
            arguments: 传给远程工具的参数。

        Returns:
            远程工具返回的文本；出错时返回错误描述而非抛异常。
        """
        session = self._sessions.get(server_name)
        if session is None:
            return f"[MCP] 错误：服务器 '{server_name}' 未连接"

        try:
            # 复用长连接直接调用，跟前面 connect_all 建立的 session 是同一个
            result = await session.call_tool(tool_name, arguments or {})
        except Exception as exc:
            return f"[MCP] 错误：调用 {server_name}:{tool_name} 失败：{exc}"

        # MCP 的返回是 content 数组（可能是文本/图片/资源等），这里统一取出
        # text 字段拼接；非文本项退化为 str()，保证结果一定是字符串
        parts: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(str(item))
        body = "\n".join(parts) if parts else "(空结果)"
        # 工具级错误（isError）也当作正常字符串返回，让模型自己判断
        if getattr(result, "isError", False):
            body = f"[MCP] 工具返回错误：\n{body}"
        return body

    async def shutdown(self) -> None:
        """关闭所有连接：先退 session，再退底层 stdio 子进程。"""
        # 顺序不能反：session 建立在 stdio 之上，必须先关上层再关下层，
        # 与 _connect_one 里的回滚顺序保持一致
        for name in reversed(list(self._session_managers.keys())):
            try:
                await self._session_managers[name].__aexit__(None, None, None)
            except Exception as exc:
                print(f"[MCP][!] 关闭会话 '{name}' 失败：{exc}")
        self._session_managers.clear()

        for name in reversed(list(self._context_managers.keys())):
            try:
                # 这里能正确清理子进程的前提，正是连接阶段用了 asyncio.timeout
                # 而非 wait_for：cancel scope 没有跨 task，__aexit__ 才能正常执行
                await self._context_managers[name].__aexit__(None, None, None)
            except Exception as exc:
                print(f"[MCP][!] 关闭进程 '{name}' 失败：{exc}")
        self._context_managers.clear()
        self._sessions.clear()
