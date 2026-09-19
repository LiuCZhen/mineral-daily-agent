"""SQLite 持久化层（schema 与主键/去重策略见 DATA_NOTES.md）。

三张表对应三类数据，各自的主键设计就是题面「去重策略」的答案：
- articles   : PRIMARY KEY = sha1(normalized_url)，回答「同一篇文章被多个 feed 重复收录」
- price_points: PRIMARY KEY = (commodity, trade_date, source)，回答「同一品种同一天多源冲突」
- http_cache : PRIMARY KEY = sha1(method+url+params)，控制对外请求量与频控压力
- evolution  : 题 #3 风格的自省日志，client 侧用于记录工具失败/降级，供复跑改进

FTS5 若不可用自动退化为 LIKE 检索，保证在最小化 SQLite 构建上也能跑。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlencode, urlsplit, urlunsplit

from servers.common.config import SETTINGS, ensure_dirs
from servers.common.logging_utils import get_logger

log = get_logger("common.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url_hash     TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT DEFAULT '',
    content      TEXT DEFAULT '',
    author       TEXT DEFAULT '',
    published_at TEXT,
    fetched_at   TEXT NOT NULL,
    lang         TEXT DEFAULT 'en',
    commodities  TEXT DEFAULT '[]',
    regions      TEXT DEFAULT '[]',
    content_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);

CREATE TABLE IF NOT EXISTS price_points (
    commodity   TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    source      TEXT NOT NULL,
    price       REAL NOT NULL,
    unit        TEXT NOT NULL,
    currency    TEXT NOT NULL,
    open        REAL, high REAL, low REAL, close REAL,
    volume      REAL,
    fetched_at  TEXT NOT NULL,
    is_proxy    INTEGER DEFAULT 0,
    PRIMARY KEY (commodity, trade_date, source)
);
CREATE INDEX IF NOT EXISTS idx_price_commodity_date ON price_points(commodity, trade_date DESC);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key  TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    status     INTEGER NOT NULL,
    body       TEXT NOT NULL,
    headers    TEXT DEFAULT '{}',
    stored_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    stage      TEXT NOT NULL,
    tool       TEXT,
    severity   TEXT DEFAULT 'warning',
    reason     TEXT NOT NULL,
    payload    TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_evolution_stage ON evolution(stage, ts DESC);
"""


