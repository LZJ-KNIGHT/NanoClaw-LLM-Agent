"""工具抽象基类定义。

Tool 是整个工具层的最底层抽象，规定「什么是一个工具」。AgentLoop 在 ReAct
循环里若模型返回 tool_calls，就会调用 ToolRegistry.execute(工具名, 参数) 执行
工具，再把字符串结果作为 role=tool 的消息回填给模型。filesystem / shell /
web_search / web_fetch / spawn / mcp 等具体工具都以它为公共父类。

子类必须实现 name / description / parameters / execute 四个成员；parameters
用 JSON Schema 描述参数，让模型知道该传什么、传什么类型。基类通过
to_function_definition() 把工具自动转成 OpenAI function calling 格式，子类只
需写业务属性，不用关心固定格式拼接。
"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """所有工具的抽象基类。

    一个 Tool 代表一个可供 Agent 调用的能力单元：向模型声明「能做什么、叫
    什么、需要哪些参数」，被调用时执行具体逻辑并返回字符串结果。

    子类实现 name / description / parameters / execute 四个抽象成员后，即可
    通过 :meth:`to_function_definition` 自动生成 OpenAI 风格的
    function-calling 描述。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，全局唯一标识，模型据此选择调用哪个工具。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，说明功能与适用场景，帮助模型判断何时调用。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """参数定义，遵循 JSON Schema 格式描述输入参数。"""
        raise NotImplementedError

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """执行工具逻辑，返回字符串形式的结果给模型。

        Args:
            **kwargs: 由模型根据 parameters 传入的实际参数。

        Returns:
            执行结果，通常为可读的文本。
        """
        raise NotImplementedError

    def to_function_definition(self) -> dict[str, Any]:
        """组装成 OpenAI function-calling 的 tools 描述。

        Returns:
            形如 ``{"type": "function", "function": {...}}`` 的字典，
            可直接放入 Chat Completions 请求的 ``tools`` 列表。
        """
        # 把子类的四个属性翻译成 OpenAI 认的 tools 结构，子类只需写业务属性
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
