"""Agent 主循环：驱动模型与工具交互，直到得出最终回答。

run() 一次调用即一轮 ReAct 循环：落盘用户消息 → 拼上下文 → 按 token 预算压缩
→ 调 Provider.chat → 有 tool_calls 就执行并回填 → 无则作为最终答复返回。

防死循环：迭代上限 max_iterations 硬性兜底；工具重复检测用滑动窗口（大小 30）
统计「工具名 + 参数」签名，重复 10 次告警、20 次熔断退出。
"""

import json
from typing import Any

from agent.context import ContextBuilder
from agent.memory import MemoryConsolidator
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider
from session.manager import SessionManager


# 工具调用签名滑动窗口大小
TOOL_HISTORY_WINDOW = 30
# 重复调用告警阈值
WARN_THRESHOLD = 10
# 重复调用熔断阈值
BREAK_THRESHOLD = 20

# 工具名 → 图标，终端展示时给不同工具配对应小图标
TOOL_ICONS = {
    "read_file": "📖",
    "write_file": "✍️",
    "list_dir": "📂",
    "exec": "💻",
    "web_search": "🔍",
    "web_fetch": "🌐",
}


class AgentLoop:
    """Agent 执行循环。

    把用户消息交给 LLM，模型请求工具则执行并回填结果，往复直到得到最终回答、
    出错或超过 max_iterations。同时管理跨轮对话历史（:attr:`_session_history`）
    与工具死循环防护（:attr:`_tool_call_history`）。
    """

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        context: ContextBuilder,
        session_manager: SessionManager,
        session_key: str = "cli:direct",
        model: str | None = None,
        max_iterations: int = 32,
        memory: MemoryConsolidator | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.context = context
        self.session_manager = session_manager
        self.session_key = session_key
        self.model = model
        self.max_iterations = max_iterations
        self.memory = memory

        self._tool_call_history: list[str] = []
        # 跨轮次对话历史：构造时从磁盘恢复上一进程留下的上下文
        self._session_history: list[dict[str, Any]] = session_manager.get_history(
            session_key
        )

    async def run(self, user_message: str) -> str:
        """执行一轮完整对话，返回最终回答文本（或错误/熔断/超时提示）。"""
        # 用户消息先落盘，保证后面即使崩了，用户说过的话也不丢
        self.session_manager.save_message(
            self.session_key, {"role": "user", "content": user_message}
        )

        messages = self.context.build_messages(
            history=self._session_history,
            current_message=user_message,
        )

        # 超出 token 预算时把中间旧历史压成摘要。压缩只在内存生效，必须重写回磁盘，
        # 否则下次启动又加载完整旧历史，反复触发超预算。
        if self.memory is not None:
            messages = await self.memory.maybe_consolidate(messages)
            # messages = [system] + history + [current_user]，去掉首尾即压缩后历史
            self._session_history = messages[1:-1]
            self.session_manager.rewrite(self.session_key, messages[1:])

        # 本轮新增消息的起点：压缩后当前用户消息仍在末尾
        new_start = len(messages) - 1

        tool_definitions = self.tools.get_definitions()

        # ReAct 子循环：模型思考 →（可能）调用工具 → 看结果再思考，最多 max_iterations 轮
        for _ in range(self.max_iterations):
            response = await self.provider.chat(
                messages, tool_definitions, self.model
            )

            if response.finish_reason == "error":
                return response.content or "错误：模型调用失败"

            # 打印思考过程，让用户能区分「在思考」还是「卡住了」。
            # reasoning 可能来自 message 顶层，也可能只挂在某个 tool_call 上。
            reasoning = response.reasoning_content or next(
                (tc.reasoning_content for tc in response.tool_calls if tc.reasoning_content),
                None,
            )
            if reasoning:
                print(f"\n💭 思考 > {reasoning}")

            if not response.has_tool_calls:
                content = response.content or ""
                final_msg = {"role": "assistant", "content": content}
                messages.append(final_msg)
                self.session_manager.save_message(self.session_key, final_msg)
                # 本轮所有新增消息（含工具往返）挂进跨轮历史，供下一轮复用
                self._save_to_history(messages[new_start:])
                return content

            # tool_calls 每个元素必须是严格的 id / type / function 三字段结构，
            # 少字段或多字段都可能被部分厂商 API 拒绝。
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ],
            }
            # reasoning_content 只能放消息顶层，绝不能混进上面 tool_calls 的每个元素：
            # 硅基流动等 API 在重放历史时对字段位置敏感，放错会直接报 20015 错误。
            first_reasoning = next(
                (tc.reasoning_content for tc in response.tool_calls if tc.reasoning_content),
                None,
            )
            if first_reasoning:
                assistant_msg["reasoning_content"] = first_reasoning
            messages.append(assistant_msg)
            # assistant 也要落盘，否则历史里的 tool 消息会找不到对应的 tool_calls
            self.session_manager.save_message(self.session_key, assistant_msg)

            for tc in response.tool_calls:
                icon = TOOL_ICONS.get(tc.name, "🛠️")
                print(f"{icon} 调用工具 {tc.name}({json.dumps(tc.arguments, ensure_ascii=False)})")

            for tc in response.tool_calls:
                # 用「工具名 + 稳定序列化(排序)后的参数」作为签名，排序是为了让
                # {"a":1,"b":2} 与 {"b":2,"a":1} 判定为同一次调用
                args_json = json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True)
                check = self._check_tool_loop(tc.name, args_json)

                if check is None:
                    result = await self.tools.execute(tc.name, tc.arguments)
                    # tool_call_id 必须与请求一一对应
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                    messages.append(tool_msg)
                    self.session_manager.save_message(self.session_key, tool_msg)
                elif check.startswith("熔断"):
                    return check
                else:
                    # 告警：跳过执行并回填错误提示引导模型换思路。SYSTEM_ERROR 前缀
                    # 告诉模型「这不是工具正常结果，是系统拦截」
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"SYSTEM_ERROR: {check}",
                    }
                    messages.append(tool_msg)
                    self.session_manager.save_message(self.session_key, tool_msg)

            continue

        # 跑满 max_iterations 仍无最终答复，兜底返回
        return f"已达到最大迭代次数（{self.max_iterations}），任务未完成，已停止。"

    def _check_tool_loop(self, tool_name: str, tool_args_json: str) -> str | None:
        """统计「工具名 + 参数」签名在滑动窗口内的重复次数，据此放行/告警/熔断。

        Returns:
            放行返回 ``None``；重复较多返回「警告」开头字符串（跳过执行）；
            疑似死循环返回「熔断」开头字符串（立即终止）。
        """
        # 先统计历史出现次数（只统计、还没把本次加进去）
        signature = f"{tool_name}:{tool_args_json}"
        count = self._tool_call_history.count(signature)

        if count >= BREAK_THRESHOLD:
            return (
                f"熔断：工具 '{tool_name}' 已重复调用 {count} 次，"
                f"疑似死循环，已强制停止。"
            )
        if count >= WARN_THRESHOLD:
            return (
                f"警告：工具 '{tool_name}' 已重复调用 {count} 次，"
                f"请检查是否存在死循环。"
            )

        self._tool_call_history.append(signature)
        # 超出窗口容量就把最老的一条挤出去（FIFO，只保留最近 N 次）
        if len(self._tool_call_history) > TOOL_HISTORY_WINDOW:
            self._tool_call_history.pop(0)
        return None

    def _save_to_history(self, messages_snapshot: list[dict[str, Any]]) -> None:
        """把本轮新增消息（run() 传入的 messages[new_start:]）延展到跨轮历史末尾。"""
        self._session_history.extend(messages_snapshot)

    def clear_history(self) -> None:
        """清空工具调用记录、会话历史与持久化文件。"""
        # 内存里的防死循环窗口、跨轮历史、磁盘 JSONL 文件，三处都要清
        self._tool_call_history.clear()
        self._session_history.clear()
        self.session_manager.clear(self.session_key)
