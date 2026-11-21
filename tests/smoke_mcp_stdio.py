"""端到端自测：真起子进程，A 走 stdio MCP，软件走 HTTP，验证全链路。

运行：python tests/smoke_mcp_stdio.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PORT = 8790
BASE = f"http://127.0.0.1:{PORT}"


async def wait_http(http: httpx.AsyncClient, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = await http.get(f"{BASE}/health")
            if response.status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("HTTP 侧没有起来")


def tool_json(result) -> dict:
    return json.loads(result.content[0].text)


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "model_mcp", "--transport", "stdio", "--port", str(PORT)],
        env={"PYTHONPATH": str(ROOT), "MODEL_MCP_CONFIG": str(ROOT / "config.yaml")},
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            assert {"service_wait", "stream_emit", "submit_result", "service_stats"} <= set(tools), tools
            print("[PASS] stdio 握手成功，工具已注册：", ", ".join(tools))

            async with httpx.AsyncClient(timeout=90) as http:
                await wait_http(http)
                print("[PASS] stdio 模式下 HTTP 侧就绪（stdout 未被日志污染）")

                ready = tool_json(await session.call_tool("service_ready", {"worker_id": "agent-a"}))
                assert ready["ok"] is True, ready
                stats = tool_json(await session.call_tool("service_stats", {}))
                assert stats["workers"] == ["agent-a"], stats
                print("[PASS] service_ready / service_stats 正常")

                lines: list[str] = []

                async def consume() -> None:
                    payload = {
                        "model": "agent-a",
                        "stream": True,
                        "messages": [
                            {"role": "system", "content": "你是模型服务后端"},
                            {"role": "user", "content": "用一句话介绍你自己"},
                        ],
                        "temperature": 0.3,
                    }
                    async with http.stream("POST", f"{BASE}/v1/chat/completions", json=payload) as response:
                        assert response.status_code == 200, await response.aread()
                        async for line in response.aiter_lines():
                            if line.strip():
                                lines.append(line)

                task = asyncio.create_task(consume())

                wait = tool_json(await session.call_tool("service_wait", {"worker_id": "agent-a", "timeout_s": 20}))
                assert wait["status"] == "request", wait
                rid = wait["request_id"]
                assert wait["messages"][-1]["content"] == "用一句话介绍你自己", wait
                print("[PASS] service_wait 领到请求：", rid)

                await session.call_tool("stream_emit", {"request_id": rid, "reasoning": "先判断对方意图"})
                await session.call_tool("stream_emit", {"request_id": rid, "content": "我是"})
                await session.call_tool("stream_emit", {"request_id": rid, "content": "我是 agent-a，"})
                result = tool_json(
                    await session.call_tool(
                        "submit_result",
                        {
                            "request_id": rid,
                            "content": "我是 agent-a，负责把模型的回复交回网关。",
                            "finish_reason": "stop",
                            "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32},
                        },
                    )
                )
                assert result["ok"] is True, result
                print("[PASS] stream_emit / submit_result 正常")

                await asyncio.wait_for(task, timeout=30)
                raw = "".join(lines)
                assert "[DONE]" in raw, raw
                chunks = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
                content = "".join(
                    c["choices"][0]["delta"].get("content", "")
                    for c in chunks
                    if c.get("choices") and c["choices"][0].get("delta")
                )
                reasoning = "".join(
                    c["choices"][0]["delta"].get("reasoning_content", "")
                    for c in chunks
                    if c.get("choices") and c["choices"][0].get("delta")
                )
                assert content == "我是 agent-a，负责把模型的回复交回网关。", content
                assert reasoning == "先判断对方意图", reasoning
                print(f"[PASS] 软件侧收到完整 SSE：content={content!r} reasoning={reasoning!r}")

                # 非流式 + 工具调用
                async def consume_blocking() -> httpx.Response:
                    payload = {
                        "model": "agent-a",
                        "messages": [{"role": "user", "content": "太原今天多少度"}],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "description": "查询天气",
                                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                                },
                            }
                        ],
                    }
                    return await http.post(f"{BASE}/v1/chat/completions", json=payload)

                task2 = asyncio.create_task(consume_blocking())
                wait2 = tool_json(await session.call_tool("service_wait", {"worker_id": "agent-a", "timeout_s": 20}))
                assert wait2["status"] == "request", wait2
                assert wait2["tools"] and wait2["tools"][0]["function"]["name"] == "get_weather", wait2
                rid2 = wait2["request_id"]
                await session.call_tool(
                    "submit_result",
                    {
                        "request_id": rid2,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": json.dumps({"city": "太原"})},
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                )
                response = await asyncio.wait_for(task2, timeout=30)
                body = response.json()
                choice = body["choices"][0]
                assert choice["finish_reason"] == "tool_calls", body
                assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"])["city"] == "太原", body
                print("[PASS] 非流式工具调用透传（tools 已透传给 A）")

                # 一次性全文 + 回放
                async def consume_replay() -> None:
                    payload = {"model": "agent-a", "stream": True, "messages": [{"role": "user", "content": "写一句长点的话"}]}
                    async with http.stream("POST", f"{BASE}/v1/chat/completions", json=payload) as response:
                        async for line in response.aiter_lines():
                            if line.strip():
                                lines.append(line)

                lines.clear()
                task3 = asyncio.create_task(consume_replay())
                wait3 = tool_json(await session.call_tool("service_wait", {"worker_id": "agent-a", "timeout_s": 20}))
                await session.call_tool(
                    "submit_result",
                    {"request_id": wait3["request_id"], "content": "这是一次性提交的全文，网关负责把它切成流式小片推给调用方。"},
                )
                await asyncio.wait_for(task3, timeout=30)
                chunks = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
                pieces = [
                    c["choices"][0]["delta"].get("content", "")
                    for c in chunks
                    if c.get("choices") and c["choices"][0].get("delta")
                ]
                assert "".join(pieces) == "这是一次性提交的全文，网关负责把它切成流式小片推给调用方。", pieces
                assert len([p for p in pieces if p]) > 1, pieces
                print(f"[PASS] 一次性提交由网关回放成流式（{len([p for p in pieces if p])} 片）")

                offline = tool_json(await session.call_tool("service_offline", {"worker_id": "agent-a"}))
                assert offline["removed"] is True, offline
                print("[PASS] service_offline 正常")


if __name__ == "__main__":
    asyncio.run(main())
    print("\n端到端全部通过")
