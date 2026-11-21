"""核心链路自测（不依赖 MCP 传输）：模拟 A 直接调用 Bridge，验证 OpenAI 兼容层。

运行：python tests/smoke_core.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx

from model_mcp.config import load_config
from model_mcp.openai_api import create_app
from model_mcp.queue import Bridge

PASSED: list[str] = []


def ok(name: str, detail: str = "") -> None:
    PASSED.append(name)
    print(f"[PASS] {name} {detail}")


async def main() -> None:
    cfg = load_config()
    cfg.queue.request_deadline_s = 30
    cfg.queue.reject_when_no_worker = True

    bridge = Bridge(cfg)
    app = create_app(bridge, cfg)
    await bridge.start()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as client:

        # 1. 没有 worker 时直接 503
        r = await client.post(
            "/v1/chat/completions", json={"model": "agent-a", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert r.status_code == 503, r.text
        ok("无 worker 时 503", r.json()["error"]["code"])

        # 2. 模型不存在 -> 404
        r = await client.post(
            "/v1/chat/completions", json={"model": "ghost", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert r.status_code == 404, r.text
        ok("未知模型 404", r.json()["error"]["code"])

        # A 上线
        bridge.touch_worker("agent-a")
        r = await client.get("/v1/models")
        body = r.json()
        assert body["data"][0]["x_online"] is True
        ok("/v1/models 显示在线", body["data"][0]["id"])

        async def roundtrip(payload: dict, handler, *, stream: bool = False):
            """发起请求 + 模拟 A 处理，返回 (响应, 收集到的 SSE 行)。"""
            lines: list[str] = []

            async def consume():
                if stream:
                    async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
                        async for line in resp.aiter_lines():
                            if line.strip():
                                lines.append(line)
                    return None
                return await client.post("/v1/chat/completions", json=payload)

            task = asyncio.create_task(consume())
            req = await bridge.wait_request("agent-a", timeout_s=5)
            assert req is not None, "worker 没领到请求"
            await handler(req)
            return await task, lines

        # 3. 非流式 + reasoning
        async def h_basic(req):
            await bridge.finalize("agent-a", req.request_id, content="你好，我是 agent-a", reasoning="先想一下再回答")

        resp, _ = await roundtrip(
            {"model": "agent-a", "messages": [{"role": "user", "content": "你是谁"}]}, h_basic
        )
        data = resp.json()
        assert data["choices"][0]["message"]["content"] == "你好，我是 agent-a", data
        assert data["choices"][0]["message"]["reasoning_content"] == "先想一下再回答", data
        ok("非流式 + reasoning", data["id"])

        # 4. 工具调用（非流式）
        async def h_tool(req):
            await bridge.finalize(
                "agent-a",
                req.request_id,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": json.dumps({"city": "太原"}, ensure_ascii=False)},
                    }
                ],
                finish_reason="tool_calls",
            )

        payload = {
            "model": "agent-a",
            "messages": [{"role": "user", "content": "太原天气"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "查天气",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    },
                }
            ],
        }
        resp, _ = await roundtrip(payload, h_tool)
        data = resp.json()
        choice = data["choices"][0]
        assert choice["finish_reason"] == "tool_calls", data
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather", data
        assert choice["message"]["content"] is None, data
        ok("工具调用透传", choice["message"]["tool_calls"][0]["id"])

        # 5. 流式：A 多次 emit 累计快照
        async def h_stream(req):
            await bridge.emit("agent-a", req.request_id, reasoning="思考中")
            await asyncio.sleep(0.05)
            await bridge.emit("agent-a", req.request_id, content="你")
            await asyncio.sleep(0.05)
            await bridge.emit("agent-a", req.request_id, content="你好")
            await bridge.finalize("agent-a", req.request_id, content="你好呀！")

        payload = {"model": "agent-a", "messages": [{"role": "user", "content": "打个招呼"}], "stream": True}
        _, lines = await roundtrip(payload, h_stream, stream=True)
        text = "".join(lines)
        assert "[DONE]" in text, text
        chunks = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
        content = "".join(
            c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices") and c["choices"][0].get("delta")
        )
        reasoning = "".join(
            c["choices"][0]["delta"].get("reasoning_content", "")
            for c in chunks
            if c.get("choices") and c["choices"][0].get("delta")
        )
        assert content == "你好呀！", content
        assert reasoning == "思考中", reasoning
        ok("流式累计快照差分", f"content={content!r}")

        # 6. 流式 + 一次性全文：网关回放
        async def h_replay(req):
            await bridge.finalize("agent-a", req.request_id, content="这是一次性提交的长文本，网关会按小片回放成流式效果。")

        payload = {"model": "agent-a", "messages": [{"role": "user", "content": "长文本"}], "stream": True}
        _, lines = await roundtrip(payload, h_replay, stream=True)
        chunks = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
        deltas = [
            c["choices"][0]["delta"].get("content", "")
            for c in chunks
            if c.get("choices") and c["choices"][0].get("delta")
        ]
        joined = "".join(deltas)
        assert joined == "这是一次性提交的长文本，网关会按小片回放成流式效果。", joined
        assert len([d for d in deltas if d]) > 1, deltas
        ok("一次性提交回放流式", f"{len(deltas)} 片")

        # 7. 流式工具调用增量拼装
        async def h_stream_tool(req):
            partial = [{"index": 0, "id": "call_s1", "function": {"name": "get_weather", "arguments": ""}}]
            await bridge.emit("agent-a", req.request_id, tool_calls=partial)
            await asyncio.sleep(0.05)
            await bridge.emit(
                "agent-a",
                req.request_id,
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_s1",
                        "function": {"name": "get_weather", "arguments": '{"city":"太原"}'},
                    }
                ],
            )
            await bridge.finalize("agent-a", req.request_id)

        payload = {"model": "agent-a", "messages": [{"role": "user", "content": "天气"}], "stream": True, "tools": []}
        _, lines = await roundtrip(payload, h_stream_tool, stream=True)
        chunks = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
        args = ""
        for c in chunks:
            if not c.get("choices"):
                continue
            for tc in c["choices"][0]["delta"].get("tool_calls", []) or []:
                args += (tc.get("function") or {}).get("arguments", "")
        assert json.loads(args)["city"] == "太原", args
        ok("流式工具调用增量", args)

        # 8. 客户端断开 -> A 收到 client_gone（直接验证网关语义）
        async def h_gone(req):
            bridge.mark_client_gone(req.request_id)
            r = await bridge.emit("agent-a", req.request_id, content="还在写")
            assert r.get("client_gone") is True, r
            r = await bridge.finalize("agent-a", req.request_id, content="写完了")
            assert r.get("client_gone") is True, r
            assert bridge.status(req.request_id)["state"] in ("cancelled", "done"), bridge.status(req.request_id)

        payload = {"model": "agent-a", "messages": [{"role": "user", "content": "写点东西"}], "stream": True}

        async def drain():
            async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
                async for _line in resp.aiter_lines():
                    pass

        task = asyncio.create_task(drain())
        req = await bridge.wait_request("agent-a", timeout_s=5)
        await h_gone(req)
        await asyncio.wait_for(task, timeout=10)
        ok("断连短路", "client_gone")

    await bridge.stop()
    print(f"\n全部通过：{len(PASSED)} 项")


if __name__ == "__main__":
    asyncio.run(main())
