"""NanoClaw 互联网搜索工具。

提供 :class:`WebSearchTool`，基于 DuckDuckGo（``ddgs`` 库）搜索互联网，返回带
标题、链接、摘要的结果列表。

ddgs 是同步阻塞库，直接 await 会卡住事件循环，因此用 asyncio.to_thread 丢进线程
池执行；结果拼接过长时截断，避免撑爆模型上下文。通常与
:class:`~agent.tools.web_fetch.WebFetchTool` 配合：先搜后抓。
"""

import asyncio
from typing import Any

from ddgs import DDGS

from agent.tools.base import Tool

# 搜索结果拼接后的最大返回长度，超出部分截断
MAX_OUTPUT_CHARS = 8000


class WebSearchTool(Tool):
    """用 DuckDuckGo 搜索互联网并返回结果。"""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "搜索互联网获取最新信息。当你需要查询实时信息、最新新闻或不确定的知识时使用。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回几条结果，默认 5",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query", "")
        max_results = kwargs.get("max_results", 5)

        if not query:
            return "错误：未提供搜索关键词"

        # ddgs 是同步阻塞调用，丢到线程池执行，避免卡住事件循环
        def _search() -> list[dict[str, Any]]:
            # DDGS 作为上下文管理器使用，退出时自动关闭底层连接
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))

        try:
            # to_thread 把同步的 _search 放到线程池跑，返回的是可 await 的对象，
            # 这样阻塞的网络请求不会冻住整个 asyncio 事件循环
            results = await asyncio.to_thread(_search)
        except Exception as exc:  # 网络/解析异常兜底
            return f"搜索出错: {exc}"

        if not results:
            return "未找到相关结果"

        lines: list[str] = []
        for i, item in enumerate(results, 1):
            title = item.get("title", "")
            href = item.get("href", "")
            body = item.get("body", "")
            # 用固定格式拼接，方便模型阅读和引用来源
            lines.append(f"### {i}. {title}\n链接: {href}\n{body}\n")

        out = "\n".join(lines)
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + "\n...(输出过长，已截断)"

        return out
