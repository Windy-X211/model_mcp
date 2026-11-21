"""轻量 Tkinter 控制台：一键启停网关，MCP 配置与接口信息可直接复制。

用法：python -m model_mcp.tk_ui   （或双击「启动模型服务.bat」）
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import queue as stdqueue
import re
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import messagebox, ttk

import uvicorn

from .config import Config, load_config
from .mcp_server import build_mcp_server
from .openai_api import create_app
from .prompts import WORKER_PROMPT
from .queue import Bridge

APP_TITLE = "model-mcp 控制台"
VERSION = "1.0.0"

BG = "#f4f6f8"
CARD = "#ffffff"
LINE = "#e3e8ee"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#0f766e"
ACCENT_SOFT = "#e6f4f1"
WARN = "#b45309"
DANGER = "#b91c1c"
CODE_BG = "#0f172a"
CODE_FG = "#e2e8f0"

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 16, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_CODE = ("Consolas", 10)


# --------------------------------------------------------------------------- 信息
class ServerController:
    """在后台线程里跑 uvicorn，Tk 主线程只通过 HTTP 访问它。"""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.server: uvicorn.Server | None = None
        self.error: str | None = None
        self.stop_requested = False

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, cfg: Config) -> tuple[bool, str]:
        if self.running():
            return False, "服务已在运行"
        self.error = None
        self.stop_requested = False
        self.server = None
        self.thread = threading.Thread(target=self._run, args=(cfg,), name="model-mcp-server", daemon=True)
        self.thread.start()

        host = "127.0.0.1"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.error:
                return False, self.error
            if not self.thread.is_alive():
                return False, "服务线程已退出"
            try:
                with urllib.request.urlopen(f"http://{host}:{cfg.server.port}/health", timeout=1.5) as resp:
                    if resp.status == 200:
                        return True, ""
            except Exception:
                time.sleep(0.25)
        return False, f"启动超时（端口 {cfg.server.port} 无响应）"

    def _run(self, cfg: Config) -> None:
        try:
            bridge = Bridge(cfg)
            mcp_server = build_mcp_server(bridge, cfg)
            mcp_app = mcp_server.streamable_http_app(streamable_http_path="/mcp", host=cfg.server.host)
            app = create_app(bridge, cfg, mcp_server=mcp_server, mcp_app=mcp_app)
            self.server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=cfg.server.host,
                    port=cfg.server.port,
                    log_config=None,
                    access_log=False,
                    lifespan="on",
                )
            )
            if self.stop_requested:
                self.server.should_exit = True
            asyncio.run(self.server.serve())
        except OSError as exc:
            self.error = f"端口 {cfg.server.port} 启动失败：{exc}"
            logging.getLogger("model_mcp").error(self.error)
        except Exception as exc:  # pragma: no cover
            self.error = f"{type(exc).__name__}: {exc}"
            logging.getLogger("model_mcp").exception("服务启动异常")

    def stop(self) -> None:
        self.stop_requested = True
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=8)
            if self.thread.is_alive():
                self.server.should_exit = True  # 再催一次


def build_info(cfg: Config) -> dict:
    root = Path(__file__).resolve().parent.parent
    exposed = "127.0.0.1" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
    base = f"http://{exposed}:{cfg.server.port}"
    return {
        "python_exe": str(Path(sys.executable).resolve()),
        "project_root": str(root),
        "config_path": str(root / "config.yaml"),
        "host": exposed,
        "port": cfg.server.port,
        "server_url": base,
        "base_url": f"{base}/v1",
        "mcp_url": f"{base}/mcp",
        "auth_required": bool(cfg.server.auth_token),
        "api_key": cfg.server.auth_token or "sk-local-dummy",
        "models": [m.name for m in cfg.models],
        "wait_timeout_s": cfg.queue.wait_timeout_s,
    }


def _port_serving(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """端口上是否已经有一个 model-mcp 在跑。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as resp:
            return resp.status == 200 and b'"status":"ok"' in resp.read()
    except Exception:
        return False


