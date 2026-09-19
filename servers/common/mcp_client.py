"""MCP client：以子进程方式拉起 stdio server，完成 initialize / tools/list / tools/call。

Agent 侧唯一与协议细节耦合的地方。用同步 Popen + 后台读线程实现带超时的请求-响应，
好处是 Agent 主循环可以是普通同步代码（无需 asyncio 事件循环嵌套）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from servers.common.logging_utils import get_logger
from servers.common.mcp_protocol import PROTOCOL_VERSION

log = get_logger("common.mcp_client")


class McpError(RuntimeError):
    pass


class McpTimeout(McpError):
    pass


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    server: str

    def required_args(self) -> list[str]:
        return list(self.input_schema.get("required") or [])


@dataclass
class CallResult:
    ok: bool
    text: str
    data: Any = None
    is_error: bool = False
    duration_ms: int = 0
    error: str | None = None
    attempts: int = 1

    @property
    def degraded(self) -> bool:
        """返回体里显式标记的降级标志（见 servers/common/models.py 的信封约定）。"""
        if isinstance(self.data, dict):
            return bool((self.data.get("provenance") or {}).get("degraded"))
        return False

    @property
    def warnings(self) -> list[dict[str, Any]]:
        if isinstance(self.data, dict):
            return list(self.data.get("warnings") or [])
        return []


class McpClient:
    """一个 server 一个子进程；支持超时、重试与优雅关闭。"""

    def __init__(self, name: str, command: list[str], *, cwd: Path | str | None = None,
                 env: dict[str, str] | None = None, timeout_s: float = 60.0,
                 retries: int = 1) -> None:
        self.name = name
        self.command = command
        self.cwd = str(cwd) if cwd else None
        self.extra_env = env or {}
        self.timeout_s = timeout_s
        self.retries = retries
        self.tools: dict[str, ToolInfo] = {}
        self.server_info: dict[str, Any] = {}
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._responses: dict[Any, dict[str, Any]] = {}
        self._response_event = threading.Event()
        self._next_id = 0
        self._reader: threading.Thread | None = None
        self.stderr_lines: list[str] = []

    # ------------------------------------------------------------ 生命周期
    def start(self) -> "McpClient":
        if self._proc is not None:
            return self
        env = os.environ.copy()
        env.update(self.extra_env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        log.info("starting mcp server %s: %s", self.name, " ".join(self.command))
        self._proc = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, name=f"{self.name}-stdout",
                                        daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain_stderr, name=f"{self.name}-stderr",
                         daemon=True).start()
        self._initialize()
        return self

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            line = line.rstrip()
            if line:
                self.stderr_lines.append(line)
                if len(self.stderr_lines) > 400:
                    self.stderr_lines.pop(0)
                log.info("[%s stderr] %s", self.name, line)

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                log.warning("[%s] non-JSON on stdout: %s", self.name, line[:300])
                continue
            msg_id = message.get("id")
            self._responses[msg_id] = message
            self._response_event.set()

    def _initialize(self) -> None:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "mineral-daily-agent", "version": "1.0.0"},
        }, timeout_s=self.timeout_s)
        self.server_info = dict(result or {})
        # 通知不需要响应
        self._notify("notifications/initialized", {})
        listed = self._request("tools/list", {}, timeout_s=self.timeout_s) or {}
        for descriptor in listed.get("tools", []):
            info = ToolInfo(
                name=descriptor.get("name", ""),
                description=descriptor.get("description", ""),
                input_schema=descriptor.get("inputSchema") or {},
                server=self.name,
            )
            self.tools[info.name] = info
        log.info("server %s exposes %d tools: %s", self.name, len(self.tools),
                 sorted(self.tools))

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except (OSError, ValueError):
            pass

    def __enter__(self) -> "McpClient":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- 调用
    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallResult:
        """调用工具，失败自动重试；超时不重试（多半是工具本身慢，重试只会更慢）。"""
        attempts = 0
        last_error: str | None = None
        while attempts <= self.retries:
            attempts += 1
            started = time.monotonic()
            try:
                response = self._request("tools/call", {
                    "name": name, "arguments": arguments or {},
                }, timeout_s=self.timeout_s)
            except McpTimeout as exc:
                return CallResult(ok=False, text="", is_error=True,
                                  error=f"timeout after {self.timeout_s}s: {exc}",
                                  attempts=attempts,
                                  duration_ms=int((time.monotonic() - started) * 1000))
            except McpError as exc:
                last_error = str(exc)
                if attempts > self.retries:
                    break
                time.sleep(0.3 * attempts)
                continue

            duration_ms = int((time.monotonic() - started) * 1000)
            content = response.get("content") or []
            text = "\n".join(
                block.get("text", "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            structured = response.get("structuredContent")
            if structured is None and text:
                try:
                    structured = json.loads(text)
                except json.JSONDecodeError:
                    structured = None
            is_error = bool(response.get("isError"))
            if is_error and attempts <= self.retries and _retryable(text):
                last_error = text
                time.sleep(0.3 * attempts)
                continue
            return CallResult(ok=not is_error, text=text, data=structured,
                              is_error=is_error, attempts=attempts,
                              duration_ms=duration_ms,
                              error=text if is_error else None)

        return CallResult(ok=False, text="", is_error=True,
                          error=last_error or "unknown error", attempts=attempts)

    # ------------------------------------------------------------ 传输实现
    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any],
                 timeout_s: float | None = None) -> Any:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            deadline = time.monotonic() + (timeout_s or self.timeout_s)
            while time.monotonic() < deadline:
                if msg_id in self._responses:
                    message = self._responses.pop(msg_id)
                    if "error" in message:
                        raise McpError(f"{method} failed: {message['error']}")
                    return message.get("result")
                self._response_event.wait(0.05)
                if self._proc is not None and self._proc.poll() is not None:
                    raise McpError(
                        f"server {self.name} exited with code {self._proc.returncode}; "
                        f"stderr tail: {' | '.join(self.stderr_lines[-5:])}"
                    )
            raise McpTimeout(f"{method} via {self.name}")

    def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError(f"server {self.name} is not running")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()


def _retryable(text: str) -> bool:
    low = (text or "").lower()
    return any(token in low for token in ("timeout", "timed out", "connection",
                                          "temporarily", "rate limit", "429", "503"))


def default_python() -> str:
    return sys.executable or "python"
