"""矿权日报 Agent — 运行期配置（全部可用环境变量覆盖）。

设计原则：
1. 任何外部数据源都可能失败（登录墙/频控/断网），因此每个源都带超时、重试、缓存与降级；
2. 所有开关集中在这里，避免散落在各 server 里读 os.environ；
3. 默认值与 .env.example 保持一致，评审按 RUN.md 操作即可复现。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 项目路径
PACKAGE_ROOT = Path(__file__).resolve().parents[2]          # mineral-daily-agent/
DATA_DIR = Path(os.getenv("MDA_DATA_DIR", PACKAGE_ROOT / "data"))
CACHE_DIR = Path(os.getenv("MDA_CACHE_DIR", DATA_DIR / "cache"))
FIXTURE_DIR = DATA_DIR / "fixtures"
LOG_DIR = Path(os.getenv("MDA_LOG_DIR", DATA_DIR / "logs"))


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# 实测：mining.com 等站点会拒绝自报家门的爬虫 UA（403），浏览器 UA 正常。
_DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class HttpConfig:
    """HTTP 行为：超时、重试、频控。频控是价格源（登录墙/接口频控）的真正考点。"""

    timeout_s: float = _float("MDA_HTTP_TIMEOUT", 20.0)
    connect_timeout_s: float = _float("MDA_HTTP_CONNECT_TIMEOUT", 8.0)
    max_retries: int = _int("MDA_HTTP_RETRIES", 3)
    backoff_base_s: float = _float("MDA_HTTP_BACKOFF", 1.5)
    user_agent: str = field(default_factory=lambda: os.getenv(
        "MDA_USER_AGENT", _DEFAULT_BROWSER_UA))
    # 每域名最小请求间隔（秒），避免触发频控
    min_interval_s: float = _float("MDA_HTTP_MIN_INTERVAL", 1.0)
    # 是否允许联网（构造时读取；运行期请用 Settings.network_enabled，它每次读环境变量）
    allow_network: bool = _bool("MDA_ALLOW_NETWORK", True)
    # 说明：user_agent 默认使用浏览器 UA。实测 mining.com 的 RSS 对自报家门的爬虫 UA
    # 返回 403（Akamai），换浏览器 UA 即正常；这是访问公开 RSS 的常规做法。
    # 若要以爬虫身份访问，设 MDA_USER_AGENT 覆盖。
    #
    # 但有些官方源要求**相反**：SEC.gov 对浏览器 UA 返回 403
    # （"Undeclared Automated Tool"），必须提供带联系方式的声明式 UA。
    # 因此这两个字段分开配置，PDF/文档下载按域名自动选用。
    declared_user_agent: str = field(default_factory=lambda: os.getenv(
        "MDA_DECLARED_USER_AGENT",
        "MineralDailyAgent/1.0 (public-data research; "
        "contact: set-MDA_DECLARED_USER_AGENT)"))


@dataclass(frozen=True)
class CacheConfig:
    """缓存策略：新闻原文长期留存，价格短 TTL，检索结果短 TTL。"""

    db_path: Path = CACHE_DIR / "store.sqlite3"
    article_ttl_s: int = _int("MDA_ARTICLE_TTL", 7 * 24 * 3600)
    price_ttl_s: int = _int("MDA_PRICE_TTL", 6 * 3600)
    http_ttl_s: int = _int("MDA_HTTP_CACHE_TTL", 900)
    enabled: bool = _bool("MDA_CACHE_ENABLED", True)


@dataclass(frozen=True)
class NewsConfig:
    """新闻源。RSS 优先（结构化、稳定），HTML 列表页作为补充。

    默认源经实测筛选（见 scripts/check_user_agent.py）：
    - mining.com 两个 feed：浏览器 UA 下可用；S&P Global：Akamai 两种 UA 均 403，
      保留在列表里是为了让降级链有真实失败可暴露，而不是假装它可用。
    - northernminer.com：实测 200 且稳定解析出条目，作为兜底源。
    """

    feeds: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            f.strip()
            for f in os.getenv(
                "MDA_NEWS_FEEDS",
                "https://www.mining.com/feed/,"
                "https://www.mining.com/category/critical-minerals/feed/,"
                "https://www.northernminer.com/feed/,"
                "https://www.spglobal.com/marketintelligence/en/rss/all",
            ).split(",")
            if f.strip()
        )
    )
    # 全文抓取时只保留正文块，避免把导航/广告写进知识库
    max_article_chars: int = _int("MDA_NEWS_MAX_CHARS", 12000)
    default_days: int = _int("MDA_NEWS_DEFAULT_DAYS", 7)
    default_limit: int = _int("MDA_NEWS_DEFAULT_LIMIT", 20)


@dataclass(frozen=True)
class PriceConfig:
    """价格源。按「免费可用 → 需登录」顺序降级，每个源独立超时。"""

    # 免费公开源（无需登录，可作 LME/SHFE 的代理指标并在 DATA_NOTES 里声明）
    primary_source: str = os.getenv("MDA_PRICE_SOURCE", "stooq")
    stooq_symbols_url: str = "https://stooq.com/q/d/l/?s={symbol}&i=d"
    # 可选：带 key 的付费/授权源，未配置则自动跳过
    lme_api_key: str = os.getenv("MDA_LME_API_KEY", "")
    metals_api_key: str = os.getenv("MDA_METALS_API_KEY", "")
    max_trend_days: int = _int("MDA_PRICE_MAX_TREND_DAYS", 180)


@dataclass(frozen=True)
class PdfConfig:
    """NI 43-101 抽取配置。"""

    max_pdf_mb: float = _float("MDA_PDF_MAX_MB", 40.0)
    max_pages: int = _int("MDA_PDF_MAX_PAGES", 400)
    # 目标章节标题（NI 43-101 惯用措辞）
    section_keywords: tuple[str, ...] = (
        "mineral resource",
        "mineral resources",
        "resource estimate",
        "measured",
        "indicated",
        "inferred",
    )
    # 单位换算：oz → t（金衡盎司）
    troy_oz_to_tonne: float = 31.1034768 / 1_000_000.0
    tolerance_pct: float = _float("MDA_ACCURACY_TOLERANCE", 5.0)
    # 低于该置信度直接 abstain，不硬给答案（题面最看重的行为）
    abstain_below: float = _float("MDA_ABSTAIN_BELOW", 0.55)


@dataclass(frozen=True)
class LLMConfig:
    """模型配置：Extractor 与 Critic 使用不同模型族，避免同源同错。"""

    extractor_provider: str = os.getenv("MDA_EXTRACTOR_PROVIDER", "openai_compatible")
    extractor_model: str = os.getenv("MDA_EXTRACTOR_MODEL", "deepseek-chat")
    critic_provider: str = os.getenv("MDA_CRITIC_PROVIDER", "openai_compatible")
    critic_model: str = os.getenv("MDA_CRITIC_MODEL", "qwen-plus")
    base_url: str = os.getenv("MDA_LLM_BASE_URL", "https://api.deepseek.com/v1")
    critic_base_url: str = os.getenv("MDA_CRITIC_BASE_URL", os.getenv("MDA_LLM_BASE_URL", ""))
    api_key: str = os.getenv("MDA_LLM_API_KEY", "")
    critic_api_key: str = os.getenv("MDA_CRITIC_API_KEY", "")
    temperature: float = _float("MDA_LLM_TEMPERATURE", 0.0)
    max_tokens: int = _int("MDA_LLM_MAX_TOKENS", 2048)
    timeout_s: float = _float("MDA_LLM_TIMEOUT", 90.0)

    @property
    def configured(self) -> bool:
        """是否配了可用的 LLM key；没有则自动走确定性解析器。"""
        return bool(self.api_key)


@dataclass(frozen=True)
class Settings:
    http: HttpConfig = field(default_factory=HttpConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    price: PriceConfig = field(default_factory=PriceConfig)
    pdf: PdfConfig = field(default_factory=PdfConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    @property
    def offline(self) -> bool:
        """每次读取环境变量：测试与 CLI 会在 import 之后再切离线模式。"""
        return _bool("MDA_OFFLINE", False)

    @property
    def network_enabled(self) -> bool:
        return self.http.allow_network and not self.offline


def ensure_dirs() -> None:
    for path in (DATA_DIR, CACHE_DIR, LOG_DIR, FIXTURE_DIR):
        path.mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()
