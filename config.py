"""配置加载模块。

定义运行配置数据结构 ``NanoClawConfig`` 并提供统一入口；只依赖标准库，可被任意层
安全导入（不会循环依赖）。加载优先级：环境变量 > config.json > 代码默认值；
密钥类字段支持环境变量覆盖，避免把明文密钥提交进仓库。
"""

import json
import os
from dataclasses import dataclass

# 默认的 OpenAI 兼容接口地址（硅基流动）
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
# 默认模型名；可被 config.json 的 model 或 models.main 覆盖
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Pro"

# 环境变量 → 配置字段 的映射表。
# 让密钥可以通过环境变量注入（容器 / CI / 本机临时设置），优先级高于 config.json，
# 这样明文密钥就不必写进文件，也不会被误提交进仓库。
ENV_OVERRIDES = {
    "NANOCLAW_API_KEY": "api_key",
    "NANOCLAW_BASE_URL": "base_url",
    "NANOCLAW_MODEL": "model",
    "NANOCLAW_FEISHU_APP_ID": "feishu_app_id",
    "NANOCLAW_FEISHU_APP_SECRET": "feishu_app_secret",
    "NANOCLAW_QQ_APP_ID": "qq_app_id",
    "NANOCLAW_QQ_APP_SECRET": "qq_app_secret",
}


@dataclass
class NanoClawConfig:
    """NanoClaw 运行配置。

    Attributes:
        api_key: 服务商 API 密钥，优先从环境变量 ``NANOCLAW_API_KEY`` 读取。
        base_url: OpenAI 兼容接口地址，默认硅基流动。
        model: 默认模型。
        workspace: 工作区根目录，供文件系统工具与记忆使用。
        max_iterations: Agent 单轮最大迭代次数。
        identity_file: 人设文件名。
    """

    # 模型接入三要素：密钥 / 接口地址 / 默认模型
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    # 多模型分工表：main=主 Agent，subagent=子 Agent，cheap=便宜模型
    models: dict = None  # {"main": "...", "subagent": "...", "cheap": "..."}
    # MCP Server 配置：键是名字，值是启动命令与参数
    mcp_servers: dict = None  # {"name": {"command": ..., "args": [...]}}
    # Agent 文件系统工具与记忆的根目录
    workspace: str = "."
    # ReAct 循环单轮最大迭代次数（上限，防止 Agent 无限打转）
    max_iterations: int = 32
    # 人设文件（相对 workspace），启动时会被读入 System Prompt
    identity_file: str = "identity.md"

    # 飞书配置
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # QQ 配置
    qq_app_id: str = ""
    qq_app_secret: str = ""
    # Web 配置
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080


def load_config(config_path: str = "config.json") -> NanoClawConfig:
    """从 JSON 文件加载配置并叠加环境变量（环境变量 > 配置文件 > 默认值）。

    Args:
        config_path: 配置文件路径，不存在则只使用默认值。
    """
    # 零配置兜底：文件不存在也能拿到一份带默认值的配置
    config = NanoClawConfig()

    if os.path.isfile(config_path):
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        # 白名单式逐字段读取，避免把 JSON 里的未知键写进配置对象
        for key in (
            "api_key",
            "base_url",
            "model",
            "workspace",
            "max_iterations",
            "identity_file",
        ):
            if key in data:
                setattr(config, key, data[key])

        # models 是可选的多模型分工；只有顶层没有 model 时才回退到 models.main
        if "models" in data:
            config.models = data["models"]
            # 如果没有单独的 model 字段，用 models.main
            if "model" not in data and "main" in config.models:
                config.model = config.models["main"]

        if "mcp_servers" in data:
            config.mcp_servers = data["mcp_servers"]

        feishu = data.get("feishu", {})
        if feishu.get("app_id"):
            config.feishu_app_id = feishu["app_id"]
        if feishu.get("app_secret"):
            config.feishu_app_secret = feishu["app_secret"]

        qq = data.get("qq", {})
        if qq.get("app_id"):
            config.qq_app_id = qq["app_id"]
        if qq.get("app_secret"):
            config.qq_app_secret = qq["app_secret"]

        web = data.get("web", {})
        if "enabled" in web:
            config.web_enabled = web["enabled"]
        if web.get("host"):
            config.web_host = web["host"]
        if web.get("port"):
            config.web_port = web["port"]

    # 环境变量优先级最高：遍历映射表逐个覆盖，这样密钥可以完全不写进 config.json，
    # 避免明文落盘、也不会被误提交进仓库。
    for env_name, field_name in ENV_OVERRIDES.items():
        env_value = os.environ.get(env_name)
        if env_value:
            setattr(config, field_name, env_value)

    return config
