# model-mcp

把**智能体 A** 变成对外可用的 **OpenAI 兼容模型服务**，通过 MCP 派活。

- 外部软件（任何 OpenAI SDK / Cline / Dify / 自研系统）调用 `POST /v1/chat/completions`；
- 网关把请求入队，A 用 MCP 工具 `service_wait` **阻塞等待**，领到请求体；
- A 自己推理，用 `stream_emit` 推流、`submit_result` 定稿；
- 网关按 OpenAI 格式把结果回给软件，支持 **流式 / 工具调用 / 思考（reasoning_content）**。

## 架构

```
外部软件（OpenAI 兼容客户端）
        │ POST /v1/chat/completions (stream 可选)
        ▼
model-mcp 网关（FastAPI + MCP SDK，单进程）
   ├─ 队列 / 租约 / 看门狗
   ├─ SSE 差分、心跳、回放
   └─ /mcp（MCP streamable-http，可选）
        ▲                        │
        │ 工具返回（请求体）      │ 工具调用（领活/推流/定稿）
        ▼                        ▼
智能体 A（你自己，负责真正的推理）
```

## 快速开始

**最省事的方式：双击 `启动模型服务.bat`** —— 无控制台启动桌面窗口（Tk 控制台），窗口内自动在 8765 端口起服务。

手动启动：

```powershell
cd F:\PythonProjects\TraeProjects\模型服务mcp

# 1) 装依赖（mcp 必须是 2.x）
F:\anaconda3\python.exe -m pip install -r requirements.txt

# 2) 启动
#    桌面窗口（推荐，内含可复制的 MCP 配置 / 接口信息）
F:\anaconda3\python.exe -m model_mcp.tk_ui --port 8765
#    浏览器控制台版（老方案，仍可用）
F:\anaconda3\python.exe -m model_mcp.launcher --port 8765
#    stdio 模式：给桌面智能体连（同时会拉起 HTTP 侧）
F:\anaconda3\python.exe -m model_mcp --transport stdio --port 8765
#    纯 HTTP 模式：A 通过 http://127.0.0.1:8765/mcp 连，软件走 /v1
F:\anaconda3\python.exe -m model_mcp --transport http --port 8765

# 3) 验证
curl.exe http://127.0.0.1:8765/v1/models
```

## 桌面窗口（Tk）

轻量 Tk 窗口，不依赖浏览器。启动后 120ms 内自动起服务（端口可在窗口顶部改），状态灯每 2 秒刷新。

| 标签页 | 内容 | 可复制 |
|---|---|---|
| 1 · MCP 配置 | stdio（JSON：python 路径 / `args` / `cwd` / `PYTHONPATH`）↔ HTTP（URL）单选切换；MCP 端点 | ✅ 配置块 / MCP 地址 |
| 2 · 模型接口 | Base URL、API Key、模型名；openai SDK 流式样例、curl 一行版、端点表 | ✅ 各代码块 / Base URL |
| 3 · 接活提示词 | 发给 A 的 `WORKER_PROMPT` 全文 | ✅ |
| 4 · 状态与日志 | 服务地址、在线 worker、队列深度、超时设置；滚动运行日志 | ✅ 清空日志 |

窗口顶部：端口输入、启动 / 停止、浏览器版（打开同一份 Web 控制台）。
关窗时若服务仍在跑会先确认，确认后停服再退出；代码块是只读控件，`Ctrl+C` 复制选中内容，无选区则复制整块。

- **单实例**：同一端口只允许一个窗口，重复双击只会把已存在的窗口切到前台后退出，不会再叠出第二个窗口抢端口；
- **窗口可见**：被系统以隐藏 / 最小化方式拉起时会自动还原并置前；
- **日志**：`logs/tk_ui.log`（滚动 1MB×3）。双击没反应先看这个文件，或改双击 `启动模型服务(调试).bat`（保留控制台，报错直接可见）。

`launcher`（Web 版）与 Tk 版共用同一套 `ServerController`，都可以用 `--port` 指定端口。

## 浏览器控制台（UI）

服务起来后打开 <http://127.0.0.1:8765/>，页面按当前运行环境自动生成内容（与 Tk 窗口同一份数据源）：

| 区块 | 内容 | 可复制 |
|---|---|---|
| 1 | MCP 配置：stdio（JSON，含 python 路径 / 项目目录 / 端口）、HTTP（URL）+ MCP 端点 | ✅ |
| 2 | 模型接口：Base URL、API Key、模型名、openai SDK / curl / 原生 HTTP 三种示例、端点表 | ✅ |
| 3 | 发给 A 的接活提示词全文 | ✅ |
| 4 | 实时状态（worker 在线、队列）与启动命令 | ✅ |

- 顶部徽标每 2 秒刷新：服务状态、worker 在线数、队列长度；
- 复制走的 `navigator.clipboard`，失败时自动降级为 `execCommand`；
- 配置数据来自 `GET /ui/info`，`/v1` 接口与 `/mcp` 端点同端口共存。

不想真接智能体时，用自带的模拟 A 跑通链路：

