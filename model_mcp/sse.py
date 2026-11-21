"""OpenAI 兼容的响应组装：SSE chunk、非流式 payload、错误对象。"""

from __future__ import annotations

import json
from typing import Any

DONE_FRAME = "data: [DONE]\n\n"
HEARTBEAT_FRAME = ": ping\n\n"


def encode(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _base(req) -> dict:
    return {
        "id": f"chatcmpl-{req.request_id}",
        "object": "chat.completion.chunk",
        "created": int(req.created_wall),
        "model": req.model,
        "system_fingerprint": "model-mcp",
    }


def role_chunk(req) -> dict:
    body = _base(req)
    body["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
    return body


def delta_chunk(req, payload: dict) -> dict:
    body = _base(req)
    delta: dict[str, Any] = {}
    if payload.get("reasoning_content"):
        delta["reasoning_content"] = payload["reasoning_content"]
    if payload.get("content"):
        delta["content"] = payload["content"]
    if payload.get("tool_calls"):
        delta["tool_calls"] = payload["tool_calls"]
    body["choices"] = [{"index": 0, "delta": delta, "finish_reason": None}]
    return body


def finish_chunk(req, finish_reason: str | None) -> dict:
    body = _base(req)
    body["choices"] = [{"index": 0, "delta": {}, "finish_reason": finish_reason or "stop"}]
    return body


def usage_chunk(req, usage: dict) -> dict:
    body = _base(req)
    body["choices"] = []
    body["usage"] = usage
    return body


def error_chunk(error: dict) -> dict:
    return {
        "error": {
            "message": error.get("message", "internal error"),
            "type": error.get("type", "server_error"),
            "code": error.get("code", "server_error"),
        }
    }


def error_body(message: str, *, type_: str = "server_error", code: str = "server_error") -> dict:
    return {"error": {"message": message, "type": type_, "code": code}}


def completion_payload(req) -> dict:
    body = {
        "id": f"chatcmpl-{req.request_id}",
        "object": "chat.completion",
        "created": int(req.created_wall),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": req.message(),
                "finish_reason": req.finish_reason or ("tool_calls" if req.tool_calls else "stop"),
            }
        ],
    }
    if req.usage:
        body["usage"] = req.usage
    return body
