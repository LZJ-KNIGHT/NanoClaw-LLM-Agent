"""NanoClaw 文件系统工具集。

提供三个面向工作区（workspace）的文件系统操作工具：

- :class:`ReadFileTool`  读取文件内容（超长自动截断）
- :class:`WriteFileTool` 写入文件（自动创建父目录）
- :class:`ListDirTool`   列出目录内容（带大小信息，目录带 ``/`` 后缀）

三者都继承 :class:`~agent.tools.base.Tool`，并做了统一的安全防护：用户传入的
相对路径先与工作区拼接、再取绝对路径，随后校验结果是否仍位于工作区内，阻止
``../../`` 之类的路径穿越。路径校验逻辑由公共父类 _WorkspaceTool 的
_resolve() 统一实现。
"""

import os
from typing import Any

from agent.tools.base import Tool

# 读取文件时的最大返回长度，超出部分截断
MAX_OUTPUT_CHARS = 16000


class _WorkspaceTool(Tool):
    """带工作区安全校验的工具基类（内部复用）。"""

    def __init__(self, workspace: str) -> None:
        """保存工作区根目录（取绝对路径，便于后续前缀比较）。"""
        self.workspace = os.path.abspath(workspace)

    def _resolve(self, user_path: str) -> str | None:
        """把用户路径解析成绝对路径，并校验是否位于工作区内。

        Args:
            user_path: 用户传入的相对/绝对路径。

        Returns:
            解析后的绝对路径；若越出工作区则返回 ``None``。
        """
        absolute = os.path.abspath(os.path.join(self.workspace, user_path))
        # 阻止 ../../ 之类的工作区逃逸。
        # 必须比较到分隔符级别：只写 startswith(ws) 会把兄弟目录误判为工作区内，
        # 例如工作区是 /data/workspace 时，/data/workspace-evil 同样以它开头。
        # absolute != ws 用于放行工作区目录本身（list_dir 的 dir_path 可缺省）。
        ws = self.workspace
        if absolute != ws and not absolute.startswith(ws + os.sep):
            return None
        return absolute


class ReadFileTool(_WorkspaceTool):
    """读取工作区内某个文件的内容，超长时自动截断。"""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "读取工作区内指定文件的内容"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "相对于工作区的文件路径",
                },
            },
            "required": ["file_path"],
        }

    async def execute(self, **kwargs: Any) -> str:
        # 先过安全校验，返回 None 说明路径逃出了工作区
        file_path = kwargs["file_path"]
        absolute = self._resolve(file_path)
        if absolute is None:
            return "拦截：路径越出工作区"

        if not os.path.isfile(absolute):
            return f"错误：文件不存在或不是文件：{file_path}"

        try:
            # errors="replace"：遇到无法解码的字节不报错，用替换符占位，
            # 避免个别脏字节导致整个读取失败
            with open(absolute, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as exc:  # 读取异常兜底
            return f"错误：读取失败：{exc}"

        # 截断，防止把超大文件整份塞给模型撑爆上下文
        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + f"\n...（已截断，共 {len(content)} 字符）"
        return content


class WriteFileTool(_WorkspaceTool):
    """把内容写入工作区内文件，自动创建缺失的父目录。"""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "把内容写入工作区内的指定文件，自动创建父目录"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "相对于工作区的文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文本内容",
                },
            },
            "required": ["file_path", "content"],
        }

    async def execute(self, **kwargs: Any) -> str:
        file_path = kwargs["file_path"]
        content = kwargs["content"]

        absolute = self._resolve(file_path)
        if absolute is None:
            return "拦截：路径越出工作区"

        try:
            # exist_ok=True：父目录已存在也不报错，避免重复创建抛异常
            os.makedirs(os.path.dirname(absolute), exist_ok=True)
            with open(absolute, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as exc:  # 写入异常兜底
            return f"错误：写入失败：{exc}"

        return f"已写入 {file_path}（{len(content)} 字符）"


class ListDirTool(_WorkspaceTool):
    """列出工作区内某个目录的内容，标注目录/文件及大小。"""

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "列出工作区内指定目录的内容（目录带 / 后缀，文件带大小）"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dir_path": {
                    "type": "string",
                    "description": "相对于工作区的目录路径，默认为工作区根目录",
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        # dir_path 缺省为空串，即等价于列出工作区根目录
        dir_path = kwargs.get("dir_path", "")
        absolute = self._resolve(dir_path)
        if absolute is None:
            return "拦截：路径越出工作区"

        if not os.path.isdir(absolute):
            return f"错误：目录不存在：{dir_path or '.'}"

        try:
            entries = sorted(os.listdir(absolute))
        except Exception as exc:  # 列目录异常兜底
            return f"错误：列出目录失败：{exc}"

        lines: list[str] = []
        for entry in entries:
            full = os.path.join(absolute, entry)
            # 目录加 "/" 后缀、文件标注字节数，方便模型区分目录和文件
            if os.path.isdir(full):
                lines.append(f"{entry}/")
            else:
                size = os.path.getsize(full)
                lines.append(f"{entry} ({size} 字节)")

        if not lines:
            return "（空目录）"
        return "\n".join(lines)
