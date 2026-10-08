"""NanoClaw 入口文件（组合根）。

把所有依赖手动装配起来：配置 → 消息总线 → 各渠道 → 路由网关 → Agent 工厂。
数据流：渠道消息 → InboundMessage → bus → Gateway（按 session_key 懒加载 AgentLoop）
→ 回复经 bus.publish_outbound → Gateway 分发 → Channel.send。
"""

import asyncio
import os
import sys

from agent.context import ContextBuilder
from agent.loop import AgentLoop, TOOL_ICONS
from agent.memory import MemoryConsolidator
from agent.skills import SkillsLoader
from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from agent.tools.registry import ToolRegistry
from agent.tools.shell import ExecTool
from agent.tools.web_fetch import WebFetchTool
from agent.tools.web_search import WebSearchTool
from agent.tools.spawn import SpawnSubagentTool
from agent.tools.mcp import MCPClientManager
from config import load_config
from providers.openai_compat import OpenAICompatProvider
from session.manager import SessionManager
from bus.queue import MessageBus
from channels import CLIChannel
from channels import FeishuChannel
from channels.qq import QQChannel
from channels.web import WebChannel
from gateway import Gateway


# 启动 banner，控制台打印用
BANNER = """
====================================
   🦞 NanoClaw · 智能编程助手
====================================
"""


def build_agent(config, session_key: str) -> AgentLoop:
    """构建一个会话的 Agent 实例（工厂函数）。

    一次装齐 Provider、ToolRegistry、SkillsLoader、SessionManager、ContextBuilder、
    MemoryConsolidator，最后组装成 AgentLoop。

    Args:
        config: 配置对象（由 load_config() 返回）
        session_key: 会话标识，格式为 "{channel}:{sender_id}"
    """
    # 模型接入：OpenAI 兼容 Provider（零依赖实现，可指向任意兼容网关）
    provider = OpenAICompatProvider(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
    )

    # 工具注册表：所有内置工具都注册进来，Agent 通过它按名字调用
    tools = ToolRegistry()
    # 文件系统三件套 + 命令执行（都被限制在 config.workspace 内）
    tools.register(ReadFileTool(config.workspace))
    tools.register(WriteFileTool(config.workspace))
    tools.register(ListDirTool(config.workspace))
    tools.register(ExecTool(config.workspace))
    # 联网工具：搜索与网页抓取
    tools.register(WebSearchTool())
    tools.register(WebFetchTool())

    # 注册子 Agent 工具
    def create_provider(model=None):
        """Provider 工厂，供子 Agent 使用；model 不传则回退到 models.subagent。"""
        # 子 Agent 默认用 subagent 模型（更便宜）
        if model is None and config.models:
            model = config.models.get("subagent", config.model)
        return OpenAICompatProvider(
            api_key=config.api_key,
            base_url=config.base_url,
            model=model or config.model
        )

    # 子 Agent 工具：允许主 Agent 派生一个带同样工具集的子 Agent 去处理子任务
    tools.register(SpawnSubagentTool(
        provider_factory=create_provider,
        tools_registry=tools,
        workspace=config.workspace,
    ))


    # 技能加载器：扫描 workspace/skills 下的技能，生成摘要注入 System Prompt
    skills_loader = SkillsLoader(os.path.join(config.workspace, "skills"))
    skills_summary = skills_loader.build_skills_summary()
    if skills_summary:
        print(f"{session_key} 发现技能 {len(skills_loader.list_skills())} 个")

    # 会话持久化：用 JSONL 存每个 session_key 的历史，进程重启后可恢复
    session_manager = SessionManager(
        os.path.join(config.workspace, "workspace", "sessions")
    )
    existing_history = session_manager.get_history(session_key)
    if existing_history:
        print(f"[{session_key}] 已恢复 {len(existing_history)} 条历史消息")

    # 上下文构建器：负责拼 System Prompt（人设 + 技能摘要 + 工具说明等）
    context = ContextBuilder(
        workspace=config.workspace,
        identity_file=config.identity_file,
        skills_summary=skills_summary,
    )

    # 记忆压缩：历史 token 超过预算(6000)时，调用模型把旧消息浓缩成摘要
    consolidator = MemoryConsolidator(
        provider=provider,
        workspace=config.workspace,
        token_budget=6000,
    )

    # 组装成 Agent 主循环
    agent = AgentLoop(
        provider=provider,
        tools=tools,
        context=context,
        session_manager=session_manager,
        session_key=session_key,
        model=config.model,
        max_iterations=config.max_iterations,
        memory=consolidator,
    )

    # 启动时打印已注册工具清单，方便排查工具是否挂上
    print("🔧 已注册工具：")
    for name in tools.list_tools():
        icon = TOOL_ICONS.get(name, "🛠️")
        print(f"  {icon} {name}")

    return agent