def normalize_url(url: str) -> str:
    """URL 归一化 —— 去重第一道闸：去 fragment、统一大小写 host、剔除跟踪参数。"""
    parts = urlsplit(url.strip())
    tracking = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "spm",
    }
    kept = [
        (k, v)
        for k, v in _parse_qs(parts.query)
        if k.lower() not in tracking
    ]
    query = urlencode(sorted(kept))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _parse_qs(query: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in query.split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        out.append((key, value))
    return out


def url_key(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


def content_key(title: str, content: str) -> str:
    """标题+正文指纹 —— 去重第二道闸：同一报道被不同 URL 转载（syndication）。"""
    basis = (title.strip().lower() + "\n" + content.strip()[:2000].lower()).encode("utf-8")
    return hashlib.sha1(basis).hexdigest()


class Store:
    """线程安全的 SQLite 访问层（MCP server 可能被并发调用）。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        ensure_dirs()
        self.db_path = Path(db_path or SETTINGS.cache.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self.fts_enabled = self._setup_fts()

    # ------------------------------------------------------------ FTS
    def _setup_fts(self) -> bool:
        try:
            with self._lock:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts "
                    "USING fts5(title, summary, content, url_hash UNINDEXED, "
                    "tokenize='unicode61')"
                )
                self._conn.commit()
            self._rebuild_fts_if_empty()
            return True
        except sqlite3.OperationalError as exc:
            log.warning("FTS5 unavailable, fallback to LIKE: %s", exc)
            return False

    def _rebuild_fts_if_empty(self) -> None:
        with self._lock:
            fts_count = self._conn.execute("SELECT count(*) FROM articles_fts").fetchone()[0]
            art_count = self._conn.execute("SELECT count(*) FROM articles").fetchone()[0]
            if art_count and not fts_count:
                self._conn.execute(
                    "INSERT INTO articles_fts (title, summary, content, url_hash) "
                    "SELECT title, summary, content, url_hash FROM articles"
                )
                self._conn.commit()

    # ------------------------------------------------------- articles
    def upsert_article(self, article: dict[str, Any]) -> tuple[bool, str]:
        """写入文章。返回 (是否新增, 去重原因)。

        三道去重：URL 归一化命中 → content_hash 命中的其他 URL → 新增。
        """
        url = normalize_url(article["url"])
        key = url_key(url)
        ckey = content_key(article.get("title", ""), article.get("content", ""))
        now = _utcnow()

        with self._lock:
            row = self._conn.execute(
                "SELECT url_hash, content_hash FROM articles WHERE url_hash=?", (key,)
            ).fetchone()
            if row is not None:
                # 已知 URL：仅补齐此前缺失的正文（RSS 摘要 → 全文）
                if article.get("content") and not self._row_has_content(key):
                    self._conn.execute(
                        "UPDATE articles SET content=?, content_hash=?, fetched_at=? "
                        "WHERE url_hash=?",
                        (article["content"], ckey, now, key),
                    )
                    self._fts_update(key, article)
                    self._conn.commit()
                    return False, "url_exists_content_filled"
                return False, "url_exists"

            dup = self._conn.execute(
                "SELECT url, title FROM articles WHERE content_hash=? LIMIT 1", (ckey,)
            ).fetchone()
            if dup is not None:
                return False, f"syndicated_duplicate_of:{dup['url']}"

            self._conn.execute(
                "INSERT INTO articles (url_hash, url, source, title, summary, content, "
                "author, published_at, fetched_at, lang, commodities, regions, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key, url, article.get("source", "unknown"), article.get("title", ""),
                    article.get("summary", ""), article.get("content", ""),
                    article.get("author", ""), article.get("published_at"),
                    now, article.get("lang", "en"),
                    json.dumps(article.get("commodities", []), ensure_ascii=False),
                    json.dumps(article.get("regions", []), ensure_ascii=False),
                    ckey,
                ),
            )
            if self.fts_enabled:
                self._conn.execute(
                    "INSERT INTO articles_fts (title, summary, content, url_hash) VALUES (?,?,?,?)",
                    (article.get("title", ""), article.get("summary", ""),
                     article.get("content", ""), key),
                )
            self._conn.commit()
        return True, "inserted"

    def _row_has_content(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT length(content) AS n FROM articles WHERE url_hash=?", (key,)
        ).fetchone()
        return bool(row and row["n"])

    def _fts_update(self, key: str, article: dict[str, Any]) -> None:
        if not self.fts_enabled:
            return
        self._conn.execute("DELETE FROM articles_fts WHERE url_hash=?", (key,))
        self._conn.execute(
            "INSERT INTO articles_fts (title, summary, content, url_hash) VALUES (?,?,?,?)",
            (article.get("title", ""), article.get("summary", ""),
             article.get("content", ""), key),
        )

    def get_article(self, url: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM articles WHERE url_hash=?", (url_key(url),)
            ).fetchone()
        return dict(row) if row else None

    def set_article_content(self, url: str, content: str, summary: str = "") -> bool:
        key = url_key(url)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE articles SET content=?, content_hash=?, fetched_at=? "
                "WHERE url_hash=? AND length(content) < ?",
                (content, content_key(url, content), _utcnow(), key, len(content)),
            )
            self._conn.commit()
            if cur.rowcount and self.fts_enabled:
                row = self._conn.execute(
                    "SELECT title, summary FROM articles WHERE url_hash=?", (key,)
                ).fetchone()
                self._conn.execute("DELETE FROM articles_fts WHERE url_hash=?", (key,))
                self._conn.execute(
                    "INSERT INTO articles_fts (title, summary, content, url_hash) "
                    "VALUES (?,?,?,?)",
                    (row["title"] if row else "", summary or (row["summary"] if row else ""),
                     content, key),
                )
                self._conn.commit()
        return bool(cur.rowcount)

    def search_articles(
        self,
        query: str,
        *,
        days: int | None = None,
        limit: int = 20,
        sources: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """关键词/短语检索，按发布时间倒序。FTS5 优先，失败退化为 LIKE。"""
        clauses: list[str] = []
        params: list[Any] = []

        if days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 0))).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            clauses.append("(a.published_at IS NULL OR a.published_at >= ?)")
            params.append(cutoff)
        if sources:
            placeholders = ",".join("?" for _ in sources)
            clauses.append(f"a.source IN ({placeholders})")
            params.extend(sources)

        terms = _tokenize_query(query)
        has_cjk = any(_has_cjk(term) for term in terms)
        # 关键取舍：SQLite 的 unicode61 分词器不切分中文，因此整段中文会被当成
        # 一个 token（"锂矿政策" ≠ "锂矿"+"政策"），FTS 对中文查询会 0 命中。
        # 中文/混合查询因此直接走 LIKE 子串匹配（本场景数据量小，性能无虞）；
        # 纯英文查询继续走 FTS5 + bm25 排序，保留词干与相关性排序能力。
        if terms and self.fts_enabled and not has_cjk:
            match = " OR ".join(f'"{t}"' for t in terms)
            sql = (
                "SELECT a.*, bm25(articles_fts) AS rank FROM articles_fts "
                "JOIN articles a ON a.url_hash = articles_fts.url_hash "
                "WHERE articles_fts MATCH ?"
            )
            args: list[Any] = [match]
            if clauses:
                sql += " AND " + " AND ".join(clauses)
            sql += " ORDER BY a.published_at DESC, rank LIMIT ?"
            args.extend(params)
            args.append(limit)
            try:
                with self._lock:
                    rows = self._conn.execute(sql, args).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError as exc:
                log.warning("FTS query failed (%s), falling back to LIKE", exc)

        if terms:
            like = " OR ".join(
                ["a.title LIKE ? OR a.summary LIKE ? OR a.content LIKE ?"] * len(terms)
            )
            clauses.append(f"({like})")
            for term in terms:
                params.extend([f"%{term}%"] * 3)
        sql = "SELECT a.* FROM articles a"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY a.published_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_articles(self, days: int | None = None) -> int:
        with self._lock:
            if days is None:
                return self._conn.execute("SELECT count(*) FROM articles").fetchone()[0]
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            return self._conn.execute(
                "SELECT count(*) FROM articles WHERE published_at >= ?", (cutoff,)
            ).fetchone()[0]

    # --------------------------------------------------------- prices
    def upsert_prices(self, rows: Iterable[dict[str, Any]]) -> int:
        now = _utcnow()
        written = 0
        with self._lock:
            for row in rows:
                self._conn.execute(
                    "INSERT INTO price_points (commodity, trade_date, source, price, unit, "
                    "currency, open, high, low, close, volume, fetched_at, is_proxy) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(commodity, trade_date, source) DO UPDATE SET "
                    "price=excluded.price, open=excluded.open, high=excluded.high, "
                    "low=excluded.low, close=excluded.close, volume=excluded.volume, "
                    "fetched_at=excluded.fetched_at",
                    (
                        row["commodity"], row["trade_date"],
                        # source 是主键的一部分，缺失时给出明确缺省值而不是让 SQLite 报错
                        row.get("source") or "unspecified",
                        row["price"],
                        row.get("unit", ""), row.get("currency", "USD"),
                        row.get("open"), row.get("high"), row.get("low"), row.get("close"),
                        row.get("volume"), now, int(bool(row.get("is_proxy", False))),
                    ),
                )
                written += 1
            self._conn.commit()
        return written

    def get_price(self, commodity: str, trade_date: str | None = None,
                  source: str | None = None) -> dict[str, Any] | None:
        clauses = ["commodity = ?"]
        params: list[Any] = [commodity]
        if trade_date:
            clauses.append("trade_date <= ?")
            params.append(trade_date)
        if source:
            clauses.append("source = ?")
            params.append(source)
        sql = (
            "SELECT * FROM price_points WHERE " + " AND ".join(clauses)
            + " ORDER BY trade_date DESC LIMIT 1"
        )
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def get_series(self, commodity: str, days: int, source: str | None = None) -> list[dict[str, Any]]:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        clauses = ["commodity = ?", "trade_date >= ?"]
        params: list[Any] = [commodity, cutoff]
        if source:
            clauses.append("source = ?")
            params.append(source)
        sql = (
            "SELECT * FROM price_points WHERE " + " AND ".join(clauses)
            + " ORDER BY trade_date ASC"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def latest_price_date(self, commodity: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT max(trade_date) AS d FROM price_points WHERE commodity=?", (commodity,)
            ).fetchone()
        return row["d"] if row and row["d"] else None

    # ----------------------------------------------------- http cache
    def cache_get(self, key: str, ttl_s: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM http_cache WHERE cache_key=?", (key,)
            ).fetchone()
        if not row:
            return None
        if ttl_s >= 0 and time.time() - row["stored_at"] > ttl_s:
            return None
        return {
            "status": row["status"],
            "body": row["body"],
            "headers": json.loads(row["headers"] or "{}"),
            "age_s": int(time.time() - row["stored_at"]),
        }

    def cache_put(self, key: str, url: str, status: int, body: str,
                  headers: dict[str, str] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO http_cache (cache_key, url, status, body, headers, stored_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "status=excluded.status, body=excluded.body, headers=excluded.headers, "
                "stored_at=excluded.stored_at",
                (key, url, status, body, json.dumps(headers or {}, ensure_ascii=False),
                 time.time()),
            )
            self._conn.commit()

    @staticmethod
    def cache_key(method: str, url: str, params: dict[str, Any] | None = None) -> str:
        basis = f"{method.upper()}|{url}|{json.dumps(params or {}, sort_keys=True)}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()

    # -------------------------------------------------------- evolution
    def log_evolution(self, stage: str, reason: str, *, tool: str | None = None,
                      severity: str = "warning", payload: dict[str, Any] | None = None) -> None:
        """自省日志：每次降级/失败都留痕，供复跑时做 few-shot 改进（题 #3 同源思路）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO evolution (ts, stage, tool, severity, reason, payload) "
                "VALUES (?,?,?,?,?,?)",
                (_utcnow(), stage, tool, severity, reason,
                 json.dumps(payload or {}, ensure_ascii=False, default=str)),
            )
            self._conn.commit()

    def read_evolution(self, limit: int = 100, stage: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if stage:
                rows = self._conn.execute(
                    "SELECT * FROM evolution WHERE stage=? ORDER BY id DESC LIMIT ?",
                    (stage, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM evolution ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _tokenize_query(query: str) -> list[str]:
    """把自然语言查询切成检索词：英文按非字母数字切，中文按 2-gram 兜底。

    中文无空格，2-gram 是无需分词器的最小可用方案（"锂矿政策" → 锂矿/矿政/政策），
    配合 LIKE 子串匹配即可稳定召回。
    """
    tokens: list[str] = []
    buf: list[str] = []
    for ch in query:
        if ch.isalnum() or ch == "-":
            buf.append(ch)
        else:
            if buf:
                tokens.append("".join(buf))
                buf = []
    if buf:
        tokens.append("".join(buf))

    cleaned = [t for t in tokens if len(t) > 1 and t.lower() not in _STOPWORDS]
    grams: list[str] = []
    for token in cleaned:
        if _has_cjk(token) and len(token) >= 2:
            grams.extend(token[i:i + 2] for i in range(len(token) - 1))
    cleaned.extend(grams)
    # 单字中文查询也要能用（如 "铜"）
    for token in tokens:
        if len(token) == 1 and _has_cjk(token):
            cleaned.append(token)
    return list(dict.fromkeys(cleaned))


_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "what", "how", "are",
    "was", "were", "has", "have", "any", "does", "did", "about", "into", "over",
    "today", "news", "report",
}