def stdio_config_text(info: dict) -> str:
    payload = {
        "mcpServers": {
            "model-mcp": {
                "command": info["python_exe"],
                "args": ["-m", "model_mcp", "--transport", "stdio", "--port", str(info["port"])],
                "cwd": info["project_root"],
                "env": {
                    "PYTHONPATH": info["project_root"],
                    "MODEL_MCP_CONFIG": info["config_path"],
                },
            }
        }
    }
    return _inline_args(json.dumps(payload, ensure_ascii=False, indent=2), payload["mcpServers"]["model-mcp"]["args"])


def _inline_args(text: str, args: list[str]) -> str:
    """把展开成多行的 args 数组折回一行，JSON 更紧凑。"""
    return re.sub(
        r'"args": \[\n.*?\n\s*\]',
        '"args": ' + json.dumps(args, ensure_ascii=False),
        text,
        count=1,
        flags=re.S,
    )


def http_config_text(info: dict) -> str:
    return json.dumps({"mcpServers": {"model-mcp": {"url": info["mcp_url"]}}}, ensure_ascii=False, indent=2)


def sdk_text(info: dict) -> str:
    model = info["models"][0] if info["models"] else "agent-a"
    return f'''from openai import OpenAI

client = OpenAI(base_url="{info["base_url"]}", api_key="{info["api_key"]}")
stream = client.chat.completions.create(
    model="{model}",
    messages=[{{"role": "user", "content": "你好，介绍一下你自己"}}],
    stream=True,
)
for chunk in stream:
    if not chunk.choices:
        continue
    delta = chunk.choices[0].delta
    if getattr(delta, "reasoning_content", None):
        print(delta.reasoning_content, end="", flush=True)   # 思考
    if delta.content:
        print(delta.content, end="", flush=True)             # 正文'''


def curl_text(info: dict) -> str:
    model = info["models"][0] if info["models"] else "agent-a"
    return (
        f'curl.exe {info["base_url"]}/chat/completions -H "Content-Type: application/json" '
        f'-d \'{{"model":"{model}","stream":true,"messages":[{{"role":"user","content":"你好"}}]}}\''
    )


# --------------------------------------------------------------------------- 日志
class QueueLogHandler(logging.Handler):
    def __init__(self, sink: "stdqueue.Queue[str]") -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put_nowait(self.format(record))
        except Exception:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- 界面
