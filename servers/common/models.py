"""跨 server 的统一返回体约定。

一个 MCP server 的工具返回必须让 Agent 一眼看出三件事：
1. 数据是什么（data）；
2. 数据从哪来、是不是代理指标（provenance）；
3. 这次调用是否降级、为什么（degraded / warnings）。

把这三件事固化成同一个信封，Agent 的「风险提示」章节才有可靠输入，
而不是靠模型自己猜数据可不可信。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Severity = Literal["info", "warning", "error"]


@dataclass
class Warning_:
    code: str
    message: str
    severity: Severity = "warning"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class Envelope:
    """工具返回信封。data 一律为可 JSON 序列化结构。"""

    tool: str
    data: Any
    # 本次数据实际来源（url / 文件名 / 缓存）
    sources: list[dict[str, Any]] = field(default_factory=list)
    # 是否降级：true 表示没有拿到一手数据（离线缓存、代理指标、fixtures）
    degraded: bool = False
    warnings: list[Warning_] = field(default_factory=list)
    # 0-1，工具对本次结果的把握程度，Agent 据此决定要不要在简报里标注不确定
    confidence: float = 1.0
    error: str | None = None

    def warn(self, code: str, message: str, severity: Severity = "warning") -> "Envelope":
        self.warnings.append(Warning_(code, message, severity))
        if severity == "error":
            self.confidence = min(self.confidence, 0.3)
        elif severity == "warning":
            self.confidence = min(self.confidence, 0.7)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": self.data,
            "provenance": {
                "sources": self.sources,
                "degraded": self.degraded,
                "confidence": round(self.confidence, 3),
            },
            "warnings": [w.to_dict() for w in self.warnings],
            "error": self.error,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str)


def ok(tool: str, data: Any, **kwargs: Any) -> Envelope:
    return Envelope(tool=tool, data=data, **kwargs)


def fail(tool: str, message: str, *, code: str = "tool_error",
         degraded: bool = True) -> Envelope:
    env = Envelope(tool=tool, data=None, degraded=degraded, confidence=0.0, error=message)
    env.warn(code, message, severity="error")
    return env
