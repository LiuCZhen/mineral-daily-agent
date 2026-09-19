"""HTTP 客户端 + 每域名频控 + 退避重试。

对应题面三个源的共同痛点：
- 源 1（mining.com / S&P Global）：需要稳定的 RSS 轮询与全文抓取；
- 源 2（政府官网）：HTML 不规整，需要容错解析（见 mining_news 的 html 抽取）；
- 源 3（LME/SHFE/钢联）：登录墙 + 接口频控 → 必须限速、重试、缓存、可降级。

所有对外请求都从这里走，保证限速与重试策略只有一份实现。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from servers.common.config import SETTINGS
from servers.common.logging_utils import get_logger

log = get_logger("common.http")

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
_RATE_LOCKS: dict[str, asyncio.Lock] = {}
_LAST_CALL: dict[str, float] = {}


class FetchError(RuntimeError):
    """网络层最终失败（已重试耗尽或被离线模式拦截）。"""

    def __init__(self, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(f"{url} -> {reason}" + (f" (status={status})" if status else ""))
        self.url = url
        self.reason = reason
        self.status = status


@dataclass
class Response:
    url: str
    status: int
    text: str
    headers: dict[str, str]
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _lock_for(host: str) -> asyncio.Lock:
    if host not in _RATE_LOCKS:
        _RATE_LOCKS[host] = asyncio.Lock()
    return _RATE_LOCKS[host]


async def _respect_rate_limit(host: str) -> None:
    """同一域名串行 + 最小间隔，避免触发频控被封。"""
    interval = SETTINGS.http.min_interval_s
    if interval <= 0:
        return
    async with _lock_for(host):
        elapsed = time.monotonic() - _LAST_CALL.get(host, 0.0)
        wait = interval - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_CALL[host] = time.monotonic()


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": SETTINGS.http.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }


# 要求「声明式 UA（含联系方式）」的域名：这类官方源对浏览器 UA 反而返回 403。
# 实测：SEC.gov 用浏览器 UA → 403 "Undeclared Automated Tool"；
#       换带联系方式的 UA → 200，正常返回 PDF。
DECLARED_UA_HOSTS = ("sec.gov", "efts.sec.gov", "www.sec.gov")


def user_agent_for(url: str, override: str | None = None) -> str:
    """按域名选择 UA：官方源用声明式，其余用浏览器 UA（两者的反爬要求正好相反）。"""
    if override:
        return override
    host = urlparse(url).netloc.lower()
    if any(host == h or host.endswith("." + h) for h in DECLARED_UA_HOSTS):
        return SETTINGS.http.declared_user_agent
    return SETTINGS.http.user_agent


async def fetch(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout_s: float | None = None,
    retries: int | None = None,
    follow_redirects: bool = True,
) -> Response:
    """带频控、退避重试、状态码判定的单次抓取。

    离线模式（MDA_OFFLINE=1 或 MDA_ALLOW_NETWORK=0）下直接抛 FetchError，
    由上层走缓存/fixtures 降级路径 —— 这是保证「断网也能演示」的开关。
    """
    if not SETTINGS.network_enabled:
        raise FetchError(url, "network disabled (offline mode)")

    cfg = SETTINGS.http
    max_retries = cfg.max_retries if retries is None else retries
    timeout = httpx.Timeout(
        timeout_s or cfg.timeout_s, connect=cfg.connect_timeout_s
    )
    merged_headers = _default_headers()
    # 按域名选用正确 UA（官方源与商业站点的反爬要求相反）
    merged_headers["User-Agent"] = user_agent_for(url)
    if headers:
        merged_headers.update(headers)

    last_error: Exception | None = None
    last_status: int | None = None

    for attempt in range(max_retries + 1):
        await _respect_rate_limit(_host(url))
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=follow_redirects
            ) as client:
                response = await client.request(
                    method, url, params=params, headers=merged_headers, data=data
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            last_status = response.status_code

            if response.status_code in RETRY_STATUS:
                # 429/503 优先听服务端的 Retry-After
                retry_after = response.headers.get("Retry-After", "")
                sleep_s = _backoff(attempt, retry_after)
                log.warning(
                    "retryable status",
                    extra={"tool": "http", "source": url, "extra_data": {
                        "status": response.status_code, "attempt": attempt,
                        "sleep_s": round(sleep_s, 2), "duration_ms": duration_ms,
                    }},
                )
                if attempt < max_retries:
                    await asyncio.sleep(sleep_s)
                    continue
                raise FetchError(url, "retryable status exhausted", response.status_code)

            log.info(
                "http ok",
                extra={"tool": "http", "source": url, "duration_ms": duration_ms,
                       "extra_data": {"status": response.status_code, "attempt": attempt}},
            )
            return Response(
                url=str(response.url),
                status=response.status_code,
                text=response.text,
                headers=dict(response.headers),
            )

        except FetchError:
            raise
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            last_error = exc
            sleep_s = _backoff(attempt, None)
            log.warning(
                "transport error",
                extra={"tool": "http", "source": url, "extra_data": {
                    "error": type(exc).__name__, "detail": str(exc)[:200],
                    "attempt": attempt, "sleep_s": round(sleep_s, 2),
                }},
            )
            if attempt < max_retries:
                await asyncio.sleep(sleep_s)
                continue

    raise FetchError(url, f"{type(last_error).__name__}: {last_error}", last_status)


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    base = SETTINGS.http.backoff_base_s
    return min(base * (2**attempt), 30.0) + random.uniform(0, 0.4)


def fetch_sync(url: str, **kwargs: Any) -> Response:
    """同步包装。若当前线程已有事件循环，则另起线程执行，避免嵌套报错。"""
    import concurrent.futures

    def runner() -> Response:
        return asyncio.run(fetch(url, **kwargs))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(fetch(url, **kwargs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(runner).result()


@dataclass
class FetchOutcome:
    """带来源标记的抓取结果，是「降级」这一概念的落地位置。"""

    url: str
    text: str
    status: int
    origin: str                  # live | cache | offline_fixture | none
    age_s: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.origin != "none"

    @property
    def degraded(self) -> bool:
        return self.origin in ("cache", "offline_fixture")


def fetch_text_cached(url: str, *, ttl_s: int, store: Any | None = None,
                      headers: dict[str, str] | None = None,
                      offline_fixture: str | None = None) -> FetchOutcome:
    """三级降级抓取（同步，供 MCP 工具直接调用）：

    1. 命中新鲜缓存 → origin=cache（不发起网络请求，保护频控额度）
    2. 实时抓取成功 → origin=live，并回写缓存
    3. 网络失败但有陈旧缓存 → origin=cache, degraded
    4. 完全不可用 → origin=offline_fixture（若提供了兜底内容）或 none

    这样「断网 / 被登录墙拦住」时服务仍然可用，且 Agent 能明确知道数据是降级的。
    """
    from servers.common.store import Store

    store = store or Store()
    key = Store.cache_key("GET", url)

    cached = store.cache_get(key, ttl_s) if SETTINGS.cache.enabled else None
    if cached:
        return FetchOutcome(url=url, text=cached["body"], status=cached["status"],
                            origin="cache", age_s=cached["age_s"])

    stale = store.cache_get(key, -1) if SETTINGS.cache.enabled else None
    try:
        response = fetch_sync(url, headers=headers)
        if SETTINGS.cache.enabled and response.text:
            store.cache_put(key, url, response.status, response.text, response.headers)
        return FetchOutcome(url=url, text=response.text, status=response.status,
                            origin="live")
    except FetchError as exc:
        if stale:
            return FetchOutcome(url=url, text=stale["body"], status=stale["status"],
                                origin="cache", age_s=stale["age_s"], error=str(exc))
        if offline_fixture is not None:
            return FetchOutcome(url=url, text=offline_fixture, status=200,
                                origin="offline_fixture", error=str(exc))
        return FetchOutcome(url=url, text="", status=0, origin="none", error=str(exc))
