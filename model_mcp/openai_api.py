"""OpenAI 兼容 HTTP 层：/v1/models、/v1/chat/completions（含 SSE）。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware.cors import CORSMiddleware

from . import sse
from .config import Config
from .prompts import WORKER_PROMPT
from .queue import Bridge

logger = logging.getLogger(__name__)

_AUTH_HEADERS = {"WWW-Authenticate": "Bearer"}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"
_index_cache: dict[str, str] = {}


def _index_html() -> str:
    if "html" not in _index_cache:
        _index_cache["html"] = _INDEX_HTML.read_text(encoding="utf-8")
    return _index_cache["html"]


def _unauthorized(cfg: Config) -> JSONResponse:
    return JSONResponse(
        sse.error_body("缺少或错误的 API Key", type_="invalid_request_error", code="invalid_api_key"),
        status_code=401,
        headers=_AUTH_HEADERS,
    )


def _check_auth(request: Request, cfg: Config) -> JSONResponse | None:
    token = cfg.server.auth_token
    if not token:
        return None
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and header[7:].strip() == token:
        return None
    return _unauthorized(cfg)


def create_app(
    bridge: Bridge,
    cfg: Config,
    mcp_server: Any | None = None,
    mcp_app: Any | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await bridge.start()
        logger.info("model-mcp 启动：http://%s:%s  MCP: /mcp", cfg.server.host, cfg.server.port)
        if mcp_server is not None:
            async with mcp_server.session_manager.run():
                yield
        else:  # pragma: no cover
            yield
        await bridge.stop()
        logger.info("model-mcp 已停止")

    app = FastAPI(title="model-mcp", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", include_in_schema=False)
    async def ui_index() -> HTMLResponse:
        return HTMLResponse(_index_html())

    @app.get("/ui/info")
    async def ui_info() -> dict:
        exposed = "127.0.0.1" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
        base = f"http://{exposed}:{cfg.server.port}"
        config_path = os.environ.get("MODEL_MCP_CONFIG") or str(_PROJECT_ROOT / "config.yaml")
        token = cfg.server.auth_token
        return {
            "name": "model-mcp",
            "version": "1.0.0",
            "python_exe": sys.executable,
            "project_root": str(_PROJECT_ROOT),
            "config_path": config_path,
            "host": exposed,
            "port": cfg.server.port,
            "server_url": base,
            "base_url": f"{base}/v1",
            "mcp_url": f"{base}/mcp",
            "auth_required": bool(token),
            "api_key": token or "sk-local-dummy",
            "models": [m.name for m in cfg.models],
            "wait_timeout_s": cfg.queue.wait_timeout_s,
            "worker_prompt": WORKER_PROMPT,
            "transport_hint": "ui 服务默认由 launcher 以 HTTP 模式启动，MCP 端点同时在 /mcp 上可用",
        }

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "workers_online": len(bridge.workers_online()),
            **{k: v for k, v in bridge.stats().items() if k in ("queued", "active")},
        }

    @app.get("/queue")
    async def queue() -> dict:
        return bridge.stats()

    @app.get("/v1/models")
    async def list_models(request: Request) -> Any:
        denied = _check_auth(request, cfg)
        if denied:
            return denied
        online = bridge.workers_online()
        now = int(time.time())
        data = []
        for spec in cfg.models:
            serving = [
                wid
                for wid, info in online.items()
                if not info.get("models") or spec.name in info.get("models", [])
            ]
            data.append(
                {
                    "id": spec.name,
                    "object": "model",
                    "created": now,
                    "owned_by": "model-mcp",
                    "x_description": spec.description,
                    "x_online": bool(serving),
                    "x_capacity": spec.capacity,
                    "x_workers_online": len(serving),
                    "x_supports": spec.supports,
                }
            )
        return {"object": "list", "data": data}

    @app.get("/v1/models/{model_id}")
    async def get_model(model_id: str, request: Request) -> Any:
        denied = _check_auth(request, cfg)
        if denied:
            return denied
        spec = cfg.models_by_name.get(model_id)
        if spec is None:
            return JSONResponse(
                sse.error_body(f"未知模型 {model_id}", type_="invalid_request_error", code="model_not_found"),
                status_code=404,
            )
        online = bridge.workers_online()
        serving = [wid for wid, info in online.items() if not info.get("models") or spec.name in info.get("models", [])]
        return {
            "id": spec.name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "model-mcp",
            "x_description": spec.description,
            "x_online": bool(serving),
            "x_capacity": spec.capacity,
            "x_workers_online": len(serving),
            "x_supports": spec.supports,
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        denied = _check_auth(request, cfg)
        if denied:
            return denied
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                sse.error_body("请求体不是合法 JSON", type_="invalid_request_error", code="invalid_json"),
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                sse.error_body("请求体必须是对象", type_="invalid_request_error", code="invalid_body"),
                status_code=400,
            )

        model_name = str(body.get("model") or cfg.default_model())
        if model_name not in cfg.models_by_name:
            return JSONResponse(
                sse.error_body(f"未知模型 {model_name}", type_="invalid_request_error", code="model_not_found"),
                status_code=404,
            )
        if not body.get("messages"):
            return JSONResponse(
                sse.error_body("messages 不能为空", type_="invalid_request_error", code="invalid_messages"),
                status_code=400,
            )
        if cfg.queue.reject_when_no_worker and not bridge.workers_online():
            return JSONResponse(
                sse.error_body(
                    "当前没有在岗的 worker（智能体 A 未连接或未调用 service_wait）",
                    type_="service_unavailable",
                    code="no_worker",
                ),
                status_code=503,
            )

        stream = bool(body.get("stream"))
        req = bridge.create_request(body, model_name, stream)

        if not stream:
            outcome, payload = await bridge.wait_outcome(req)
            if outcome == "error":
                status = 504 if payload.get("code") == "timeout" else 500
                return JSONResponse(payload, status_code=status)
            return JSONResponse(sse.completion_payload(req))

        return StreamingResponse(
            _stream(req, bridge, cfg),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _stream(req, bridge: Bridge, cfg: Config) -> AsyncIterator[str]:
        heartbeat = max(5, cfg.stream.heartbeat_s)
        finished = False
        try:
            yield sse.encode(sse.role_chunk(req))
            while True:
                try:
                    event = await asyncio.wait_for(req.events.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield sse.HEARTBEAT_FRAME
                    continue
                kind = event[0]
                payload = event[1] if len(event) > 1 else None
                if kind == "delta":
                    yield sse.encode(sse.delta_chunk(req, payload or {}))
                elif kind == "final":
                    payload = payload or req.final_payload()
                    yield sse.encode(sse.finish_chunk(req, payload.get("finish_reason")))
                    if payload.get("usage"):
                        yield sse.encode(sse.usage_chunk(req, payload["usage"]))
                    finished = True
                    yield sse.DONE_FRAME
                    return
                elif kind == "error":
                    yield sse.encode(sse.error_chunk(payload or {}))
                    finished = True
                    yield sse.DONE_FRAME
                    return
                elif kind == "done":
                    if not finished:
                        finished = True
                        yield sse.DONE_FRAME
                    return
        except asyncio.CancelledError:  # 客户端中途断开
            bridge.mark_client_gone(req.request_id)
            raise
        finally:
            if not finished:
                bridge.mark_client_gone(req.request_id)

    if mcp_app is not None:
        app.mount("/", mcp_app)

    return app
