"""文件系统 MCP Server。

提供三个工具：read_file（读取工作区内文件，超长自动截断）、write_file
（写入工作区内文件，自动创建父目录）、list_dir（列出工作区内目录内容）。

工作区根目录由环境变量 ``NANOCLAW_WORKSPACE`` 指定，缺省为进程当前目录。
所有相对路径都会先拼接工作区、取绝对路径后校验是否仍位于工作区内，
以阻止 ``../../`` 之类的路径穿越。

运行方式（stdio 传输，供 MCP 客户端接入）：
    python mcp_servers/filesystem_server.py
"""

# 作为一个 MCP Server，通过 stdio 暴露下面 3 个文件操作工具给客户端（客户端
# 一侧见 agent/tools/mcp.py）。工具在 Agent 侧会带服务名前缀
# filesystem-server__xxx，与同名内置工具区分开、不冲突。

import os

from mcp.server.fastmcp import FastMCP

# 读取文件时的最大返回长度，超出部分截断，防止超大文件撑爆模型上下文
MAX_OUTPUT_CHARS = 16000

# 工作区根目录：优先取环境变量，缺省用进程当前目录。
# 先规范成绝对路径，_resolve() 里的前缀比较才有意义。
WORKSPACE = os.path.abspath(os.environ.get("NANOCLAW_WORKSPACE", os.getcwd()))

# 服务名会成为客户端工具名前缀（filesystem-server__xxx），
# 同时也是 mcp.py 配置里用来索引该连接的 key。
mcp = FastMCP("filesystem-server")


def _resolve(user_path: str) -> str | None:
    """把用户路径解析成绝对路径，并校验是否位于工作区内。"""
    # 把相对/绝对路径拼到工作区下并规范化（消解 ../ 等），
    # 再要求结果要么正好等于工作区、要么以「工作区 + 路径分隔符」开头，
    # 否则视为越界，返回 None 由调用方拦截。
    absolute = os.path.abspath(os.path.join(WORKSPACE, user_path))
    if absolute != WORKSPACE and not absolute.startswith(WORKSPACE + os.sep):
        return None
    return absolute


# 装饰器把函数注册成一个可被 MCP 客户端调用的工具：参数名/类型注解自动生成
# 入参 JSON Schema，函数 docstring 作为工具 description 暴露给模型。
@mcp.tool()
def read_file(file_path: str) -> str:
    """读取工作区内指定文件的内容"""
    absolute = _resolve(file_path)
    if absolute is None:
        return "拦截：路径越出工作区"
    if not os.path.isfile(absolute):
        return f"错误：文件不存在或不是文件：{file_path}"

    try:
        # errors="replace"：遇到无法解码的字节不抛异常，用替换符占位
        with open(absolute, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as exc:
        return f"错误：读取失败：{exc}"

    # 超长截断，并在末尾标注原始总长度
    if len(content) > MAX_OUTPUT_CHARS:
        content = content[:MAX_OUTPUT_CHARS] + f"\n...（已截断，共 {len(content)} 字符）"
    return content


@mcp.tool()
def write_file(file_path: str, content: str) -> str:
    """把内容写入工作区内的指定文件，自动创建父目录"""
    absolute = _resolve(file_path)
    if absolute is None:
        return "拦截：路径越出工作区"

    try:
        os.makedirs(os.path.dirname(absolute), exist_ok=True)
        with open(absolute, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        return f"错误：写入失败：{exc}"

    return f"已写入 {file_path}（{len(content)} 字符）"


@mcp.tool()
def list_dir(dir_path: str = "") -> str:
    """列出工作区内指定目录的内容（目录带 / 后缀，文件带大小）"""
    absolute = _resolve(dir_path)
    if absolute is None:
        return "拦截：路径越出工作区"
    if not os.path.isdir(absolute):
        return f"错误：目录不存在：{dir_path or '.'}"

    try:
        entries = sorted(os.listdir(absolute))
    except Exception as exc:
        return f"错误：列出目录失败：{exc}"

    lines = []
    for entry in entries:
        full = os.path.join(absolute, entry)
        # 目录加 "/" 后缀、文件标注字节数，便于区分
        if os.path.isdir(full):
            lines.append(f"{entry}/")
        else:
            lines.append(f"{entry} ({os.path.getsize(full)} 字节)")

    return "\n".join(lines) if lines else "（空目录）"


if __name__ == "__main__":
    mcp.run(transport="stdio")
