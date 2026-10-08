"""长期记忆压缩模块。

对话历史超过 token 预算时，把中间的旧消息交给 LLM 压缩成摘要以控制上下文长度，
并把摘要持久化到 workspace/memory/HISTORY.md。

思路：超预算就保留 system 头部 + 最近若干条，把旧消息用一次 LLM 调用压成摘要，
摘要以 system 消息形式插回中间。
"""

import json
import os
from datetime import datetime

from providers.base import LLMProvider


class MemoryConsolidator:
    """对话历史的 Token 预算管理与记忆压缩器。

    估算消息列表的 token 数；超预算时保留 system 与最近 6 条消息，把中间旧消息
    压缩成一条摘要，并持久化到 HISTORY.md 供审计或复用。
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: str,
        token_budget: int = 6000,
    ) -> None:
        self.provider = provider
        self.workspace = workspace
        self.token_budget = token_budget

    def estimate_tokens(self, messages: list[dict]) -> int:
        """粗略估算消息列表的总 token 数。

        按每条消息 JSON 序列化后的字符长度 // 2 折算，中英文混合时约等于 token
        数量级。以字符数近似 token 数，避免引入 tokenizer 依赖和额外计算。
        """
        total = 0
        for msg in messages:
            # ensure_ascii=False 保证中文按 1 个字符算，不会被转义成 \uXXXX 撑大长度
            total += len(json.dumps(msg, ensure_ascii=False)) // 2
        return total

    async def maybe_consolidate(self, messages: list[dict]) -> list[dict]:
        """超出 token 预算时压缩历史（保留 system 与最后 6 条），否则原样返回。"""
        if self.estimate_tokens(messages) <= self.token_budget:
            return messages

        print(f"\n  🗜️ Token 预算超出（{self.estimate_tokens(messages)}/{self.token_budget}），正在压缩...")
        split = max(1, len(messages) - 6)
        # 切点不能落在 tool 消息上：tool 消息必须紧跟它所属的 assistant(tool_calls)，
        # 否则压缩后 API 重放历史会报
        # "Messages with role 'tool' must be a response to ... tool_calls"。
        # 往前挪，保证「assistant(tool_calls) + 其 tool 结果」整组留在同一侧。
        while split > 1 and messages[split].get("role") == "tool":
            split -= 1

        head = messages[:1]
        tail = messages[split:]
        old_messages = messages[1:split]

        if not old_messages:
            return messages

        summary = await self._summarize(old_messages)
        # 摘要以 role=system 插回，模型会把它当作既定背景事实
        summary_message = {
            "role": "system",
            "content": f"[历史摘要]: {summary}",
        }

        self._save_to_history(summary, len(old_messages))

        # 结果结构：[system 原 prompt] + [历史摘要 system] + [最近 tail]
        return head + [summary_message] + tail

    async def _summarize(self, messages: list[dict]) -> str:
        """把旧消息压缩成一段 3-5 句的摘要。"""
        # 把待压缩消息拍平成「role: content」文本
        parts = []
        for msg in messages:
            content = msg.get("content", "")
            # content 可能不是字符串（如 assistant 带 tool_calls 时为 None/结构），
            # 统一转成 JSON 文本，避免拼接时报类型错
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            parts.append(f"{msg.get('role', 'unknown')}: {content}")
        text = "\n\n".join(parts)

        prompt = (
            "请用 3-5 句话概括以下对话的关键信息，保留重要的事实和结论，"
            "省略过程细节和寒暄。只输出摘要，不要其他内容。"
        )
        try:
            response = await self.provider.chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ]
            )
        except Exception:
            # 摘要失败不能拖垮主流程：旧消息直接丢弃，返回占位说明即可
            return "（摘要生成失败，旧消息已丢弃）"

        summary = (response.content or "").strip()
        # 模型返回空或报错时同样降级处理
        if not summary or response.finish_reason == "error":
            return "（摘要生成失败，旧消息已丢弃）"

        return summary

    def _save_to_history(self, summary: str, original_count: int) -> None:
        """把一次压缩记录追加写入 workspace/memory/HISTORY.md。"""
        memory_dir = os.path.join(self.workspace, "workspace", "memory")
        os.makedirs(memory_dir, exist_ok=True)

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        entry = (
            f"## {timestamp}\n"
            f"压缩了 {original_count} 条旧消息\n\n"
            f"{summary}\n\n---\n"
        )

        path = os.path.join(memory_dir, "HISTORY.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
