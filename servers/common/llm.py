"""LLM 提供方封装：OpenAI 兼容接口 + 未配置时的显式不可用。

设计取舍：
- 只用 OpenAI 兼容协议（DeepSeek / Qwen-DashScope / GLM / OpenAI 全部支持），
  一个实现覆盖三家，避免为每家写适配器；
- Extractor 与 Critic 使用**不同模型族**（默认 deepseek-chat vs qwen-plus），
  这是题面「Critic 调另一个模型」的要求，也是减少同源同错的工程手段；
- 没配 key 时抛 LLMUnavailable，调用方走确定性降级路径，
  绝不静默返回空结果假装成功。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from servers.common.config import SETTINGS
from servers.common.logging_utils import get_logger

log = get_logger("common.llm")


class LLMUnavailable(RuntimeError):
    """未配置 key / 网络不可用 / 上游报错。调用方必须走降级路径。"""


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict[str, Any]
    raw: dict[str, Any]


def _clean(base_url: str) -> str:
    return base_url.rstrip("/")


class LLMClient:
    """OpenAI 兼容 chat/completions 客户端。"""

    def __init__(self, role: str = "extractor", *, model: str | None = None,
                 base_url: str | None = None, api_key: str | None = None,
                 timeout_s: float | None = None) -> None:
        cfg = SETTINGS.llm
        if role == "critic":
            self.model = model or cfg.critic_model
            self.base_url = _clean(base_url or cfg.critic_base_url or cfg.base_url)
            self.api_key = api_key or cfg.critic_api_key or cfg.api_key
        else:
            self.model = model or cfg.extractor_model
            self.base_url = _clean(base_url or cfg.base_url)
            self.api_key = api_key or cfg.api_key
        self.timeout_s = timeout_s or cfg.timeout_s
        self.role = role
        if not self.api_key:
            raise LLMUnavailable(
                f"未配置 MDA_LLM_API_KEY（role={role}），请设置后重试；"
                "系统会自动改用确定性解析路径。"
            )

    # -------------------------------------------------------------- 调用
    def complete(self, prompt: str, *, system: str | None = None,
                 temperature: float | None = None,
                 max_tokens: int | None = None) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": SETTINGS.llm.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or SETTINGS.llm.max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout_s, connect=8.0)) as client:
                response = client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"LLM 请求失败（{self.model}）：{exc}") from exc

        if response.status_code >= 400:
            raise LLMUnavailable(
                f"LLM 返回 {response.status_code}（{self.model}）：{response.text[:300]}"
            )
        body = response.json()
        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable(f"LLM 响应结构异常：{str(body)[:300]}") from exc
        return LLMResponse(text=text, model=self.model,
                           usage=body.get("usage") or {}, raw=body)

    def complete_json(self, prompt: str, *, system: str | None = None,
                      **kwargs: Any) -> dict[str, Any]:
        """要求模型返回 JSON，并做一次容错解析（去掉 ```json 围栏、截取最外层花括号）。"""
        response = self.complete(prompt, system=system, **kwargs)
        payload = extract_json_object(response.text)
        if payload is None:
            raise LLMUnavailable(
                f"LLM 未返回合法 JSON（{self.model}）：{response.text[:200]}"
            )
        return payload


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里稳健地取出 JSON 对象。"""
    if not text:
        return None
    candidates: list[str] = []
    fenced = _JSON_FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
