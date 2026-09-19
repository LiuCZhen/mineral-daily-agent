"""统一日志：控制台 + 结构化 JSONL 落盘。

MCP server 的 stdout 属于协议通道，日志必须只走 stderr，
否则会污染 JSON-RPC 流导致客户端解析失败。这里强制 stream=stderr。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from servers.common.config import LOG_DIR, ensure_dirs

_CONFIGURED: dict[str, logging.Logger] = {}


class _JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("event", "tool", "duration_ms", "source", "extra_data"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str, jsonl: bool = True) -> logging.Logger:
    """返回一个已配置的 logger；同名只配置一次。"""
    if name in _CONFIGURED:
        return _CONFIGURED[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        # 关键：MCP 的 stdout 是协议通道，日志一律 stderr
        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(console)

        if jsonl:
            try:
                ensure_dirs()
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(
                    Path(LOG_DIR) / f"{name.replace('.', '_')}.jsonl", encoding="utf-8"
                )
                file_handler.setFormatter(_JsonlFormatter())
                logger.addHandler(file_handler)
            except OSError:
                # 日志落盘失败不能影响服务本身
                pass

    _CONFIGURED[name] = logger
    return logger
