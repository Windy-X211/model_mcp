"""MCP 工具层：智能体 A 用这些工具领活、推流、定稿。"""

from __future__ import annotations

import json
import logging

from mcp.server.mcpserver import MCPServer

from .config import Config
from .prompts import SERVER_INSTRUCTIONS
from .queue import DEFAULT_WORKER, Bridge

logger = logging.getLogger(__name__)


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def build_mcp_server(bridge: Bridge, cfg: Config) -> MCPServer:
    server: MCPServer = MCPServer(
        name="model-mcp",
        title="模型服务 MCP",
        version="1.0.0",
        instructions=SERVER_INSTRUCTIONS,
        log_level="WARNING",
    )

    @server.tool(
        name="service_ready",
        description=(
            "向网关声明自己在岗：上报 worker_id、可服务的模型名与能力（stream/tools/reasoning）。"
            "返回当前队列与在线 worker 信息。开始接活前调用一次即可。"
        ),
    )
    async def service_ready(
        worker_id: str = "",
        models: list[str] | None = None,
        supports: list[str] | None = None,
        capacity: int = 0,
    ) -> str:
        info = bridge.touch_worker(worker_id or DEFAULT_WORKER, models, supports, capacity or None)
        return _dumps(
            {
                "ok": True,
                "worker_id": worker_id or DEFAULT_WORKER,
                "models": info["models"],
                "supports": info["supports"],
                "capacity": info["capacity"],
                "queue": {"queued": bridge.stats()["queued"], "active": bridge.stats()["active"]},
            }
        )

    @server.tool(
        name="service_wait",
        description=(
            "【核心】阻塞等待外部软件发来的模型请求（长轮询）。"
            "返回 status=request 时附带完整 OpenAI 请求（messages/tools/params/stream），"
            "用 request_id 处理并回填；返回 status=idle 表示这段时间无人调用，应立即再次调用本工具继续等。"
        ),
    )
    async def service_wait(worker_id: str = "", timeout_s: int = 0, models: list[str] | None = None) -> str:
        req = await bridge.wait_request(worker_id or DEFAULT_WORKER, timeout_s or None, models)
        if req is None:
            return _dumps({"status": "idle", "queue": bridge.stats(), "hint": "立刻再次调用 service_wait 继续等待"})
        return _dumps(req.wait_payload())

    @server.tool(
        name="stream_emit",
        description=(
            "流式推送：把当前“累计”的 content / reasoning / tool_calls 快照发上来，网关自动做差分并推给调用方。"
            "content 与 reasoning 传全量累计文本；tool_calls 传全量数组（arguments 是 JSON 字符串）。"
        ),
    )
    async def stream_emit(
        request_id: str,
        content: str | None = None,
        reasoning: str | None = None,
        tool_calls: list[dict] | str | None = None,
        worker_id: str = "",
    ) -> str:
        result = await bridge.emit(worker_id or DEFAULT_WORKER, request_id, content, reasoning, tool_calls)
        return _dumps(result)

    @server.tool(
        name="submit_result",
        description=(
            "定稿：提交这次请求的最终回复。流式场景先多次 stream_emit 再调本工具（只补最后差值）；"
            "一次性场景可直接带全文，网关会按小片回放成流式。finish_reason 用 stop / tool_calls / length。"
        ),
    )
    async def submit_result(
        request_id: str,
        content: str | None = None,
        reasoning: str | None = None,
        tool_calls: list[dict] | str | None = None,
        finish_reason: str = "",
        usage: dict | None = None,
        worker_id: str = "",
    ) -> str:
        result = await bridge.finalize(
            worker_id or DEFAULT_WORKER,
            request_id,
            content,
            reasoning,
            tool_calls,
            finish_reason or None,
            usage,
        )
        return _dumps(result)

    @server.tool(
        name="fail_result",
        description="放弃/报错：通知网关这次请求失败，调用方会收到 OpenAI 格式的错误响应。",
    )
    async def fail_result(request_id: str, message: str = "", code: str = "server_error", worker_id: str = "") -> str:
        result = await bridge.fail(worker_id or DEFAULT_WORKER, request_id, message or "worker failed", code)
        return _dumps(result)

    @server.tool(
        name="request_status",
        description="查询某个请求的状态：是否被调用方断开、剩余超时预算、已推送次数。生成途中可随时自查。",
    )
    async def request_status(request_id: str) -> str:
        return _dumps(bridge.status(request_id))

    @server.tool(
        name="service_stats",
        description="查看网关状态：排队数、处理中数量、在线 worker、暴露的模型名。",
    )
    async def service_stats() -> str:
        stats = bridge.stats()
        stats["workers"] = list(bridge.workers_online().keys())
        return _dumps(stats)

    @server.tool(
        name="service_offline",
        description="下线：告知网关本 worker 不再接活（结束前调用，避免请求被派给已离线的 worker）。",
    )
    async def service_offline(worker_id: str = "") -> str:
        return _dumps(bridge.offline(worker_id or DEFAULT_WORKER))

    return server
