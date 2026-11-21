"""一键启动：拉起网关 + 自动打开浏览器控制台。

用法：python -m model_mcp.launcher   （双击「启动模型服务.bat」等价）
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from .config import load_config
from .mcp_server import build_mcp_server
from .openai_api import create_app
from .queue import Bridge


def _listening(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def _open_when_ready(url: str, host: str, port: int, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.5) as resp:
                if resp.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.3)


def build_app(cfg):
    bridge = Bridge(cfg)
    mcp_server = build_mcp_server(bridge, cfg)
    mcp_app = mcp_server.streamable_http_app(streamable_http_path="/mcp", host=cfg.server.host)
    return create_app(bridge, cfg, mcp_server=mcp_server, mcp_app=mcp_app)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="model-mcp-launcher", description="启动 model-mcp 并打开本地控制台")
    parser.add_argument("--host", default=None, help="监听地址，默认取配置")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取配置")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器（调试用）")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port

    ui_url = f"http://127.0.0.1:{cfg.server.port}/"
    if _listening("127.0.0.1", cfg.server.port):
        print(f"[model-mcp] 端口 {cfg.server.port} 已在运行，直接打开控制台：{ui_url}")
        if not args.no_browser:
            webbrowser.open(ui_url)
        return 0

    app = build_app(cfg)
    if not args.no_browser:
        threading.Thread(
            target=_open_when_ready, args=(ui_url, "127.0.0.1", cfg.server.port), daemon=True
        ).start()

    print("=" * 58)
    print("  model-mcp 已启动")
    print(f"  控制台   {ui_url}")
    print(f"  模型接口 http://127.0.0.1:{cfg.server.port}/v1")
    print(f"  MCP      http://127.0.0.1:{cfg.server.port}/mcp")
    print("  按 Ctrl+C 停止服务")
    print("=" * 58)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=cfg.server.host,
            port=cfg.server.port,
            log_config=None,
            access_log=False,
            lifespan="on",
        )
    )
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        print("\n[model-mcp] 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
