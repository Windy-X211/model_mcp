"""HTTP 模式自测：MCP over streamable-http（A 连 /mcp）+ OpenAI 兼容接口。

运行：python tests/smoke_mcp_http.py
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
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"


async def wait_up(http: httpx.AsyncClient, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (await http.get(f"{BASE}/health")).status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("服务没起来")


def tool_json(result) -> dict:
    return json.loads(result.content[0].text)


async def main() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "model_mcp",
        "--transport",
        "http",
        "--port",
        str(PORT),
        cwd=str(ROOT),
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": str(ROOT)},
    )
    try:
        async with httpx.AsyncClient(timeout=90) as http:
            await wait_up(http)
            print("[PASS] HTTP 模式服务已启动")

            async with streamable_http_client(f"{BASE}/mcp") as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = [t.name for t in (await session.list_tools()).tools]
                    assert "service_wait" in tools, tools
                    print("[PASS] MCP over HTTP 握手成功，工具数：", len(tools))

                    assert tool_json(await session.call_tool("service_ready", {"worker_id": "agent-a"}))["ok"] is True

                    lines: list[str] = []

                    async def consume() -> None:
                        payload = {"model": "agent-a", "stream": True, "messages": [{"role": "user", "content": "1+1=?"}]}
                        async with http.stream("POST", f"{BASE}/v1/chat/completions", json=payload) as response:
                            assert response.status_code == 200, await response.aread()
                            async for line in response.aiter_lines():
                                if line.strip():
                                    lines.append(line)

                    task = asyncio.create_task(consume())
                    wait = tool_json(await session.call_tool("service_wait", {"worker_id": "agent-a", "timeout_s": 20}))
                    assert wait["status"] == "request", wait
                    rid = wait["request_id"]
                    await session.call_tool("stream_emit", {"request_id": rid, "content": "2"})
                    await session.call_tool("submit_result", {"request_id": rid, "content": "2。"})
                    await asyncio.wait_for(task, timeout=30)

                    chunks = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
                    content = "".join(
                        c["choices"][0]["delta"].get("content", "")
                        for c in chunks
                        if c.get("choices") and c["choices"][0].get("delta")
                    )
                    assert content == "2。", content
                    assert "[DONE]" in "".join(lines)
                    print("[PASS] HTTP 模式下全链路完成：content=", content)

            models = (await http.get(f"{BASE}/v1/models")).json()
            assert models["data"][0]["x_online"] is True, models
            print("[PASS] /v1/models 正常")
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
    print("\nHTTP 模式全部通过")
