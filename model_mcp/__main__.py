"""入口：python -m model_mcp --transport stdio|http"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

import uvicorn

from .config import load_config
from .mcp_server import build_mcp_server
from .openai_api import create_app
from .queue import Bridge

logger = logging.getLogger("model_mcp")


def _configure_logging(level: str) -> None:
    # stdio 模式下 stdout 归 MCP 报文独占，所有日志必须走 stderr
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stderr,
        force=True,
    )


def _uvicorn_config(app, cfg) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )


async def _run_stdio(app, mcp_server, cfg) -> None:
    server = uvicorn.Server(_uvicorn_config(app, cfg))
    http_task = asyncio.create_task(server.serve(), name="model-mcp-http")
    print(
        f"[model-mcp] stdio 模式就绪；OpenAI 兼容侧 http://{cfg.server.host}:{cfg.server.port}/v1 ，"
        f"MCP(HTTP) http://{cfg.server.host}:{cfg.server.port}/mcp",
        file=sys.stderr,
    )
    try:
        await mcp_server.run_stdio_async()
    finally:
        server.should_exit = True
        await asyncio.sleep(0.2)
        http_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await http_task


async def _run_http(app, cfg) -> None:
    server = uvicorn.Server(_uvicorn_config(app, cfg))
    print(
        f"[model-mcp] HTTP 模式就绪；OpenAI 兼容侧 http://{cfg.server.host}:{cfg.server.port}/v1 ，"
        f"MCP(HTTP) http://{cfg.server.host}:{cfg.server.port}/mcp",
        file=sys.stderr,
    )
    await server.serve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="model-mcp", description="智能体充当模型后端的 MCP 网关")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio", help="MCP 传输方式，默认 stdio（同时会拉起 HTTP 侧）")
    parser.add_argument("--host", default=None, help="监听地址，默认取配置")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取配置")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 ./config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    _configure_logging(args.log_level)
    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port

    bridge = Bridge(cfg)
    mcp_server = build_mcp_server(bridge, cfg)
    mcp_app = mcp_server.streamable_http_app(streamable_http_path="/mcp", host=cfg.server.host)
    app = create_app(bridge, cfg, mcp_server=mcp_server, mcp_app=mcp_app)

    try:
        if args.transport == "http":
            asyncio.run(_run_http(app, cfg))
        else:
            asyncio.run(_run_stdio(app, mcp_server, cfg))
    except KeyboardInterrupt:
        print("[model-mcp] 收到中断，退出", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
