"""请求队列与状态机。

外部软件的请求进入 pending，由智能体 A 通过 service_wait 认领（claimed），
A 用 stream_emit 推增量（streaming），用 submit_result / fail_result 收尾。
看门狗负责超时、租约、断连和重排队。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import Config

logger = logging.getLogger(__name__)

PENDING = "pending"
CLAIMED = "claimed"
STREAMING = "streaming"
DONE = "done"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"

TERMINAL = frozenset({DONE, FAILED, TIMEOUT, CANCELLED})
ACTIVE = frozenset({PENDING, CLAIMED, STREAMING})

DEFAULT_WORKER = "agent-a"


def _prefix_delta(old: str, new: str) -> str:
    """按“新值 = 旧值 + 追加”求增量；检测到重写时退回公共前缀之后的剩余部分。"""
    if not new:
        return ""
    if not old:
        return new
    if new.startswith(old):
        return new[len(old) :]
    limit = min(len(old), len(new))
    i = 0
    while i < limit and old[i] == new[i]:
        i += 1
    if i >= len(new):
        return ""
    return new[i:]


def _normalize_tool_calls(raw: Any, request_id: str) -> list[dict]:
    """把各种形态的 tool_calls（JSON 字符串 / dict / list）规整成 OpenAI 结构。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("tool_calls 不是合法 JSON，已忽略：%s", text[:120])
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    out: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else {}
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append(
            {
                "index": int(item.get("index", index)),
                "id": str(item.get("id") or f"call_{request_id[-6:]}_{index}"),
                "type": str(item.get("type") or "function"),
                "function": {"name": str(fn.get("name", "")), "arguments": args},
            }
        )
    return out


def _tool_call_deltas(previous: list[dict], current: list[dict]) -> list[dict]:
    """对比前后两次 tool_calls 快照，产出 OpenAI 流式 tool_calls 增量。"""
    deltas: list[dict] = []
    prev_map = {c["index"]: c for c in previous}
    for call in current:
        idx = call["index"]
        old = prev_map.get(idx)
        entry: dict = {"index": idx}
        changed = False
        fn_out: dict = {}
        if old is None:
            entry["id"] = call["id"]
            entry["type"] = call["type"]
            fn_out["name"] = call["function"]["name"]
            fn_out["arguments"] = call["function"]["arguments"]
            changed = True
        else:
            if old["id"] != call["id"]:
                entry["id"] = call["id"]
                changed = True
            if old["function"]["name"] != call["function"]["name"]:
                fn_out["name"] = call["function"]["name"]
                changed = True
            args_delta = _prefix_delta(old["function"]["arguments"], call["function"]["arguments"])
            if args_delta:
                fn_out["arguments"] = args_delta
                changed = True
        if fn_out:
            entry["function"] = fn_out
        if changed:
            deltas.append(entry)
    return deltas


@dataclass
class BridgeRequest:
    request_id: str
    body: dict
    model: str
    stream: bool
    created_at: float
    deadline: float
    created_wall: float
    state: str = PENDING
    worker: str | None = None
    claimed_at: float | None = None
    last_activity: float = 0.0
    emitted: int = 0
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict | None = None
    client_gone: bool = False
    requeues: int = 0
    error: dict | None = None
    finished_at: float | None = None
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    replay_task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        if not self.last_activity:
            self.last_activity = self.created_at

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def remaining_ms(self) -> int:
        return max(0, int((self.deadline - time.monotonic()) * 1000))

    def wait_payload(self) -> dict:
        excluded = {"messages", "model", "stream", "tools", "tool_choice"}
        return {
            "status": "request",
            "request_id": self.request_id,
            "model": self.model,
            "stream": self.stream,
            "messages": self.body.get("messages") or [],
            "tools": self.body.get("tools") or [],
            "tool_choice": self.body.get("tool_choice"),
            "params": {k: v for k, v in self.body.items() if k not in excluded},
            "body": self.body,
            "deadline_ms": self.remaining_ms(),
            "created_at": int(self.created_wall),
            "seq_hint": self.emitted,
            "note": "处理后请用 stream_emit 推内容，最后用 submit_result 定稿。",
        }

    def message(self) -> dict:
        content = self.content if self.content else (None if self.tool_calls else "")
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if self.reasoning:
            msg["reasoning_content"] = self.reasoning
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c["id"], "type": c["type"], "function": c["function"]} for c in self.tool_calls
            ]
        return msg

    def final_payload(self) -> dict:
        return {
            "finish_reason": self.finish_reason or ("tool_calls" if self.tool_calls else "stop"),
            "usage": self.usage,
            "message": self.message(),
        }


