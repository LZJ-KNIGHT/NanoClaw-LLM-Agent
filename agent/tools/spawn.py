"""子任务 Agent 派生工具。

SpawnSubagentTool 让 Agent 把独立的子任务交给一个临时子 Agent 执行：
子 Agent 复制一份工具集（不含 spawn 自身）、使用固定的极简 system
prompt、不落盘，执行完成后返回结果。用 max_depth 限制递归派生深度，
避免无限派生。
"""

import uuid
from typing import Any, Callable

from agent.loop import AgentLoop
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider

# 子 Agent 的固定 system prompt
SUBAGENT_SYSTEM_PROMPT = "你是任务专员，完成任务直接输出结果。"


class DummySessionManager:
    """不落盘的会话管理器，供子 Agent 使用。

    子 Agent 是一次性的，无需持久化，因此三个方法都退化为空操作；这样调用方
    无需到处判断 session_manager 是否为 None（空对象模式）。
    """

    def get_history(self, session_key: str) -> list[dict[str, Any]]:
        # 子 Agent 永远从空历史开始，看不到任何主对话内容
        return []

    def save_message(self, session_key: str, message: dict[str, Any]) -> None:
        # 空操作：子 Agent 的消息不写盘、不持久化
        pass

    def clear(self, session_key: str) -> None:
        # 空操作：没有落盘自然也不用清理
        pass


class _SubagentContext:
    """子 Agent 的极简上下文：固定 system prompt，不注入人设/记忆/技能。"""

    def __init__(self, system_prompt: str) -> None:
        self._system_prompt = system_prompt

    def build_messages(
        self,
        history: list[dict[str, Any]] | None = None,
        current_message: str = "",
    ) -> list[dict[str, Any]]:
        """拼装发给子 Agent 模型的消息列表。

        Args:
            history: 历史消息（子 Agent 通常为空）。
            current_message: 本次要执行的任务描述。

        Returns:
            [system, ...history, user] 形式的消息列表。
        """
        # 子 Agent 只拿到一句固定的极简 system prompt，
        # 不注入主 Agent 的人设、长期记忆、技能等，保持上下文干净轻量
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt}
        ]
        if history:
            messages.extend(history)
        if current_message:
            messages.append({"role": "user", "content": current_message})
        return messages


class SpawnSubagentTool(Tool):
    """派生一个临时子 Agent 执行子任务并返回结果。"""

    def __init__(
        self,
        provider_factory: Callable[[str | None], LLMProvider],
        tools_registry: ToolRegistry,
        workspace: str,
        current_depth: int = 0,
        max_depth: int = 2,
    ) -> None:
        self._provider_factory = provider_factory
        self._tools_registry = tools_registry
        self._workspace = workspace
        self._current_depth = current_depth
        self._max_depth = max_depth

    @property
    def name(self) -> str:
        return "spawn_subagent"

    @property
    def description(self) -> str:
        return (
            "派生一个独立的子 Agent 执行子任务，完成后返回结果。"
            "当任务包含多个可并行或相互独立的步骤时应调用它，"
            "例如同时搜索 + 翻译 + 写多份文件，可拆给多个子 Agent 分头完成，"
            "避免主对话上下文被过程细节占满。子 Agent 看不到主对话历史，"
            "task 必须写清完整上下文和期望产出。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "交给子 Agent 的任务描述，要具体明确",
                },
                "model": {
                    "type": "string",
                    "description": "子 Agent 使用的模型名，缺省用默认模型",
                },
            },
            "required": ["task"],
        }

    async def execute(self, **kwargs: Any) -> str:
        task = (kwargs.get("task") or "").strip()
        model = kwargs.get("model")

        if not task:
            return "错误：未提供任务描述（task）"

        # model 为 None 时由 provider_factory 自选默认模型，
        # 子 Agent 可指定更便宜/更快的模型省钱
        provider = self._provider_factory(model)

        # 复制工具，跳过 spawn_subagent 自己
        sub_tools = ToolRegistry()
      
        for tool_name, tool in self._tools_registry._tools.items():
            if tool_name != self.name:
                sub_tools.register(tool)

        # 递归深度控制：current_depth + 1 < max_depth 时才继续下发 spawn，
        # 最多派生到 max_depth 层，避免无限自我复制
        if self._current_depth + 1 < self._max_depth:
            sub_tools.register(
                SpawnSubagentTool(
                    provider_factory=self._provider_factory,
                    tools_registry=self._tools_registry,
                    workspace=self._workspace,
                    current_depth=self._current_depth + 1,
                    max_depth=self._max_depth,
                )
            )

        # 极简 system prompt、不落盘；用独立的 session_key（spawn:随机）和
        # DummySessionManager，保证子 Agent 既读不到主历史，也不污染持久化存储
        sub_context = _SubagentContext(SUBAGENT_SYSTEM_PROMPT)
        sub_agent = AgentLoop(
            provider=provider,
            tools=sub_tools,
            context=sub_context,
            session_manager=DummySessionManager(),
            session_key=f"spawn:{uuid.uuid4().hex[:8]}",
            model=model,
        )

        # await 子 Agent 跑完，把它的最终输出当作本工具的结果返回给主 Agent
        return await sub_agent.run(task)
