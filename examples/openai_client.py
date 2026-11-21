"""模拟外部软件：用 OpenAI SDK / httpx 调用网关的 /v1/chat/completions。

用法：
  先启动服务（stdio 或 http 模式均可，HTTP 侧一直在 8765）
  python examples/openai_client.py "帮我介绍一下你自己" --stream
  python examples/openai_client.py "太原天气" --sdk      # 走官方 openai SDK
"""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx


async def with_httpx(base: str, prompt: str, stream: bool) -> None:
    payload = {
        "model": "agent-a",
        "stream": stream,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        if not stream:
            response = await client.post(f"{base}/v1/chat/completions", json=payload)
            print(f"HTTP {response.status_code}")
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
            return

        async with client.stream("POST", f"{base}/v1/chat/completions", json=payload) as response:
            print(f"HTTP {response.status_code}（流式）")
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    print("\n[DONE]")
                    break
                chunk = json.loads(data)
                if not chunk.get("choices"):
                    continue
                delta = chunk["choices"][0].get("delta") or {}
                if delta.get("reasoning_content"):
                    print(f"\033[90m{delta['reasoning_content']}\033[0m", end="", flush=True)
                if delta.get("content"):
                    print(delta["content"], end="", flush=True)


def with_sdk(base: str, prompt: str) -> None:
    from openai import OpenAI

    client = OpenAI(base_url=f"{base}/v1", api_key="sk-local-dummy")
    stream = client.chat.completions.create(
        model="agent-a",
        messages=[{"role": "user", "content": prompt}],
        stream=True,
    )
    for chunk in stream:
        if not chunk.choices:  # usage 统计块，choices 为空
            continue
        delta = chunk.choices[0].delta
        if getattr(delta, "reasoning_content", None):
            print(f"[思考] {delta.reasoning_content}", end="", flush=True)
        if delta.content:
            print(delta.content, end="", flush=True)
    print()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?", default="你好，请介绍一下你自己")
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--sdk", action="store_true", help="改用官方 openai SDK 调用")
    args = parser.parse_args()

    if args.sdk:
        with_sdk(args.base, args.prompt)
    else:
        await with_httpx(args.base, args.prompt, args.stream)


if __name__ == "__main__":
    asyncio.run(main())
