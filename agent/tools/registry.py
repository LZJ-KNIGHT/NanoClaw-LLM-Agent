"""工具注册与调度中心。

ToolRegistry 集中管理一组 :class:`Tool` 实例，负责注册、导出 function-calling
定义、按名称分发执行，并兜底处理「工具不存在」和「执行抛异常」两类常见错误。

上层只依赖「名字 → 工具」的查找，不关心具体有哪些工具、怎么实现，新增工具只需
register 一行。
"""

from typing import Any

from agent.tools.base import Tool


class ToolRegistry:
    """工具注册表：按名称管理并调度一组工具。"""

    def __init__(self) -> None:
        """初始化一个空的工具存储。"""
        # key 为工具名，value 为 Tool 实例
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。

        若同名工具已存在，将被覆盖。

        Args:
            tool: 待注册的工具实例。
        """
        self._tools[tool.name] = tool

    def get_definitions(self) -> list[dict[str, Any]]:
        """汇总所有工具的 OpenAI function-calling 定义。

        Returns:
            由每个工具的 :meth:`Tool.to_function_definition` 结果组成的列表，
            可直接作为 Chat Completions 请求的 ``tools`` 参数。
        """
        return [tool.to_function_definition() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """按名称查找并执行工具。

        Args:
            name: 要调用的工具名。
            arguments: 传给工具的参数字典。

        Returns:
            工具执行结果字符串；当工具不存在或执行抛异常时，返回错误描述。
        """
        # 找不到工具时返回可读的错误文本而非抛异常，模型可能只是拼错了名字，
        # 看到错误后可自行纠正
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：未找到名为 '{name}' 的工具"

        try:
            return await tool.execute(**arguments)
        except Exception as exc:  # 兜底，避免异常向上扩散
            # 任何工具执行异常都转成字符串结果回填给模型，
            # 保证单次工具失败不会中断整个 ReAct 循环
            return f"错误：工具 '{name}' 执行失败：{exc}"

    def list_tools(self) -> list[str]:
        """列出所有已注册工具的名称。

        Returns:
            工具名列表。
        """
        return list(self._tools.keys())
