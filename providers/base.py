"""LLM Provider 的基础数据模型与抽象接口定义。

providers/ 是模型接入层，不含任何真实网络调用：上层（agent / agent.tools）
只认这里定义的结构和接口，不感知具体厂商。各厂商返回的差异都在这一层
归一化成 :class:`LLMResponse`，使上层调用无需关心具体 API 形态。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRequest:
    """一次工具调用请求，对应模型返回的单个 ``tool_calls`` 元素。

    Attributes:
        id: 该次调用的唯一标识，回传结果时需要用到。
        name: 要调用的工具名。
        arguments: 传给工具的参数字典。
        reasoning_content: 部分支持「思考」的模型（如 Kimi-K2.5、
            DeepSeek-R1）在发起 tool_calls 时附带的推理过程文本，
            无则保留为 ``None``，便于上层留存审计或调试。
    """

    id: str
    name: str
    arguments: dict[str, Any]
    reasoning_content: str | None = None


@dataclass
class LLMResponse:
    """一次模型对话的标准化返回结果。

    Attributes:
        content: 模型生成的文本内容，纯工具调用时可能为 ``None``。
        tool_calls: 模型请求执行的工具调用列表，无则为空列表。
        finish_reason: 结束原因，常见取值 ``stop`` / ``tool_calls``。
        usage: token 用量等统计信息，默认为空字典。
        reasoning_content: 模型在本轮返回前附带的「思考过程」文本，
            部分支持推理的模型（Kimi-K2.5、DeepSeek-R1 等）会给出，
            无则保留为 ``None``。与 :attr:`ToolCallRequest.reasoning_content`
            不同，它来自 message 顶层，即便没有工具调用也可能存在。
    """

    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    reasoning_content: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        """是否包含待执行的工具调用。"""
        return bool(self.tool_calls)


class LLMProvider(ABC):
    """LLM Provider 抽象基类。

    封装「调用大模型、拿到统一格式结果」这一职责。不同厂商（OpenAI、
    Kimi、DeepSeek 等）只需实现 :meth:`chat`，把各自 API 的原始返回
    归一化成 :class:`LLMResponse` 即可。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """发起一次对话请求。

        Args:
            messages: 对话消息列表，元素为 ``{"role": ..., "content": ...}``。
            tools: OpenAI 格式的工具定义列表，供模型按需选择调用。
            model: 指定模型名，缺省时使用 Provider 的默认模型。

        Returns:
            归一化后的模型响应。
        """
        raise NotImplementedError
