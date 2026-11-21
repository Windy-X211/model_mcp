"""配置加载：config.yaml -> Config，支持 ${ENV_VAR} 展开。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_MODEL = "agent-a"


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class ModelSpec:
    name: str
    description: str = ""
    capacity: int = 1
    supports: list[str] = field(default_factory=lambda: ["stream", "tools", "reasoning"])


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str | None = None


@dataclass
class QueueConfig:
    wait_timeout_s: int = 40
    request_deadline_s: int = 600
    lease_s: int = 120
    requeue_max: int = 2
    retain_terminal_s: int = 300
    max_tracked: int = 2000
    reject_when_no_worker: bool = True


@dataclass
class StreamConfig:
    replay_chunk_chars: int = 24
    replay_delay_ms: int = 30
    heartbeat_s: int = 15


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    models: list[ModelSpec] = field(default_factory=lambda: [ModelSpec(DEFAULT_MODEL)])
    queue: QueueConfig = field(default_factory=QueueConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)

    @property
    def models_by_name(self) -> dict[str, ModelSpec]:
        return {m.name: m for m in self.models}

    def default_model(self) -> str:
        return self.models[0].name if self.models else DEFAULT_MODEL


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def load_config(path: str | Path | None = None) -> Config:
    """读取 YAML 配置；文件不存在时使用内置默认值。"""
    if path is None:
        path = os.environ.get("MODEL_MCP_CONFIG") or Path(__file__).resolve().parent.parent / "config.yaml"
    path = Path(path)
    raw: dict = {}
    if path.is_file():
        with path.open("r", encoding="utf-8") as fh:
            raw = _expand(yaml.safe_load(fh) or {})
    raw = _as_dict(raw)

    srv = _as_dict(raw.get("server"))
    q = _as_dict(raw.get("queue"))
    st = _as_dict(raw.get("stream"))

    cfg = Config(
        server=ServerConfig(
            host=str(srv.get("host", ServerConfig.host)),
            port=int(srv.get("port", ServerConfig.port)),
            auth_token=(str(srv["auth_token"]) if srv.get("auth_token") else None),
        ),
        queue=QueueConfig(
            wait_timeout_s=int(q.get("wait_timeout_s", QueueConfig.wait_timeout_s)),
            request_deadline_s=int(q.get("request_deadline_s", QueueConfig.request_deadline_s)),
            lease_s=int(q.get("lease_s", QueueConfig.lease_s)),
            requeue_max=int(q.get("requeue_max", QueueConfig.requeue_max)),
            retain_terminal_s=int(q.get("retain_terminal_s", QueueConfig.retain_terminal_s)),
            max_tracked=int(q.get("max_tracked", QueueConfig.max_tracked)),
            reject_when_no_worker=bool(q.get("reject_when_no_worker", QueueConfig.reject_when_no_worker)),
        ),
        stream=StreamConfig(
            replay_chunk_chars=int(st.get("replay_chunk_chars", StreamConfig.replay_chunk_chars)),
            replay_delay_ms=int(st.get("replay_delay_ms", StreamConfig.replay_delay_ms)),
            heartbeat_s=int(st.get("heartbeat_s", StreamConfig.heartbeat_s)),
        ),
    )

    models: list[ModelSpec] = []
    for item in raw.get("models") or []:
        item = _as_dict(item)
        if not item.get("name"):
            continue
        models.append(
            ModelSpec(
                name=str(item["name"]),
                description=str(item.get("description", "")),
                capacity=int(item.get("capacity", 1)),
                supports=list(item.get("supports") or ["stream", "tools", "reasoning"]),
            )
        )
    if models:
        cfg.models = models
    return cfg
