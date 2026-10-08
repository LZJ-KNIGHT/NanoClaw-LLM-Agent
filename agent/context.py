"""构建发送给 LLM 的上下文（Context）消息。

把每轮对话拼装成完整 messages：System Prompt（人设 + 环境信息 + 长期记忆 + 记忆
指引 + 技能目录）＋ 历史对话 ＋ 当前用户消息。本身不调用模型。
"""

import os
from datetime import datetime
from typing import Any

# 人设文件缺失时使用的兜底人设
DEFAULT_IDENTITY = (
    "你是一个运行在指定工作区内的智能助手，"
    "可以通过读写文件、浏览目录来协助用户完成任务。"
    "回答应简洁、准确、务实。"
)

# 长期记忆文件的相对路径（相对 workspace）。
# 「读」(_load_memory) 与「写」(System Prompt 里的记忆管理指引) 共用这一个常量，
# 避免两端各写一份字符串、改了一处忘了另一处而导致长期记忆静默失效。
MEMORY_RELATIVE_PATH = os.path.join("memory", "MEMORY.md")


class ContextBuilder:
    """把「人设 + 当前时间 + 工作区路径 + 长期记忆」拼成 System Prompt，再与历史对话、
    当前用户消息合并，产出可直接交给 LLM 的 messages。
    """

    def __init__(
        self,
        workspace: str,
        identity_file: str = "identity.md",
        skills_summary: str = "",
    ) -> None:
        """workspace 为工作区根目录；skills_summary 非空时注入 System Prompt。"""
        self.workspace = os.path.abspath(workspace)
        self.identity_file = identity_file
        # 技能摘要由外部（SkillsLoader.build_skills_summary）预先算好传入，
        # 避免每次构建都重新扫盘
        self.skills_summary = skills_summary

    def _load_identity(self) -> str:
        """读取人设文件内容，不存在时返回默认人设。"""
        if os.path.isfile(self.identity_file):
            with open(self.identity_file, encoding="utf-8") as f:
                return f.read()
        return DEFAULT_IDENTITY

    def _load_memory(self) -> str:
        """读取长期记忆（``<workspace>/memory/MEMORY.md``），不存在返回空串。

        路径与「记忆管理指引」里告诉模型写入的位置共用同一常量，保证写的能被读到。
        """
        memory_path = os.path.join(self.workspace, MEMORY_RELATIVE_PATH)
        if os.path.isfile(memory_path):
            with open(memory_path, encoding="utf-8") as f:
                return f.read()
        return ""

    def build_system_prompt(self) -> str:
        """拼接 System Prompt：人设、当前时间 + 工作区路径、长期记忆、记忆管理指引、
        技能目录摘要（后两者有内容才追加）。
        """
        identity = self._load_identity()
        # 只取日期不取时分，避免每轮都变化影响缓存
        current_time = datetime.now().strftime('%Y-%m-%d')
        system_prompt = f"""{identity}
## 环境信息
- 当前时间：{current_time}
- 工作区路径：{self.workspace}"""

        memory = self._load_memory()
        if memory:
            system_prompt += f"\n\n## 长期记忆\n\n{memory}"

        # 写入路径与 _load_memory 的读取路径共用同一常量，保证「写进去的能被读出来」
        memory_display_path = MEMORY_RELATIVE_PATH.replace(os.sep, "/")
        system_prompt += f"""

## 记忆管理指引
当你在对话中发现以下类型的重要信息时，使用 write_file 工具更新 `{memory_display_path}`：

    - 用户的姓名、职业、技术偏好
    - 用户的项目信息和工作习惯
    - 用户明确要求你记住的事情
    - 用户纠正过你的错误（避免下次再犯）

更新时读取现有内容，在末尾追加新条目，保持 Markdown 列表格式。
不要记录琐碎的对话细节，只记录长期有价值的信息。"""

        # 渐进式披露：只放技能名 + 简介，模型需要时再读全文
        if self.skills_summary:
            system_prompt += f"\n\n## 可用技能\n{self.skills_summary}"

        return system_prompt

    def build_messages(
        self,
        history: list[dict[str, Any]] | None = None,
        current_message: str = "",
    ) -> list[dict[str, Any]]:
        """构建完整 messages：System Prompt + 历史对话 + 当前消息，末条为当前用户输入。

        current_message 为空则不追加。
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt()}
        ]

        # 历史对话原样接在 system 之后
        if history:
            messages.extend(history)

        if current_message:
            messages.append({"role": "user", "content": current_message})

        return messages