class Bridge:
    """进程内的请求总线：HTTP 侧入队，MCP 侧取活与回填。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._requests: dict[str, BridgeRequest] = {}
        self._pending: list[str] = []
        self._notify = asyncio.Event()
        self._workers: dict[str, dict] = {}
        self._stop = asyncio.Event()
        self._watchdog: asyncio.Task | None = None

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._stop.clear()
            self._watchdog = asyncio.create_task(self._watchdog_loop(), name="model-mcp-watchdog")

    async def stop(self) -> None:
        self._stop.set()
        if self._watchdog:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog = None

    # ---------------------------------------------------------------- 对外入口
    def create_request(self, body: dict, model: str, stream: bool) -> BridgeRequest:
        now = time.monotonic()
        rid = "req_" + uuid.uuid4().hex[:12]
        req = BridgeRequest(
            request_id=rid,
            body=body,
            model=model,
            stream=stream,
            created_at=now,
            deadline=now + self.cfg.queue.request_deadline_s,
            created_wall=time.time(),
        )
        self._requests[rid] = req
        self._pending.append(rid)
        self._prune()
        self._notify.set()
        logger.info("请求入队 %s model=%s stream=%s", rid, model, stream)
        return req

    def get(self, request_id: str) -> BridgeRequest | None:
        return self._requests.get(request_id)

    def mark_client_gone(self, request_id: str) -> None:
        req = self._requests.get(request_id)
        if req and not req.terminal:
            req.client_gone = True
            logger.info("客户端断开 %s", request_id)

    async def wait_outcome(self, req: BridgeRequest) -> tuple[str, Any]:
        """非流式场景：阻塞直到拿到 final 或 error。"""
        while True:
            try:
                ev = await asyncio.wait_for(req.events.get(), timeout=5)
            except asyncio.TimeoutError:
                if req.terminal and req.error:
                    return "error", req.error
                continue
            kind = ev[0]
            if kind == "final":
                return "final", ev[1]
            if kind == "error":
                return "error", ev[1]
            if kind == "done":
                if req.error:
                    return "error", req.error
                return "final", req.final_payload()

    # ---------------------------------------------------------------- A 侧动作
    async def wait_request(
        self, worker_id: str = DEFAULT_WORKER, timeout_s: int | None = None, models: list[str] | None = None
    ) -> BridgeRequest | None:
        worker_id = worker_id or DEFAULT_WORKER
        cap = self.cfg.queue.wait_timeout_s
        timeout = min(timeout_s or cap, cap)
        end = time.monotonic() + max(1, timeout)
        self._touch_worker(worker_id)
        while True:
            req = self._take_pending(models)
            if req is not None:
                req.state = CLAIMED
                req.worker = worker_id
                req.claimed_at = time.monotonic()
                req.last_activity = req.claimed_at
                self._touch_worker(worker_id)
                logger.info("请求 %s 被 %s 认领", req.request_id, worker_id)
                return req
            remain = end - time.monotonic()
            if remain <= 0:
                return None
            self._notify.clear()
            if self._has_pending(models):
                continue
            try:
                await asyncio.wait_for(self._notify.wait(), min(remain, 5))
            except asyncio.TimeoutError:
                pass

    async def emit(
        self,
        worker_id: str,
        request_id: str,
        content: str | None = None,
        reasoning: str | None = None,
        tool_calls: Any = None,
    ) -> dict:
        req = self._requests.get(request_id)
        if req is None:
            return {"ok": False, "error": f"未知 request_id: {request_id}"}
        if req.terminal:
            return {"ok": False, "error": f"请求已结束（{req.state}）", "state": req.state}
        if req.worker and worker_id and req.worker != worker_id:
            return {"ok": False, "error": f"该请求由 {req.worker} 认领，当前 worker 无权写入"}
        if req.state == PENDING:
            req.state = CLAIMED
            req.worker = worker_id or req.worker
            req.claimed_at = time.monotonic()
        if req.client_gone:
            return {"ok": True, "client_gone": True, "pushed": 0, "message": "客户端已断开，无需继续生成"}

        now = time.monotonic()
        payload: dict = {}
        if reasoning is not None and reasoning != req.reasoning:
            delta = _prefix_delta(req.reasoning, reasoning)
            req.reasoning = reasoning
            if delta:
                payload["reasoning_content"] = delta
        if content is not None and content != req.content:
            delta = _prefix_delta(req.content, content)
            req.content = content
            if delta:
                payload["content"] = delta
        if tool_calls is not None:
            normalized = _normalize_tool_calls(tool_calls, request_id)
            deltas = _tool_call_deltas(req.tool_calls, normalized)
            req.tool_calls = normalized
            if deltas:
                payload["tool_calls"] = deltas

        req.last_activity = now
        self._touch_worker(worker_id)
        if not payload:
            return {"ok": True, "pushed": 0, "client_gone": False, "seq": req.emitted}
        req.emitted += 1
        req.state = STREAMING
        await req.events.put(("delta", payload))
        return {"ok": True, "pushed": len(payload), "client_gone": False, "seq": req.emitted}

    async def finalize(
        self,
        worker_id: str,
        request_id: str,
        content: str | None = None,
        reasoning: str | None = None,
        tool_calls: Any = None,
        finish_reason: str | None = None,
        usage: dict | None = None,
    ) -> dict:
        req = self._requests.get(request_id)
        if req is None:
            return {"ok": False, "error": f"未知 request_id: {request_id}"}
        if req.terminal:
            return {"ok": False, "error": f"请求已结束（{req.state}）", "state": req.state}
        if req.worker and worker_id and req.worker != worker_id:
            return {"ok": False, "error": f"该请求由 {req.worker} 认领，当前 worker 无权定稿"}
        if req.client_gone:
            self._close(req, CANCELLED, {"message": "client disconnected", "code": "client_gone"})
            return {"ok": True, "client_gone": True, "message": "客户端已断开，结果已丢弃"}

        tail: dict = {}
        if reasoning is not None and reasoning != req.reasoning:
            delta = _prefix_delta(req.reasoning, reasoning)
            req.reasoning = reasoning
            if delta:
                tail["reasoning_content"] = delta
        if content is not None and content != req.content:
            delta = _prefix_delta(req.content, content)
            req.content = content
            if delta:
                tail["content"] = delta
        if tool_calls is not None:
            normalized = _normalize_tool_calls(tool_calls, request_id)
            deltas = _tool_call_deltas(req.tool_calls, normalized)
            req.tool_calls = normalized
            if deltas:
                tail["tool_calls"] = deltas

        req.finish_reason = finish_reason or ("tool_calls" if req.tool_calls else "stop")
        req.usage = usage
        req.last_activity = time.monotonic()

        if tail and req.emitted > 0:
            req.emitted += 1
            await req.events.put(("delta", tail))

        # 流式但 A 全程没发过增量 → 由网关回放切块，软件侧仍然是打字机效果
        if req.stream and req.emitted == 0 and self.cfg.stream.replay_chunk_chars > 0:
            self._close(req, DONE, signal=False)
            req.replay_task = asyncio.create_task(self._replay(req), name=f"replay-{request_id}")
            return {"ok": True, "replayed": True, "finish_reason": req.finish_reason, "usage": req.usage}

        await req.events.put(("final", req.final_payload()))
        self._close(req, DONE)
        logger.info("请求完成 %s finish=%s", request_id, req.finish_reason)
        return {"ok": True, "finish_reason": req.finish_reason, "usage": req.usage}

    async def fail(self, worker_id: str, request_id: str, message: str, code: str = "server_error") -> dict:
        req = self._requests.get(request_id)
        if req is None:
            return {"ok": False, "error": f"未知 request_id: {request_id}"}
        if req.terminal:
            return {"ok": False, "error": f"请求已结束（{req.state}）", "state": req.state}
        self._close(req, FAILED, {"message": message, "code": code, "type": "server_error"})
        logger.warning("请求失败 %s: %s", request_id, message)
        return {"ok": True, "state": req.state}

    def status(self, request_id: str) -> dict:
        req = self._requests.get(request_id)
        if req is None:
            return {"ok": False, "error": f"未知 request_id: {request_id}"}
        return {
            "ok": True,
            "request_id": req.request_id,
            "state": req.state,
            "client_gone": req.client_gone,
            "emitted": req.emitted,
            "remaining_ms": req.remaining_ms(),
            "worker": req.worker,
            "finish_reason": req.finish_reason,
        }

    # ---------------------------------------------------------------- worker
    def touch_worker(
        self,
        worker_id: str,
        models: list[str] | None = None,
        supports: list[str] | None = None,
        capacity: int | None = None,
    ) -> dict:
        worker_id = worker_id or DEFAULT_WORKER
        info = self._workers.setdefault(worker_id, {})
        info.update(
            {
                "last_seen": time.monotonic(),
                "models": list(models) if models else list(info.get("models", [m.name for m in self.cfg.models]),
                ),
                "supports": list(supports) if supports else list(info.get("supports", ["stream", "tools", "reasoning"]),
                ),
                "capacity": int(capacity) if capacity else int(info.get("capacity", 1)),
            }
        )
        return info

    def offline(self, worker_id: str) -> dict:
        worker_id = worker_id or DEFAULT_WORKER
        existed = self._workers.pop(worker_id, None) is not None
        return {"ok": True, "worker_id": worker_id, "removed": existed}

    def _touch_worker(self, worker_id: str) -> None:
        if not worker_id:
            return
        info = self._workers.setdefault(worker_id, {"models": [m.name for m in self.cfg.models], "supports": ["stream", "tools", "reasoning"], "capacity": 1})
        info["last_seen"] = time.monotonic()

    def workers_online(self, stale_after: float = 90.0) -> dict[str, dict]:
        now = time.monotonic()
        online = {}
        for wid, info in self._workers.items():
            if now - float(info.get("last_seen", 0)) <= stale_after:
                online[wid] = info
        return online

    # ---------------------------------------------------------------- 统计
    def stats(self) -> dict:
        queued = sum(1 for r in self._requests.values() if r.state == PENDING)
        active = sum(1 for r in self._requests.values() if r.state in (CLAIMED, STREAMING))
        return {
            "ok": True,
            "queued": queued,
            "active": active,
            "tracked": len(self._requests),
            "workers_online": {k: v.get("capacity", 1) for k, v in self.workers_online().items()},
            "server": {"host": self.cfg.server.host, "port": self.cfg.server.port},
            "models": [m.name for m in self.cfg.models],
        }

    # ---------------------------------------------------------------- 内部
    def _has_pending(self, models: list[str] | None) -> bool:
        for rid in self._pending:
            req = self._requests.get(rid)
            if req and req.state == PENDING and (not models or req.model in models):
                return True
        return False

    def _take_pending(self, models: list[str] | None) -> BridgeRequest | None:
        for rid in list(self._pending):
            req = self._requests.get(rid)
            if req is None or req.state != PENDING:
                self._discard_pending(rid)
                continue
            if models and req.model not in models:
                continue
            if req.client_gone:
                self._discard_pending(rid)
                self._close(req, CANCELLED, {"message": "client disconnected", "code": "client_gone"})
                continue
            self._discard_pending(rid)
            return req
        return None

    def _discard_pending(self, rid: str) -> None:
        try:
            self._pending.remove(rid)
        except ValueError:
            pass

    def _close(self, req: BridgeRequest, state: str, error: dict | None = None, signal: bool = True) -> None:
        req.state = state
        req.finished_at = time.monotonic()
        req.error = error
        self._discard_pending(req.request_id)
        if not signal:
            return
        if error:
            req.events.put_nowait(("error", error))
        req.events.put_nowait(("done",))

    async def _replay(self, req: BridgeRequest) -> None:
        """A 一次性给了全文：网关按小片回放，制造流式观感。"""
        chunk = max(1, self.cfg.stream.replay_chunk_chars)
        delay = max(0, self.cfg.stream.replay_delay_ms) / 1000.0
        try:
            for start in range(0, len(req.reasoning), chunk):
                if req.client_gone:
                    return
                await req.events.put(("delta", {"reasoning_content": req.reasoning[start : start + chunk]}))
                if delay:
                    await asyncio.sleep(delay)
            for start in range(0, len(req.content), chunk):
                if req.client_gone:
                    return
                await req.events.put(("delta", {"content": req.content[start : start + chunk]}))
                if delay:
                    await asyncio.sleep(delay)
            if req.tool_calls:
                await req.events.put(
                    (
                        "delta",
                        {
                            "tool_calls": [
                                {
                                    "index": c["index"],
                                    "id": c["id"],
                                    "type": c["type"],
                                    "function": c["function"],
                                }
                                for c in req.tool_calls
                            ],
                        },
                    )
                )
            await req.events.put(("final", req.final_payload()))
            await req.events.put(("done",))
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            logger.exception("回放失败 %s", req.request_id)

    async def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(1)
            now = time.monotonic()
            for rid in list(self._requests.keys()):
                req = self._requests.get(rid)
                if req is None:
                    continue
                if req.terminal:
                    if req.finished_at and now - req.finished_at > self.cfg.queue.retain_terminal_s:
                        self._requests.pop(rid, None)
                    continue
                if req.client_gone:
                    self._close(req, CANCELLED, {"message": "client disconnected", "code": "client_gone", "type": "server_error"})
                    continue
                if now >= req.deadline:
                    self._close(
                        req,
                        TIMEOUT,
                        {"message": f"请求超时（{self.cfg.queue.request_deadline_s}s 内未完成）", "code": "timeout", "type": "server_error"},
                    )
                    continue
                if req.state in (CLAIMED, STREAMING) and now - req.last_activity > self.cfg.queue.lease_s:
                    if req.requeues < self.cfg.queue.requeue_max:
                        req.requeues += 1
                        req.state = PENDING
                        req.worker = None
                        req.emitted = 0
                        req.content = ""
                        req.reasoning = ""
                        req.tool_calls = []
                        if req.request_id not in self._pending:
                            self._pending.insert(0, req.request_id)
                        self._notify.set()
                        logger.warning("请求 %s 租约到期，退回队列（第 %d 次）", req.request_id, req.requeues)
                    else:
                        self._close(
                            req,
                            TIMEOUT,
                            {"message": "worker 长时间无产出，请求放弃", "code": "lease_timeout", "type": "server_error"},
                        )

    def _prune(self) -> None:
        if len(self._requests) <= self.cfg.queue.max_tracked:
            return
        drop = len(self._requests) - self.cfg.queue.max_tracked
        for rid in list(self._requests.keys()):
            if drop <= 0:
                break
            req = self._requests.get(rid)
            if req and req.terminal:
                self._requests.pop(rid, None)
                self._discard_pending(rid)
                drop -= 1
