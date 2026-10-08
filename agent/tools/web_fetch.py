"""NanoClaw 网页抓取工具。

提供 :class:`WebFetchTool`，抓取指定 URL 的网页内容并转换成纯文本，通常配合
:class:`~agent.tools.web_search.WebSearchTool` 使用：先搜索、再抓取。

用 httpx.AsyncClient 异步抓取，适配项目的全异步架构；URL 协议白名单（只允许
http/https）是第一道 SSRF 防护，挡住 file://、ftp:// 等危险协议；用 html2text
把 HTML 转成 Markdown 纯文本，去掉标签噪声。
"""

import re
from typing import Any
from urllib.parse import urlparse

import html2text
import httpx

from agent.tools.base import Tool

# 抓取结果最大返回长度，超出部分截断
MAX_OUTPUT_CHARS = 12000
# 常见浏览器 UA，避免被目标站点当作爬虫拒绝
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class WebFetchTool(Tool):
    """抓取指定 URL 的网页内容并转换成纯文本。"""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return "抓取指定 URL 的网页内容。当你需要阅读某个具体网页的详细内容时使用。通常配合 web_search 工具先搜索再抓取。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的网页 URL",
                },
            },
            "required": ["url"],
        }

    async def execute(self, **kwargs: Any) -> str:
        url = kwargs.get("url", "")
        if not url:
            return "错误：未提供要抓取的 URL"

        # URL 安全检查：只允许 http/https。
        # SSRF 防护第一道闸门：白名单协议，拒绝 file:// / ftp:// / gopher://
        # 等可能被用来读取本地文件或探测内网的危险协议
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            return f"错误：URL 格式无效：{exc}"

        if not parsed.scheme:
            return "错误：URL 缺少协议，请以 http:// 或 https:// 开头"
        if parsed.scheme not in ("http", "https"):
            return "安全拦截：只允许 http/https 协议"

        try:
            # 用 async with 保证连接用完即关；follow_redirects 自动跟随跳转，
            # headers 带浏览器 UA，降低被站点当爬虫拒绝的概率
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = await client.get(url)
        except Exception as exc:  # 网络超时、DNS 解析失败等
            return f"抓取失败：{exc}"

        if not 200 <= response.status_code < 300:
            return f"抓取失败：HTTP {response.status_code}"

        # HTML 转纯文本（Markdown 格式）
        try:
            h = html2text.HTML2Text()
            h.ignore_links = False   # 保留链接，方便模型引用来源
            h.ignore_images = True   # 丢弃图片（纯文本模型用不上）
            h.body_width = 0         # 不按宽度自动折行，保持原文段落
            text = h.handle(response.text)
        except Exception as exc:  # HTML 解析异常
            return f"抓取失败：解析网页内容出错：{exc}"

        # 清理输出：压缩连续空行、超长截断
        # 把 3 个以上连续换行压成 2 个，去掉转换后残留的大段空白
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + "\n...(内容过长，已截断)"

        return text
