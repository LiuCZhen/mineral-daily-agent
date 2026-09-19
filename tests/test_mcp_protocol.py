"""MCP 协议合规性测试：initialize / tools/list / tools/call 与错误处理。

这是「交付物能不能被 Claude Desktop / Cursor 接上」的自动化代理验证。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common.mcp_protocol import (                     # noqa: E402
    INVALID_PARAMS, METHOD_NOT_FOUND, McpServer, prop, schema,
)


def _echo(text: str, times: int = 1) -> str:
    return text * times


def build_server() -> McpServer:
    server = McpServer(name="test-mcp")

    @server.tool("echo", "回显文本", schema(
        properties={"text": prop("string", "要回显的文本"),
                    "times": prop("integer", "重复次数", default=1)},
        required=["text"]))
    def echo(text: str, times: int = 1) -> str:
        return json.dumps({"echo": _echo(text, times)})

    @server.tool("boom", "总是抛异常", schema(properties={}))
    def boom() -> str:
        raise ValueError("intentional failure")

    return server


def test_initialize_returns_protocol_and_capabilities() -> None:
    server = build_server()
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "t"}},
    })
    assert response is not None
    result = response["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "test-mcp"


def test_notification_has_no_response() -> None:
    server = build_server()
    assert server.handle_message({
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) is None


def test_tools_list_schema_is_object_rooted() -> None:
    server = build_server()
    response = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = response["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {"echo", "boom"}
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_tools_call_success() -> None:
    server = build_server()
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "ab", "times": 2}},
    })
    result = response["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"echo": "abab"}


def test_tools_call_missing_required_argument_is_invalid_params() -> None:
    server = build_server()
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "echo", "arguments": {}},
    })
    assert response["error"]["code"] == INVALID_PARAMS


def test_tools_call_rejects_unknown_arguments() -> None:
    server = build_server()
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "a", "nope": 1}},
    })
    assert response["error"]["code"] == INVALID_PARAMS


def test_tool_exception_becomes_is_error_result_not_crash() -> None:
    """工具内部异常必须转成 isError 结果 —— 否则整个 server 会被一次坏调用打挂。"""
    server = build_server()
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "boom", "arguments": {}},
    })
    assert response["result"]["isError"] is True
    assert "intentional failure" in response["result"]["content"][0]["text"]


def test_unknown_method_returns_method_not_found() -> None:
    server = build_server()
    response = server.handle_message({"jsonrpc": "2.0", "id": 7, "method": "does/not/exist"})
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_unknown_tool_is_invalid_params() -> None:
    server = build_server()
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "missing", "arguments": {}},
    })
    assert response["error"]["code"] == INVALID_PARAMS


def test_serve_forever_handles_bad_json_without_dying() -> None:
    import io

    server = build_server()
    stdin = io.StringIO("not json\n" + json.dumps({
        "jsonrpc": "2.0", "id": 9, "method": "tools/list"}) + "\n")
    stdout = io.StringIO()
    assert server.serve_forever(stdin=stdin, stdout=stdout) == 0
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    assert "error" in json.loads(lines[0])
    assert "result" in json.loads(lines[1])