class ConsoleApp:
    def __init__(self, cfg: Config, autostart: bool = True) -> None:
        self.cfg = cfg
        self.info = build_info(cfg)
        self.server = ServerController()
        self.logs: "stdqueue.Queue[str]" = stdqueue.Queue()
        self._ui_queue: "stdqueue.Queue[Callable[[], None]]" = stdqueue.Queue()
        self.busy = False
        self._polling = False

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("900x800")
        self.root.minsize(780, 620)
        self.root.configure(bg=BG)
        self._init_style()

        self._build_header()
        self._build_notebook()
        self._build_footer()

        self._install_logging()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(80, self._pump_ui)
        self.root.after(120, self._show_window)
        self.root.after(300, lambda: self._drain_logs())
        self.root.after(600, self.refresh_status)
        if autostart:
            self.root.after(120, self.do_start)

    # ---------------------------------------------------------------- 主线程调度
    def _show_window(self) -> None:
        """pythonw / start 启动时窗口可能落成最小化，这里强制还原并置前。"""
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except tk.TclError:
            pass

    def _post(self, fn: Callable[[], None]) -> None:
        """后台线程只把回调丢进队列，不直接碰 Tk。"""
        self._ui_queue.put(fn)

    def _pump_ui(self) -> None:
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except stdqueue.Empty:
                break
            try:
                fn()
            except Exception:
                logging.getLogger("model_mcp").exception("界面回调异常")
        self.root.after(80, self._pump_ui)

    # ---------------------------------------------------------------- 样式
    def _init_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT, font=FONT_UI)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 7), font=FONT_UI)
        style.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)])
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Accent.TButton", background=ACCENT, foreground="white", font=FONT_UI, padding=(12, 5))
        style.map("Accent.TButton", background=[("active", "#0b5f59"), ("disabled", "#cbd5e1")])
        style.configure("TButton", font=FONT_UI, padding=(10, 4))
        style.configure("Small.TLabel", background=BG, foreground=MUTED, font=FONT_SMALL)
        style.configure("Strong.TLabel", background=CARD, foreground=TEXT, font=("Microsoft YaHei UI", 10, "bold"))
        for name in ("Code.Vertical.TScrollbar", "Code.Horizontal.TScrollbar"):
            style.configure(
                name,
                background="#1f2937",
                troughcolor="#0f172a",
                bordercolor="#0f172a",
                arrowcolor="#94a3b8",
                darkcolor="#1f2937",
                lightcolor="#1f2937",
                relief="flat",
                arrowsize=12,
            )

    # ---------------------------------------------------------------- 头部
    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        header.pack(fill="x", side="top")

        inner = tk.Frame(header, bg=CARD)
        inner.pack(fill="x", padx=18, pady=12)

        title_box = tk.Frame(inner, bg=CARD)
        title_box.pack(side="left")
        tk.Label(title_box, text="model-mcp", bg=CARD, fg=TEXT, font=FONT_TITLE).pack(anchor="w")
        tk.Label(
            title_box,
            text="智能体充当模型后端 · OpenAI 兼容网关",
            bg=CARD,
            fg=MUTED,
            font=FONT_SMALL,
        ).pack(anchor="w")

        status_box = tk.Frame(inner, bg=CARD)
        status_box.pack(side="right")
        self.dot = tk.Label(status_box, text="●", bg=CARD, fg=DANGER, font=("Segoe UI", 13))
        self.dot.pack(side="left", padx=(0, 6))
        self.status_label = tk.Label(status_box, text="已停止", bg=CARD, fg=TEXT, font=FONT_UI)
        self.status_label.pack(side="left")

        controls = tk.Frame(header, bg=CARD)
        controls.pack(fill="x", padx=18, pady=(0, 12))

        tk.Label(controls, text="端口", bg=CARD, fg=MUTED, font=FONT_SMALL).pack(side="left")
        self.port_var = tk.StringVar(value=str(self.cfg.server.port))
        self.port_entry = ttk.Entry(controls, textvariable=self.port_var, width=7, font=FONT_UI)
        self.port_entry.pack(side="left", padx=(6, 12))

        self.btn_start = ttk.Button(controls, text="启动服务", style="Accent.TButton", command=self.do_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(controls, text="停止", command=self.do_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_browser = ttk.Button(controls, text="浏览器版", command=self.open_browser)
        self.btn_browser.pack(side="left", padx=6)

        self.meta_label = tk.Label(
            controls, text=f"模型 {', '.join(self.info['models'])}", bg=CARD, fg=MUTED, font=FONT_SMALL
        )
        self.meta_label.pack(side="right")

    # ---------------------------------------------------------------- 内容
    def _build_notebook(self) -> None:
        holder = tk.Frame(self.root, bg=BG)
        holder.pack(fill="both", expand=True, padx=14, pady=12)

        self.nb = ttk.Notebook(holder)
        self.nb.pack(fill="both", expand=True)

        self._build_mcp_tab()
        self._build_api_tab()
        self._build_prompt_tab()
        self._build_status_tab()

    def _tab(self, title: str) -> tk.Frame:
        frame = tk.Frame(self.nb, bg=CARD)
        self.nb.add(frame, text=title)
        return frame

    def _make_code(self, parent: tk.Frame, height: int, wrap: str = "none") -> tuple[tk.Frame, tk.Text]:
        wrap_frame = tk.Frame(parent, bg=CODE_BG, highlightbackground="#0b1220", highlightthickness=1)
        text = tk.Text(
            wrap_frame,
            height=height,
            bg=CODE_BG,
            fg=CODE_FG,
            insertbackground=CODE_FG,
            relief="flat",
            font=FONT_CODE,
            wrap=wrap,
            padx=12,
            pady=10,
            selectbackground="#1d4ed8",
            state="disabled",
        )
        ys = ttk.Scrollbar(wrap_frame, orient="vertical", command=text.yview, style="Code.Vertical.TScrollbar")
        xs = ttk.Scrollbar(wrap_frame, orient="horizontal", command=text.xview, style="Code.Horizontal.TScrollbar")
        text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        wrap_frame.rowconfigure(0, weight=1)
        wrap_frame.columnconfigure(0, weight=1)
        text.bind("<Control-c>", lambda event: (self._copy_widget(text), "break")[1])
        return wrap_frame, text

    def _row_toolbar(self, parent: tk.Frame, label: str, target: Callable[[], str]) -> tk.Frame:
        bar = tk.Frame(parent, bg=CARD)
        bar.pack(fill="x", pady=(4, 4))
        tk.Label(bar, text=label, bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        ttk.Button(bar, text="复制", command=lambda: self.copy(target())).pack(side="right")
        return bar

    def _build_mcp_tab(self) -> None:
        tab = self._tab("  1 · MCP 配置  ")
        body = tk.Frame(tab, bg=CARD)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 10))

        tk.Label(
            body,
            text="把下面的 JSON 粘进智能体软件的 MCP 设置，然后在 A 里发送第 3 页的提示词。",
            bg=CARD,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(0, 6))

        switch = tk.Frame(body, bg=CARD)
        switch.pack(fill="x", pady=(0, 6))
        self.mcp_mode = tk.StringVar(value="stdio")
        ttk.Radiobutton(
            switch, text="stdio（桌面智能体）", value="stdio", variable=self.mcp_mode, command=self._render_mcp
        ).pack(side="left")
        ttk.Radiobutton(
            switch, text="HTTP（远程 / 多客户端）", value="http", variable=self.mcp_mode, command=self._render_mcp
        ).pack(side="left", padx=12)

        self._row_toolbar(body, "配置内容", lambda: self.mcp_text.get("1.0", "end-1c"))
        frame, self.mcp_text = self._make_code(body, height=14)
        frame.pack(fill="both", expand=True)

        url_bar = tk.Frame(body, bg=CARD)
        url_bar.pack(fill="x", pady=(10, 0))
        tk.Label(url_bar, text="MCP 端点", bg=CARD, fg=MUTED, font=FONT_SMALL).pack(side="left")
        tk.Label(url_bar, text=self.info["mcp_url"], bg=CARD, fg=ACCENT, font=FONT_CODE).pack(side="left", padx=10)
        ttk.Button(url_bar, text="复制地址", command=lambda: self.copy(self.info["mcp_url"])).pack(side="right")

        tk.Label(
            body,
            text="Trae / Cline 放在 mcp.json；Claude Desktop 放在 %APPDATA%\\Claude\\claude_desktop_config.json。改完重启智能体软件。",
            bg=CARD,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=780,
        ).pack(fill="x", pady=(8, 0))

        self._render_mcp()

    def _render_mcp(self) -> None:
        content = stdio_config_text(self.info) if self.mcp_mode.get() == "stdio" else http_config_text(self.info)
        self._set_text(self.mcp_text, content)

    def _build_api_tab(self) -> None:
        tab = self._tab("  2 · 模型接口  ")
        body = tk.Frame(tab, bg=CARD)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 10))

        kv = tk.Frame(body, bg=CARD)
        kv.pack(fill="x")
        rows = [
            ("Base URL", self.info["base_url"]),
            ("API Key", self.info["api_key"] if self.info["auth_required"] else "sk-local-dummy（任意值，当前未开鉴权）"),
            ("模型名", ", ".join(self.info["models"])),
        ]
        for index, (key, value) in enumerate(rows):
            tk.Label(kv, text=key, bg=CARD, fg=MUTED, font=FONT_SMALL, width=9, anchor="w").grid(
                row=index, column=0, sticky="w", pady=1
            )
            tk.Label(kv, text=value, bg=CARD, fg=TEXT, font=FONT_CODE, anchor="w").grid(
                row=index, column=1, sticky="w", padx=6, pady=1
            )
        ttk.Button(kv, text="复制 Base URL", command=lambda: self.copy(self.info["base_url"])).grid(
            row=0, column=2, rowspan=3, padx=(16, 0)
        )

        self._row_toolbar(body, "openai SDK 流式调用", lambda: self.sdk_text.get("1.0", "end-1c"))
        frame, self.sdk_text = self._make_code(body, height=15)
        frame.pack(fill="both", expand=True)

        self._row_toolbar(body, "curl（PowerShell 一行版）", lambda: self.curl_text.get("1.0", "end-1c"))
        frame2, self.curl_text = self._make_code(body, height=3, wrap="word")
        frame2.pack(fill="x")

        tk.Label(
            body,
            text="端点：GET /v1/models · POST /v1/chat/completions（支持 stream / tools / reasoning_content）· GET /health · POST /mcp",
            bg=CARD,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=780,
        ).pack(fill="x", pady=(10, 0))

        self._set_text(self.sdk_text, sdk_text(self.info))
        self._set_text(self.curl_text, curl_text(self.info))

    def _build_prompt_tab(self) -> None:
        tab = self._tab("  3 · 接活提示词  ")
        body = tk.Frame(tab, bg=CARD)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 10))

        tk.Label(
            body,
            text="整段复制给智能体 A（或写成它的系统提示词），它会循环领活 → 生成 → 回填，不会自己结束。",
            bg=CARD,
            fg=MUTED,
            font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        self._row_toolbar(body, "worker 提示词", lambda: self.prompt_text.get("1.0", "end-1c"))
        frame, self.prompt_text = self._make_code(body, height=20, wrap="word")
        frame.pack(fill="both", expand=True)
        self._set_text(self.prompt_text, WORKER_PROMPT)

    def _build_status_tab(self) -> None:
        tab = self._tab("  4 · 状态与日志  ")
        body = tk.Frame(tab, bg=CARD)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 10))

        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x")
        self.status_values: dict[str, tk.Label] = {}
        fields = [("服务地址", "server"), ("在线 worker", "workers"), ("队列", "queue"), ("等待超时", "timeout")]
        for row, (key, name) in enumerate(fields):
            tk.Label(grid, text=key, bg=CARD, fg=MUTED, font=FONT_SMALL, width=10, anchor="w").grid(
                row=row, column=0, sticky="w"
            )
            value = tk.Label(grid, text="-", bg=CARD, fg=TEXT, font=FONT_UI, anchor="w")
            value.grid(row=row, column=1, sticky="w", padx=6)
            self.status_values[name] = value

        bar = tk.Frame(body, bg=CARD)
        bar.pack(fill="x", pady=(10, 4))
        tk.Label(bar, text="运行日志", bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        ttk.Button(bar, text="清空", command=self._clear_logs).pack(side="right")

        frame, self.log_text = self._make_code(body, height=14, wrap="word")
        frame.pack(fill="both", expand=True)

    def _build_footer(self) -> None:
        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", side="bottom", padx=18, pady=(0, 10))
        self.hint_label = tk.Label(footer, text="", bg=BG, fg=ACCENT, font=FONT_SMALL, anchor="w")
        self.hint_label.pack(side="left")
        tk.Label(
            footer,
            text=f"v{VERSION} · 配置 {Path(self.info['config_path']).name}",
            bg=BG,
            fg=MUTED,
            font=FONT_SMALL,
        ).pack(side="right")

    # ---------------------------------------------------------------- 工具
    def _set_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _copy_widget(self, widget: tk.Text) -> None:
        try:
            selected = widget.get("sel.first", "sel.last")
        except tk.TclError:
            selected = widget.get("1.0", "end-1c")
        self.copy(selected)

    def copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_hint("已复制到剪贴板")

    def set_hint(self, message: str, ms: int = 2500) -> None:
        self.hint_label.configure(text=message)
        self.root.after(ms, lambda: self.hint_label.configure(text=""))

    # ---------------------------------------------------------------- 启停
    def do_start(self) -> None:
        if self.busy or self.server.running():
            return
        try:
            port = int(self.port_var.get())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showwarning(APP_TITLE, "端口必须是 1~65535 的整数")
            return

        self.cfg.server.port = port
        self.info = build_info(self.cfg)
        self._refresh_static_info()
        if _port_serving(port):
            self.set_hint(f"端口 {port} 上已有服务在运行，可直接使用", 8000)
            self._set_state("已在运行（外部实例）", WARN)
            messagebox.showinfo(
                APP_TITLE,
                f"端口 {port} 上已经有一个 model-mcp 服务在运行。\n"
                "直接按第 1/2 页的配置使用即可；要重新启动，请先关掉那个实例，或换个端口。",
            )
            return
        self.busy = True
        self._set_buttons(start=False, stop=False)
        self._set_state("启动中…", WARN)
        self.set_hint(f"正在启动 127.0.0.1:{port} …", 60000)
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self) -> None:
        ok, error = self.server.start(self.cfg)
        self._post(lambda: self._on_started(ok, error))

    def _on_started(self, ok: bool, error: str) -> None:
        self.busy = False
        if ok:
            self._set_buttons(start=False, stop=True)
            self.port_entry.configure(state="disabled")
            self._set_state("运行中", ACCENT)
            self.set_hint(f"服务已启动：{self.info['server_url']}")
            logging.getLogger("model_mcp").info("服务已在 127.0.0.1:%s 启动", self.info["port"])
        else:
            self._set_buttons(start=True, stop=False)
            self._set_state("已停止", DANGER)
            friendly = error
            if any(token in error for token in ("10048", "already in use", "Only one usage")):
                friendly = (
                    f"端口 {self.info['port']} 已被占用：可能已有一个窗口在运行，\n"
                    "或被别的程序占用了该端口。请换个端口再试。"
                )
            self.set_hint(friendly)
            messagebox.showerror(APP_TITLE, friendly)

    def do_stop(self) -> None:
        if self.busy or not self.server.running():
            return
        self.busy = True
        self._set_buttons(start=False, stop=False)
        self._set_state("停止中…", WARN)
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self) -> None:
        self.server.stop()
        self._post(self._on_stopped)

    def _on_stopped(self) -> None:
        self.busy = False
        self._set_buttons(start=True, stop=False)
        self.port_entry.configure(state="normal")
        self._set_state("已停止", DANGER)
        self.set_hint("服务已停止")

    def _set_buttons(self, start: bool, stop: bool) -> None:
        self.btn_start.configure(state="normal" if start else "disabled")
        self.btn_stop.configure(state="normal" if stop else "disabled")

    def _set_state(self, text: str, color: str) -> None:
        self.status_label.configure(text=text, fg=color)
        self.dot.configure(fg=color)

    def open_browser(self) -> None:
        webbrowser.open(f"http://127.0.0.1:{self.info['port']}/")
        self.set_hint("已用浏览器打开控制台")

    # ---------------------------------------------------------------- 刷新
    def _refresh_static_info(self) -> None:
        self._render_mcp()
        self._set_text(self.sdk_text, sdk_text(self.info))
        self._set_text(self.curl_text, curl_text(self.info))
        self.meta_label.configure(text=f"模型 {', '.join(self.info['models'])}")
        self.status_values["server"].configure(text=self.info["server_url"])
        self.status_values["timeout"].configure(text=f"service_wait 单次 {self.cfg.queue.wait_timeout_s}s")

    def refresh_status(self) -> None:
        if not self._polling and self.server.running():
            self._polling = True
            threading.Thread(target=self._fetch_status, daemon=True).start()
        self.root.after(2000, self.refresh_status)

    def _fetch_status(self) -> None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.info['port']}/queue", timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._post(lambda: self._apply_status(data))
        except Exception:
            self._post(lambda: self._apply_status(None))
        finally:
            self._polling = False

    def _apply_status(self, data: dict | None) -> None:
        if data is None:
            if self.server.running():
                self.status_values["workers"].configure(text="状态查询失败", fg=WARN)
            return
        workers = data.get("workers_online") or {}
        if workers:
            text = "、".join(f"{name}（并发 {cap}）" for name, cap in workers.items())
            self.status_values["workers"].configure(text=text, fg=TEXT)
        else:
            self.status_values["workers"].configure(text="暂无 worker —— 请让智能体 A 调用 service_wait", fg=WARN)
        self.status_values["queue"].configure(
            text=f"等待 {data.get('queued', 0)} · 处理中 {data.get('active', 0)} · 累计 {data.get('tracked', 0)}"
        )
        self.status_values["server"].configure(text=self.info["server_url"])

    # ---------------------------------------------------------------- 日志
    def _install_logging(self) -> None:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(QueueLogHandler(self.logs))
        logging.getLogger("model_mcp").setLevel(logging.INFO)

    def _drain_logs(self) -> None:
        lines: list[str] = []
        while len(lines) < 60:
            try:
                lines.append(self.logs.get_nowait())
            except stdqueue.Empty:
                break
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(lines) + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(300, self._drain_logs)

    def _clear_logs(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ---------------------------------------------------------------- 关闭
    def on_close(self) -> None:
        if self.server.running():
            if not messagebox.askokcancel(APP_TITLE, "服务仍在运行，退出会同时停止服务。确定退出？"):
                return
            self._set_state("停止中…", WARN)
            self.root.update_idletasks()
            self.server.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()

    # ---------------------------------------------------------------- 调试截图
    def capture(self, path: str, tab: int = 0) -> None:
        self.root.update_idletasks()
        self.root.update()
        if tab:
            self.nb.select(tab)
            self.root.update_idletasks()
            self.root.update()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.update()
        time.sleep(0.4)
        try:
            from PIL import ImageGrab

            x = self.root.winfo_rootx()
            y = self.root.winfo_rooty()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            image = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target)
        finally:
            self.root.attributes("-topmost", False)


