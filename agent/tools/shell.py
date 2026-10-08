"""NanoClaw Shell 命令执行工具。

提供 :class:`ExecTool`，用于在工作区目录下执行一条 Shell 命令并返回输出。

执行前用 :data:`DENY_PATTERNS` 正则清单拦截常见危险命令——递归删除、格式化
磁盘、关机重启、权限提升、下载执行、端口监听、磁盘覆写、Fork 炸弹等，命中即
拒绝执行；另用 60 秒超时兜底，防止命令长时间挂起拖垮 Agent。

职责边界：黑名单覆盖常见危险写法，命令拼接、别名、编码等变体无法覆盖；进程级
隔离（容器 / 降权用户 / 受限文件系统）不在本工具职责范围内。_decode 优先
UTF-8、回退 GBK，用于解决 Windows 中文命令行输出的乱码。
"""

import asyncio
import os
import re
from typing import Any

from agent.tools.base import Tool

# 单次命令输出最大返回长度，超出部分截断
MAX_OUTPUT_CHARS = 10000
# 命令执行超时秒数
EXEC_TIMEOUT = 60

# 危险命令拦截清单：命中任意一条即拒绝执行（忽略大小写）。
# 黑名单拦截常见危险写法；命令拼接、别名、编码等变体无法覆盖，
# 进程级隔离（容器 / 降权用户 / 受限文件系统）不在本工具职责范围内。
DENY_PATTERNS: list[str] = [
    r"rm\s+.*-r",            # rm -r / -rf / -r -f / --recursive 递归删除
    r"rm\s+-fr",             # rm -fr 递归删除（-f 在前，上一条匹配不到）
    r"rmdir\s+/s",           # Windows rmdir /s 递归删除目录
    r"format\s+",            # Windows format 格式化磁盘
    r"mkfs",                 # Linux mkfs.* 格式化磁盘
    r"\bshutdown\b",         # 关机
    r"\breboot\b",           # 重启
    r"sudo\s+",              # sudo 提权
    r"\bsu\b",               # su 切换用户提权
    r"chmod\s+777",          # 危险权限修改
    r">\s*/dev/",            # 覆盖设备文件
    r"wget\s+.*\|\s*sh",     # wget 下载并直接执行脚本
    r"curl\s+.*\|\s*bash",   # curl 下载并直接执行脚本
    r"nc\s+-l",              # nc 监听端口（网络后门）
    r"ncat\s+-l",            # ncat 监听端口（网络后门）
    r"dd\s+if=",             # dd 磁盘镜像覆写
    r":\(\)\{.*\}",          # Fork 炸弹 :(){ :|:& };:
]


def _decode(data: bytes) -> str:
    """把命令输出字节解码为文本，优先 UTF-8，回退 GBK（Windows）。

    Windows 上很多命令（如 cmd 自带工具、中文软件）默认输出 GBK 编码，
    直接按 UTF-8 解码会抛 UnicodeDecodeError 或产生乱码，所以这里逐个
    编码尝试。

    Args:
        data: 命令 stdout/stderr 的原始字节。

    Returns:
        解码后的文本；所有候选编码都失败时用替换符兜底。
    """
    # 按优先级依次尝试：UTF-8（跨平台默认）→ GBK（Windows 中文）
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # 都失败就忽略错误、用替换符解出，保证不抛异常
    return data.decode(errors="replace")


class ExecTool(Tool):
    """执行 Shell 命令并返回输出结果的工具。"""

    def __init__(self, workspace: str = ".") -> None:
        self.workspace = os.path.abspath(workspace)

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "在工作区目录下执行一条 Shell 命令并返回输出"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 Shell 命令",
                },
            },
            "required": ["command"],
        }

    def _is_dangerous(self, command: str) -> str | None:
        """检查命令是否命中危险模式。

        Args:
            command: 待检查的命令字符串。

        Returns:
            命中时返回拦截信息，否则返回 ``None``。
        """
        for pattern in DENY_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return f"安全拦截：检测到危险命令模式 '{pattern}'"
        return None

    async def execute(self, **kwargs: Any) -> str:
        command = kwargs.get("command", "")
        if not command:
            return "错误：未提供要执行的命令"

        danger = self._is_dangerous(command)
        if danger:
            return danger

        try:
            # 用 create_subprocess_shell 起子进程异步执行命令，
            # cwd 限定在工作区，stdout/stderr 都接管道以便捕获输出
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            try:
                # 60 秒超时兜底：wait_for 到点会取消 communicate 的等待
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=EXEC_TIMEOUT
                )
            except asyncio.TimeoutError:
                # 超时后必须 kill 掉子进程，否则命令会一直挂着变僵尸；
                # 再 await communicate() 回收管道，避免资源泄漏
                process.kill()
                await process.communicate()
                return f"命令执行超时（{EXEC_TIMEOUT}秒），已终止"
        except Exception as exc:  # 启动子进程或通信异常兜底
            return f"错误：执行失败：{exc}"

        out = _decode(stdout)
        err = _decode(stderr)
        if err:
            out = f"{out}\n标准错误:\n{err}"

        # 输出过长截断，防止一条命令刷屏把模型上下文撑爆
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + "\n...(输出过长，已截断)"

        # 附上退出码，模型据此判断命令是成功还是失败
        return f"{out}\n[退出码: {process.returncode}]"
