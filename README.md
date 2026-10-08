# NanoClaw 🦞

> 一个**多渠道 LLM Agent 平台**：同一套 Agent 内核，同时接入 **终端 CLI / 网页 / 飞书 / QQ** 四个渠道，
> 具备**工具调用（ReAct 循环）**、**联网搜索与网页抓取**、**MCP 协议扩展**、**技能系统**、
> **长上下文自动压缩** 与 **子 Agent 派生** 能力。

全异步实现（`asyncio`），**零 Agent 框架依赖，内核完全自研**——只用 `openai` SDK 做模型接入，
其余（消息总线、ReAct 循环、工具注册表、上下文压缩、渠道适配）全部自己实现。

---

## 目录

- [功能特性](#功能特性)
- [架构总览](#架构总览)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [项目结构](#项目结构)
- [技术栈](#技术栈)
- [常见问题](#常见问题)

---

## 功能特性

### 一、多渠道接入（一次开发，四处可用）

| 渠道 | 实现方式 | 说明 |
|---|---|---|
| CLI | 标准输入输出 | 启动即可用，支持 `/exit` `/clear` `/tools` |
| Web | FastAPI + WebSocket | 自带聊天前端，浏览器打开即用 |
| 飞书 | `lark-oapi` WebSocket 长连接 | 无需公网 IP，事件回调经 `run_coroutine_threadsafe` 转回主事件循环 |
| QQ | `qq-botpy` | 支持群聊与单聊 |

四个渠道共用**同一套 Agent 内核**，渠道层只负责「收发消息」，不掺杂任何业务逻辑。

### 二、Agent 内核

- **ReAct 循环**：模型思考 → 调用工具 → 观察结果 → 再思考，最多 32 轮
- **工具系统**：抽象基类 + 注册表，新增工具只需继承并注册
- **内置工具**：读写文件、列目录、执行 Shell、联网搜索、网页抓取、派生子 Agent
- **技能系统**：把「做事套路」写成 `SKILL.md`，模型按需加载，**加能力不用改代码**
- **上下文压缩**：token 超阈值时保留 system + 最近消息，中段交给模型摘要
- **长期记忆**：跨会话记住用户偏好
- **会话持久化**：JSONL 追加写，进程重启不丢历史

### 三、安全防护

路径穿越校验、Shell 危险命令黑名单、命令执行超时、URL 协议白名单（防 SSRF）、
工具输出截断、工具调用死循环熔断、子 Agent 递归深度限制、YAML 安全反序列化。

---

## 架构总览

一条消息从进入到返回，依次穿过下面这几层：

```text
用户消息
   │
   ▼
渠道层  channels/          CLI · Web · 飞书 · QQ
   │                       只负责收发消息，不掺杂任何业务逻辑
   │  写入 inbound queue
   ▼
消息总线  bus/queue.py      inbound / outbound 双队列，生产-消费解耦
   │
   ▼
网关  gateway.py            按 session_key 复用会话，路由消息
   │
   ▼
Agent 内核  agent/loop.py    ReAct 主循环：思考 → 调工具 → 观察（最多 32 轮）
   ├── context.py            System Prompt 构造
   ├── skills.py             技能按需加载，注入上下文
   ├── memory.py             上下文超阈值时压缩
   ├── tools/                工具注册表
   │      filesystem · shell · web_search · web_fetch · spawn_subagent · MCP
   ├── providers/            OpenAI 兼容协议 → LLM 服务
   └── session/              JSONL 会话持久化
   │
   ▼
渠道层  channels/            结果写回 outbound queue，回到原渠道
```

**依赖方向是单向的**（上层依赖下层，下层不认识上层）：

```
main.py（组合根）
   └── gateway.py（路由）
         └── agent/（Agent 内核）
               ├── providers/（模型接入）
               ├── session/（持久化）
               └── agent/tools/（工具）
                     └── bus/queue.py（消息总线，纯数据）
```

---

## 快速开始

### 环境要求

- Python **3.10+**（开发环境为 3.11.16）
- 可选：Node.js（仅当使用 filesystem MCP 服务时需要）

### 安装与启动

```bash
# 1. 克隆
git clone <your-repo-url>
cd nanoclaw

# 2. 创建虚拟环境
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# 3. 安装依赖（版本已锁定）
pip install -r requirements.txt

# 4. 准备配置
cp config.example.json config.json   # Windows: copy config.example.json config.json
# 然后编辑 config.json 填入你的 api_key（或改用环境变量，见下）

# 5. 启动
python main.py
```

启动后：

- **终端**直接打字即可对话
- **浏览器**打开 <http://localhost:8080> 使用网页版

### 只用环境变量（推荐，密钥不落盘）

```bash
# Windows PowerShell
$env:NANOCLAW_API_KEY = "你的密钥"
python main.py
```

```bash
# macOS / Linux
export NANOCLAW_API_KEY="你的密钥"
python main.py
```

---

## 配置说明

配置加载遵循三层优先级：**环境变量 > `config.json` > 代码内置默认值**。

| 环境变量 | 覆盖的配置项 | 说明 |
|---|---|---|
| `NANOCLAW_API_KEY` | `api_key` | 模型服务密钥 |
| `NANOCLAW_BASE_URL` | `base_url` | OpenAI 兼容接口地址 |
| `NANOCLAW_MODEL` | `model` | 默认模型名 |
| `NANOCLAW_FEISHU_APP_ID` | `feishu.app_id` | 飞书应用 ID |
| `NANOCLAW_FEISHU_APP_SECRET` | `feishu.app_secret` | 飞书应用密钥 |
| `NANOCLAW_QQ_APP_ID` | `qq.app_id` | QQ 机器人 ID |
| `NANOCLAW_QQ_APP_SECRET` | `qq.app_secret` | QQ 机器人密钥 |

### 换模型厂商

只要对方兼容 OpenAI 协议，**改配置即可，无需改代码**：

```json
{
    "api_key": "你的密钥",
    "base_url": "https://api.deepseek.com/v1",
    "models": { "main": "deepseek-chat" }
}
```

常见兼容端点：DeepSeek `https://api.deepseek.com/v1`、硅基流动 `https://api.siliconflow.cn/v1`、
月之暗面 `https://api.moonshot.cn/v1`、通义 `https://dashscope.aliyuncs.com/compatible-mode/v1`。

---

## 项目结构

```
nanoclaw/
├── main.py                  # 入口 / 组合根：装配配置、Provider、工具、渠道
├── gateway.py               # 路由中枢：消息总线 ↔ Agent，按 session_key 复用会话
├── config.py                # 配置加载（三层优先级）
├── config.example.json      # 配置模板（真实 config.json 不入库）
├── identity.md              # Agent 人设，会被注入 System Prompt
├── requirements.txt         # 依赖清单（已锁版本）
│
├── bus/
│   └── queue.py             # 消息总线：inbound / outbound 双队列
├── channels/                # 渠道适配层（每个渠道一个文件）
│   ├── base.py              #   Channel 抽象基类
│   ├── cli.py               #   终端
│   ├── web.py               #   网页（FastAPI + WebSocket）
│   ├── feishu.py            #   飞书
│   ├── qq.py                #   QQ
│   └── web_ui/index.html    #   网页聊天前端
├── providers/               # 模型接入层
│   ├── base.py              #   LLMProvider 抽象基类 + 统一响应结构
│   └── openai_compat.py     #   OpenAI 兼容协议实现
├── agent/                   # Agent 内核（项目核心）
│   ├── loop.py              #   ReAct 主循环
│   ├── context.py           #   System Prompt 构造
│   ├── memory.py            #   长上下文自动压缩
│   ├── skills.py            #   技能加载（渐进式披露）
│   └── tools/               #   工具层
│       ├── base.py          #     Tool 抽象基类
│       ├── registry.py      #     工具注册表
│       ├── filesystem.py    #     读写文件 / 列目录
│       ├── shell.py         #     执行命令（含黑名单）
│       ├── web_search.py    #     联网搜索
│       ├── web_fetch.py     #     网页抓取
│       ├── spawn.py         #     派生子 Agent
│       └── mcp.py           #     MCP 客户端
├── session/
│   └── manager.py           # 会话持久化（JSONL）
├── mcp_servers/             # MCP 服务端示例
│   ├── poetry_server.py     #   古诗词查询
│   └── filesystem_server.py #   文件系统
├── skills/                  # 技能库
│   ├── weather/SKILL.md
│   ├── translator/SKILL.md
│   └── code_review/SKILL.md
├── workspace/               # Agent 的工作目录（运行产物，不入库）
└── memory/                  # Agent 长期记忆（运行产物，不入库）
```

---

## 技术栈

| 层面 | 选型 | 为什么 |
|---|---|---|
| 异步模型 | `asyncio` | Agent 是 IO 密集型：等模型、等网络、等子进程，异步能显著提升并发吞吐 |
| 消息解耦 | 自研双队列消息总线 | 渠道与 Agent 互不认识，新增渠道零侵入 |
| 模型接入 | `openai` SDK（OpenAI 兼容协议） | 换厂商只改 `base_url`，不必为每家写适配 |
| Agent 内核 | 自研 ReAct 循环 | 逻辑透明可控，便于定制与调试，无框架黑盒 |
| 工具扩展 | 抽象基类 + 注册表 | 新增工具只需继承 + 注册，符合开闭原则 |
| 外部能力 | MCP 协议 | 用统一协议接入第三方工具生态，比自己造接口更通用 |
| Web 渠道 | FastAPI + uvicorn | 原生 async，与项目异步模型一致，自带 WebSocket |
| 配置 | dataclass + JSON + 环境变量 | 类型清晰、可读性好，密钥可走环境变量不落盘 |
| 持久化 | JSONL 追加写 | 追加写不重写全文件，天然适合逐条对话记录，出错易恢复 |
| 上下文管理 | 滑动窗口 + 模型摘要 | 比单纯截断保留了更多有效信息 |

---

## 常见问题

### `web_search` 超时 / 返回「搜索出错」

`web_search` 基于 `ddgs`（DuckDuckGo），**该服务在中国大陆无法直连**，因此在国内网络下会超时，
而 `web_fetch`（直接抓取指定 URL）不受影响。

解决办法（任选其一）：

```bash
# 方案一：走代理（注意 ddgs 只认 DDGS_PROXY，不认 HTTPS_PROXY）
# Windows PowerShell
$env:DDGS_PROXY = "http://127.0.0.1:7890"

# macOS / Linux
export DDGS_PROXY="http://127.0.0.1:7890"
```

方案二：开启代理软件的 **TUN 模式**（全局接管流量），而非仅开启系统代理。

> 注：`ddgs` 底层使用 `primp`，它**不读取系统代理设置**，必须显式设置 `DDGS_PROXY` 环境变量。

### 启动报 `ModuleNotFoundError`

虚拟环境未激活。请确认已执行 `.venv\Scripts\activate`（Windows）或 `source .venv/bin/activate`。

### 启动报 `FileNotFoundError: config.json`

必须在**项目根目录**启动，因为 `config.json` 与 `workspace` 均为相对路径。

### MCP 服务连接失败

不会导致启动失败（已做容错，会跳过并继续）。若使用 `filesystem` 服务，请确认本机已安装 Node.js。

### 报 `No module named 'pip._vendor.typing_extensions'`

解释器环境损坏，建议重建虚拟环境后重新 `pip install -r requirements.txt`。

---

## License

[MIT](LICENSE)