def main() -> None:
    """启动 NanoClaw：读配置 → 装配总线/渠道/网关 → 预热 CLI Agent → 跑网关。"""
    # Windows 控制台默认 GBK，emoji 输出会抛 UnicodeEncodeError，统一转 UTF-8
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    print(BANNER)

    config = load_config()
    if not config.api_key:
        print("错误：未配置 API Key。")
        print("请在 config.json 中填写 api_key，或设置环境变量 NANOCLAW_API_KEY。")
        sys.exit(1)

    # 消息总线：入站/出站两条队列，解耦渠道与 Agent
    bus = MessageBus()

    # MCP 管理器：连接在异步启动阶段完成（connect_all）
    mcp_manager = MCPClientManager(config.mcp_servers or {})

    def create_agent(session_key: str) -> AgentLoop:
        """把 session_key 变成实际 Agent：build_agent + 挂 MCP 远程工具。"""
        agent = build_agent(config, session_key)
        # 把 MCP 远程工具注册进 Agent
        for tool in mcp_manager.get_tools():
            agent.tools.register(tool)
        return agent

    # CLI 渠道始终启用，是本地调试的主要入口
    cli_channel = CLIChannel(bus)
    channels = [cli_channel]

    # 飞书渠道（配置了 feishu app_id / app_secret 就自动启用）
    if config.feishu_app_id and config.feishu_app_secret:
        feishu_channel = FeishuChannel(bus, config.feishu_app_id, config.feishu_app_secret)
        channels.append(feishu_channel)
        print("[启动] 已启用飞书渠道")
    else:
        print("[启动] 未配置飞书，跳过飞书渠道")

    # QQ 渠道（配置了 qq app_id / app_secret 就自动启用）
    if config.qq_app_id and config.qq_app_secret:
        qq_channel = QQChannel(bus, config.qq_app_id, config.qq_app_secret)
        channels.append(qq_channel)
        print("[启动] 已启用 QQ 渠道")
    else:
        print("[启动] 未配置 QQ，跳过 QQ 渠道")

    # Web 渠道（默认启用，不需要外部凭证）
    if config.web_enabled:
        web_channel = WebChannel(bus, host=config.web_host, port=config.web_port)
        channels.append(web_channel)
        print(f"[启动] 已启用 Web 渠道: http://{config.web_host}:{config.web_port}")
    else:
        print("[启动] Web 渠道已禁用")


    gateway = Gateway(bus, channels, create_agent)

    async def start() -> None:
        """异步启动编排：连接 MCP → 预热 CLI Agent → 跑网关 → 收尾关 MCP。"""
        # 连接所有 MCP Server（失败/超时跳过，不阻塞启动）
        # 必须在 create_agent 之前连好，否则子 Agent 拿到的是空的 MCP 工具列表
        await mcp_manager.connect_all()

        # 预热：MCP 工具加载完成后预创建 CLI Agent，让初始化信息在启动时就显示；
        # 直接写进 Gateway 的 _agents 缓存，后续 CLI 消息复用同一实例
        cli_session_key = "cli:local"
        agent = create_agent(cli_session_key)
        gateway._agents[cli_session_key] = agent

        # 注入工具列表和清空回调给 CLI 渠道
        cli_channel.tool_names = agent.tools.list_tools()
        cli_channel._clear_callback = lambda: agent.clear_history()

        try:
            # 进入网关主循环，常驻直到被取消/退出
            await gateway.run()
        finally:
            # 无论怎么退出都确保 MCP 子进程被回收
            await mcp_manager.shutdown()

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        print("\n[NanoClaw] 正在退出...")
        # os._exit(0) 是硬退出：MCP 子进程/后台线程可能挂住正常退出流程，
        # 普通 sys.exit 可能卡在 threading join，这里直接终止进程最省事
        os._exit(0)  # 强制退出，杀掉残留的 WebSocket 子线程



if __name__ == "__main__":
    main()
