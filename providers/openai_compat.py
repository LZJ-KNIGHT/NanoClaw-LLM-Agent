"""OpenAI 兼容协议 Provider 实现。

面向所有兼容 OpenAI Chat Completions 协议的第三方服务（硅基流动、
Kimi、DeepSeek 等），统一通过 ``AsyncOpenAI`` 客户端发起请求，
并把返回结果归一化成 :class:`LLMResponse`。换厂商通常只需改 base_url 与 model。

实现上做了三处兼容处理：异常统一兜底成 LLMResponse(error)，不让异常
炸穿上层 Agent；模型的 arguments 是 JSON 字符串，此处解析且失败不崩；
reasoning_content 在 tool_call 层缺失时回退到 message 层。
"""

import json
from typing import Any

from openai import AsyncOpenAI

from providers.base import LLMProvider, LLMResponse, ToolCallRequest


class OpenAICompatProvider(LLMProvider):
    """基于 OpenAI 兼容协议的 LLM Provider。

    构造时传入 API 密钥、服务地址与默认模型，之后调用 :meth:`chat`
    即可获得统一的响应结构，屏蔽不同服务商在细节上的差异。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """初始化 Provider。

        参数:
            api_key: 服务商密钥。
            base_url: OpenAI 兼容协议的服务地址（换厂商主要就是改这里）。
            model: 默认模型名，chat() 未显式指定时用它。
            extra_headers: 额外请求头，部分服务商要求（如自定义鉴权/版本头）。

        client 只创建一次并被后续所有请求复用，避免每次调用都重建连接。
        """
        self.model = model
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """发起一次（可带工具的）对话请求，并把结果归一化为 LLMResponse。

        参数:
            messages: OpenAI 格式的消息列表，直接透传给服务端。
            tools: OpenAI 格式的工具定义；为空时不携带 tools/tool_choice。
            model: 临时覆盖默认模型，缺省用 self.model。

        返回:
            统一的 LLMResponse；任何异常都会被兜底成 finish_reason="error"，
            不向上抛，保证 Agent 主循环不会因单次请求失败而整体崩溃。
        """
        request_kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }

        # 仅在确有工具时才传 tools 与 tool_choice，避免服务端报错：
        # "The tools parameter must be specified when tool_choice is utilized"
        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = "auto"

        try:
            response = await self.client.chat.completions.create(**request_kwargs)
        except Exception as exc:  # 网络/鉴权/限流/超时等异常在此统一兜底
            return LLMResponse(content=f"错误信息：{exc}", finish_reason="error")

        choice = response.choices[0]  # 非流式请求，取第一个候选
        message = choice.message

        # 部分模型把思考过程放在 message 顶层，而非单个 tool_call 上
        message_reasoning = getattr(message, "reasoning_content", None)

        tool_calls: list[ToolCallRequest] = []
        for tc in message.tool_calls or []:  # 无工具调用时属性可能为 None
            raw_args = tc.function.arguments  # JSON 字符串，不是 dict
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                arguments = {}  # 模型偶尔吐出非法 JSON，解析失败退化为空参数

            tool_calls.append(
                ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=arguments,
                    # 优先取 tool_call 层级，缺失时回退到 message 层级
                    reasoning_content=getattr(tc, "reasoning_content", None)
                    or message_reasoning,
                )
            )

        usage: dict[str, Any] = {}
        if response.usage:  # 部分兼容服务不返回 usage，判空后再取值
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=message_reasoning,
        )
