"""Tk 桌面控制台冒烟测试：不进 mainloop，用 root.update() 手动驱动。"""

from __future__ import annotations

import json
import time
import urllib.request

from model_mcp.config import load_config
from model_mcp.tk_ui import ConsoleApp

PORT = 8797
CHECKS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if not ok:
        raise AssertionError(f"{name} 失败 {detail}")
    CHECKS.append(name)


def pump(app: ConsoleApp, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.root.update()
        time.sleep(0.02)


def wait_for(app: ConsoleApp, predicate, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.root.update()
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def health() -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    cfg = load_config(None)
    cfg.server.port = PORT
    app = ConsoleApp(cfg, autostart=False)
    pump(app, 0.3)

    # 1 复制到剪贴板
    app.copy("hello-clip")
    check("clipboard_plain", app.root.clipboard_get() == "hello-clip")

    # 2 按钮复制内容（Base URL）
    app.copy(app.info["base_url"])
    check("clipboard_base_url", app.root.clipboard_get() == app.info["base_url"])

    # 3 只读代码块选中片段复制
    app.mcp_text.tag_add("sel", "1.0", "1.8")
    app.mcp_text.mark_set("insert", "1.0")
    app._copy_widget(app.mcp_text)
    check("clipboard_selection", app.root.clipboard_get() == app.mcp_text.get("1.0", "1.8"))
    app.mcp_text.tag_remove("sel", "1.0", "end")
    app.mcp_text.configure(state="disabled")

    # 4 只读文本可读、可整体复制
    full = app.mcp_text.get("1.0", "end-1c")
    app._copy_widget(app.mcp_text)
    check("clipboard_whole_block", app.root.clipboard_get() == full)

    # 5 stdio / http 切换
    app.mcp_mode.set("http")
    app._render_mcp()
    pump(app, 0.1)
    data = json.loads(app.mcp_text.get("1.0", "end-1c"))
    check("config_http", data["mcpServers"]["model-mcp"]["url"].endswith("/mcp"), str(data))
    app.mcp_mode.set("stdio")
    app._render_mcp()
    pump(app, 0.1)
    data = json.loads(app.mcp_text.get("1.0", "end-1c"))
    check("config_stdio", "command" in data["mcpServers"]["model-mcp"])
    check("config_stdio_utf8", "模型服务mcp" in data["mcpServers"]["model-mcp"]["cwd"])

    # 6 启动服务
    app.do_start()
    check("server_started", wait_for(app, app.server.running), "服务未起来")
    pump(app, 1.2)
    body = health()
    check("health_ok", body.get("status") == "ok", str(body))
    check("ui_state_running", app.status_label.cget("text") == "运行中", app.status_label.cget("text"))

    # 7 队列状态刷新
    app.refresh_status()
    pump(app, 2.6)
    workers = app.status_values["workers"].cget("text")
    check("queue_view", "worker" in workers, workers)

    # 8 停止服务
    app.do_stop()
    check("server_stopped", wait_for(app, lambda: not app.server.running()), "服务未停")
    try:
        health()
    except Exception:
        stopped = True
    else:
        stopped = False
    check("port_closed", stopped)
    check("ui_state_stopped", app.status_label.cget("text") == "已停止", app.status_label.cget("text"))

    app.root.destroy()
    print(f"Tk UI smoke: {len(CHECKS)}/{len(CHECKS)} OK -> " + ", ".join(CHECKS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