def _enable_dpi_awareness() -> None:
    """让窗口在高分屏上不发虚。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


_MUTEX_HANDLE = None


def _acquire_single_instance(port: int) -> bool:
    """同一端口只允许一个控制台窗口，避免叠出多个窗口互相抢端口。"""
    global _MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, f"Local\\model_mcp_tk_ui_{port}")
        already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        _MUTEX_HANDLE = handle
        return not already
    except Exception:
        return True


def _focus_existing_window() -> bool:
    """把已有窗口提到最前，让用户知道服务其实已经在跑。"""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _log_file() -> Path:
    return Path(__file__).resolve().parent.parent / "logs" / "tk_ui.log"


def _setup_file_logging() -> None:
    """pythonw 没有控制台，崩溃与启动失败一律落到文件里。"""
    try:
        target = _log_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(target, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="model-mcp-tk", description="model-mcp 桌面控制台")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-autostart", action="store_true", help="打开窗口但不自动启动服务")
    parser.add_argument("--shot", default=None, help="截图保存路径（调试用，截完自动退出）")
    parser.add_argument("--shot-tab", type=int, default=1, help="截图时选中的标签页（0=第一页）")
    parser.add_argument("--shot-delay", type=int, default=1500, help="截图前等待毫秒")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port

    _enable_dpi_awareness()
    _setup_file_logging()

    if not args.shot and not _acquire_single_instance(cfg.server.port):
        _focus_existing_window()
        logging.getLogger("model_mcp").info("已有实例在 %s 端口运行，本次启动退出", cfg.server.port)
        return 0

    try:
        app = ConsoleApp(cfg, autostart=not args.no_autostart)
        if args.shot:
            def _finish() -> None:
                try:
                    app.capture(args.shot, args.shot_tab)
                finally:
                    app.server.stop()
                    app.root.destroy()

            app.root.after(max(300, args.shot_delay), _finish)
        app.run()
    except Exception as exc:  # pragma: no cover
        logging.exception("界面异常")
        detail = f"启动失败：{exc}\n\n详细日志：{_log_file()}"
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, detail)
        except Exception:
            print(detail)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