```powershell
# 终端 1：起服务
F:\anaconda3\python.exe -m model_mcp --transport http --port 8765
# 终端 2：模拟 A 接活
F:\anaconda3\python.exe examples\demo_agent_a.py
# 终端 3：模拟软件调用
F:\anaconda3\python.exe examples\openai_client.py --stream
```

## A 侧工作循环

```text
service_ready  →  service_wait（阻塞，最多 40s）
                    ├─ status=idle    → 立刻再 service_wait
                    └─ status=request → 读 messages/tools/params
                                         ├─ stream_emit（累计快照，可多次）
                                         └─ submit_result（定稿）
                                      →  回到 service_wait
```

把 `examples/agent_a_prompt.md` 里的提示词直接发给 A 即可让它常驻接活。

## MCP 工具

| 工具 | 作用 | 关键参数 |
|---|---|---|
| `service_ready` | 上报在岗与能力 | `worker_id`, `models`, `supports`, `capacity` |
| `service_wait` | **阻塞等待请求**（长轮询） | `worker_id`, `timeout_s`, `models` |
| `stream_emit` | 流式推送（累计快照，网关自动差分） | `request_id`, `content`, `reasoning`, `tool_calls` |
| `submit_result` | 定稿 | `request_id`, `content`, `reasoning`, `tool_calls`, `finish_reason`, `usage` |
| `fail_result` | 报错 | `request_id`, `message`, `code` |
| `request_status` | 查状态（是否断连、剩余预算） | `request_id` |
| `service_stats` | 网关与队列状态 | — |
| `service_offline` | 下线 | `worker_id` |

约定：

- `content` / `reasoning` / `tool_calls` 都传**累计全量**（不是增量），网关做 diff；
- `tool_calls` 是 OpenAI 结构数组，`function.arguments` 是 JSON 字符串；
- 返回 `client_gone=true` 表示调用方已断开，直接放弃该请求；
- 请求超时（默认 600s）、A 失联（默认 120s 无产出）都会自动收尾/重派。

## OpenAI 兼容接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | `agent-a` 等模型，附 `x_online` / `x_supports` |
| POST | `/v1/chat/completions` | 支持 `stream:true`（SSE），思考走 `reasoning_content` |
| GET | `/health` `/queue` | 存活与队列状态 |
| POST | `/mcp` | MCP streamable-http 端点 |

```powershell
curl.exe http://127.0.0.1:8765/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"agent-a\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'
```

## 接入配置

Trae / Cline 的 `mcp.json`（`examples/mcp_config_trae.json`）：

```json
{
  "mcpServers": {
    "model-mcp": {
      "command": "F:\\anaconda3\\python.exe",
      "args": ["-m", "model_mcp", "--transport", "stdio", "--port", "8765"],
      "cwd": "F:\\PythonProjects\\TraeProjects\\模型服务mcp",
      "env": { "PYTHONPATH": "F:\\PythonProjects\\TraeProjects\\模型服务mcp" }
    }
  }
}
```

Claude Desktop（`%APPDATA%\Claude\claude_desktop_config.json`）写法相同，见 `examples/mcp_config_claude.json`。

支持 HTTP 的客户端（或另一台机器上的 A）：

```json
{ "mcpServers": { "model-mcp": { "url": "http://127.0.0.1:8765/mcp" } } }
```

## 配置说明（config.yaml）

| 键 | 默认 | 说明 |
|---|---|---|
| `server.host` / `port` | 127.0.0.1 / 8765 | 监听地址；放开给局域网用 `0.0.0.0` |
| `server.auth_token` | null | 设置后 `/v1/*` 需 `Authorization: Bearer <token>` |
| `models[].name` | agent-a | 软件侧看到的模型名 |
| `queue.wait_timeout_s` | 40 | `service_wait` 单次阻塞上限，**要小于 MCP 客户端超时** |
| `queue.request_deadline_s` | 600 | 单请求总预算，超时回 504 |
| `queue.lease_s` | 120 | A 认领后无产出多久算失联 |
| `queue.reject_when_no_worker` | true | 没有 A 在岗时直接 503 |
| `stream.replay_chunk_chars` / `replay_delay_ms` | 24 / 30 | A 一次性给全文时的回放粒度 |
| `stream.heartbeat_s` | 15 | SSE 心跳间隔 |

## 测试

```powershell
F:\anaconda3\python.exe tests\smoke_core.py        # 核心：队列/流式/工具/断连
F:\anaconda3\python.exe tests\smoke_mcp_stdio.py   # 端到端：stdio MCP + HTTP
F:\anaconda3\python.exe tests\smoke_mcp_http.py    # 端到端：MCP over HTTP
```

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 软件收到 503 `no_worker` | A 没连上，或没在循环调 `service_wait` |
| A 的 `service_wait` 报超时 | 智能体软件的单次工具调用超时偏小，把 `wait_timeout_s` 调低到 20~30 |
| 流式没有打字效果 | A 只调了 `submit_result`；确认 `stream.replay_chunk_chars > 0` 会自动回放 |
| 断线后 A 还在写 | 看 `stream_emit` 返回的 `client_gone`，为 true 立即停 |
| 中文乱码 | 网关全链路 UTF-8；Windows 控制台看日志乱码不影响接口 |
