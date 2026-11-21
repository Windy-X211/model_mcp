"""模拟智能体 A：走 MCP over HTTP 接活并把回复交回网关。

用法：先启动 `python -m model_mcp --transport http --port 8765`，再运行本脚本。
"""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def text_of(result) -> dict:
    return json.loads(result.content[0].text)


def last_user_text(messages: list) -> str:
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--worker", default="agent-a")
    parser.add_argument("--idle-rounds", type=int, default=3, help="连续多少次 idle 后退出（demo 用）")
    args = parser.parse_args()

    async with streamable_http_client(args.url) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(text_of(await session.call_tool("service_ready", {"worker_id": args.worker})))
            print("[A] 已上岗，开始等待请求…")

            idle = 0
            while idle < args.idle_rounds:
                result = text_of(await session.call_tool("service_wait", {"worker_id": args.worker, "timeout_s": 15}))
                if result.get("status") != "request":
                    idle += 1
                    print(f"[A] 暂无请求（{idle}/{args.idle_rounds}）")
                    continue

                idle = 0
                rid = result["request_id"]
                prompt = last_user_text(result.get("messages"))
                tools = [t.get("function", {}).get("name") for t in result.get("tools") or []]
                print(f"[A] 领到 {rid}：{prompt!r}  tools={tools}  stream={result.get('stream')}")

                await session.call_tool("stream_emit", {"request_id": rid, "reasoning": "先理解一下问题"})

                reply = f"【模拟 A】收到：{prompt}"
                for end in range(4, len(reply), 4):
                    await session.call_tool("stream_emit", {"request_id": rid, "content": reply[:end]})

                done = text_of(
                    await session.call_tool(
                        "submit_result",
                        {
                            "request_id": rid,
                            "content": reply,
                            "finish_reason": "stop",
                            "usage": {"prompt_tokens": 10, "completion_tokens": len(reply), "total_tokens": 10 + len(reply)},
                        },
                    )
                )
                print(f"[A] 已交付 {rid}：{done}")

            await session.call_tool("service_offline", {"worker_id": args.worker})
            print("[A] 已下线")


if __name__ == "__main__":
    asyncio.run(main())
