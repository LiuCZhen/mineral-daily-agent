"""零第三方依赖的 MCP（Model Context Protocol）stdio 协议实现。

为什么要自己实现协议栈：
官方 Python SDK 需要联网安装，而交付要求「5 分钟内跑起来」。MCP 的 stdio 传输本质是
换行分隔的 JSON-RPC 2.0，协议面很小，自研后交付物变成零依赖，评审 clone 即跑。

实现的协议要点（与 MCP 2024-11-05 规范一致）：
- 传输：stdin/stdout 上按行分隔的 JSON-RPC 2.0 消息（每行一个完整 JSON）；
- 生命周期：initialize → notifications/initialized → tools/list / tools/call；
- initialize 返回 protocolVersion、capabilities.tools.listChanged、serverInfo；
- tools/list 返回 {name, description, inputSchema}，inputSchema 为 object 型 JSON Schema；
- tools/call 返回 {content: [{type: "text", text: ...}], isError}；
- 通知（无 id）不回复；未知方法返回 -32601。
日志一律走 stderr，stdout 只允许协议消息（MCP 客户端会因此解析失败）。
"""

from __future__ import annotations

import inspect
import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

PROTOCOL_VERSION = "2024-11-05"
SERVER_VERSION = "1.0.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    # 返回体是否已经是字符串（True 则不再 JSON 序列化）
    returns_text: bool = False

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    structured: Any = None

    @classmethod
    def from_any(cls, value: Any, *, is_error: bool = False) -> "ToolResult":
        if isinstance(value, ToolResult):
            return value
        if isinstance(value, str):
            return cls(text=value, is_error=is_error)
        return cls(
            text=json.dumps(value, ensure_ascii=False, indent=2, default=str),
            is_error=is_error,
            structured=value,
        )


def schema(*, properties: dict[str, Any], required: list[str] | None = None,
           description: str = "") -> dict[str, Any]:
    """构造 object 型 inputSchema。MCP 要求根节点必须是 object。"""
    out: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        # 显式 additionalProperties: false，让客户端与模型都不传野字段
        "additionalProperties": False,
    }
    if required:
        out["required"] = required
    if description:
        out["description"] = description
    return out


def prop(type_name: str, description: str, *, default: Any = None,
         enum: list[Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": type_name, "description": description}
    if default is not None:
        out["default"] = default
    if enum:
        out["enum"] = enum
    return out


class McpServer:
    """stdio MCP server。同步单线程循环：工具实现内部自己处理并发需求。"""

    def __init__(self, name: str, version: str = SERVER_VERSION,
                 instructions: str = "") -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self.tools: dict[str, ToolSpec] = {}
        self._initialized = False

    # ------------------------------------------------------------ 注册
    def tool(self, name: str, description: str, input_schema: dict[str, Any],
             returns_text: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = ToolSpec(
                name=name, description=description, input_schema=input_schema,
                handler=func, returns_text=returns_text,
            )
            return func
        return decorator

    # ------------------------------------------------------ 协议处理
    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 消息，返回要写回的响应（通知返回 None）。"""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "message must be a JSON object")

        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if method == "initialize":
            return _result(msg_id, self._initialize_result(params))
        if method in ("notifications/initialized", "initialized"):
            self._initialized = True
            return None
        if method == "ping":
            return _result(msg_id, {})
        if method in ("tools/list", "list_tools"):
            return _result(msg_id, {"tools": [t.descriptor() for t in self.tools.values()]})
        if method in ("tools/call", "call_tool"):
            return self._call_tool(msg_id, params)
        if method in ("resources/list", "prompts/list"):
            # 本交付只暴露 tools，但显式回应空列表比报错更友好（部分客户端会探测）
            key = "resources" if method.startswith("resources") else "prompts"
            return _result(msg_id, {key: []})
        if method == "notifications/cancelled":
            return None

        if is_notification:
            return None
        return _error(msg_id, METHOD_NOT_FOUND, f"unknown method: {method}")

    def _initialize_result(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        return {
            # 客户端请求的版本若受支持则原样返回，否则回落到本实现版本
            "protocolVersion": requested if requested else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
            "instructions": self.instructions or None,
        }

    def _call_tool(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in self.tools:
            return _error(msg_id, INVALID_PARAMS, f"unknown tool: {name!r}")
        if not isinstance(arguments, dict):
            return _error(msg_id, INVALID_PARAMS, "arguments must be an object")

        spec = self.tools[name]
        try:
            _validate_arguments(spec, arguments)
            value = spec.handler(**arguments)
            if inspect.iscoroutine(value):
                value = _run_coroutine(value)
            result = ToolResult.from_any(value)
        except TypeError as exc:
            return _error(msg_id, INVALID_PARAMS, f"invalid arguments for {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001 - 协议层兜底
            # 工具内部异常不能让整个 server 退出：返回 isError 结果，模型可据此重试/降级
            detail = f"{type(exc).__name__}: {exc}"
            return _result(msg_id, {
                "content": [{"type": "text", "text": detail}],
                "isError": True,
                "_meta": {"traceback": traceback.format_exc()[-2000:]},
            })

        payload: dict[str, Any] = {
            "content": [{"type": "text", "text": result.text}],
            "isError": result.is_error,
        }
        if result.structured is not None:
            # 部分客户端（含 Cursor）会优先读 structuredContent
            payload["structuredContent"] = result.structured
        return _result(msg_id, payload)

    # --------------------------------------------------------- 主循环
    def serve_forever(self, stdin: Any = None, stdout: Any = None) -> int:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(stdout, _error(None, PARSE_ERROR, f"invalid JSON: {exc}"))
                continue
            response = self.handle_message(message)
            if response is not None:
                _write(stdout, response)
        return 0


def _run_coroutine(coro: Any) -> Any:
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _validate_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> None:
    """轻量入参校验：必填项与类型。避免把明显错误的调用传给工具实现。"""
    schema_ = spec.input_schema
    required = schema_.get("required") or []
    for key in required:
        if key not in arguments or arguments[key] is None:
            raise TypeError(f"missing required argument: {key}")
    properties = schema_.get("properties") or {}
    if schema_.get("additionalProperties") is False:
        unknown = set(arguments) - set(properties)
        if unknown:
            raise TypeError(f"unexpected arguments: {sorted(unknown)}")
    type_map = {
        "string": str, "integer": int, "number": (int, float),
        "boolean": bool, "array": list, "object": dict,
    }
    for key, value in arguments.items():
        expected = (properties.get(key) or {}).get("type")
        if expected in type_map and not isinstance(value, type_map[expected]):
            raise TypeError(f"argument {key} must be {expected}")


def _result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _write(stdout: Any, payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    stdout.flush()


def run_server(server: McpServer) -> int:
    """入口封装：把未捕获异常写进 stderr，避免污染 stdout 协议通道。"""
    try:
        return server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:                                   # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        return 1
