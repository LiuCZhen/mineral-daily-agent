"""去重 / 检索 / 缓存 / 降级 的单元测试（对应题面「去重策略」与频控要求）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common.store import Store, content_key, normalize_url, url_key  # noqa: E402


def test_url_normalization_strips_tracking_and_fragment() -> None:
    a = "https://Mining.com/Article/x/?utm_source=rss&utm_medium=feed#top"
    b = "https://mining.com/Article/x"
    assert normalize_url(a) == normalize_url(b)
    assert url_key(a) == url_key(b)


def test_url_normalization_keeps_meaningful_query() -> None:
    a = "https://example.com/news?id=42&utm_campaign=x"
    assert "id=42" in normalize_url(a)
    assert "utm_campaign" not in normalize_url(a)


def test_same_url_twice_is_deduplicated(temp_store: Store) -> None:
    article = {"url": "https://example.com/a?utm_source=x", "title": "T", "content": "body"}
    first, reason1 = temp_store.upsert_article(article)
    second, reason2 = temp_store.upsert_article(
        {"url": "https://example.com/a", "title": "T", "content": "body"})
    assert first is True and reason1 == "inserted"
    assert second is False and reason2 == "url_exists"


def test_syndicated_duplicate_detected_by_content_fingerprint(temp_store: Store) -> None:
    base = {"title": "Copper hits record", "content": "Same syndicated text " * 20}
    assert temp_store.upsert_article({**base, "url": "https://a.com/1"})[0] is True
    is_new, reason = temp_store.upsert_article({**base, "url": "https://b.com/2"})
    assert is_new is False
    assert reason.startswith("syndicated_duplicate_of:")


def test_content_fingerprint_ignores_minor_suffix(temp_store: Store) -> None:
    common = {"title": "Lithium auction", "content": "Identical body " * 30}
    temp_store.upsert_article({**common, "url": "https://a.com/1"})
    # 正文前 2000 字符相同 → 视为同一篇（转载），避免重复计数
    assert content_key(common["title"], common["content"]) == content_key(
        common["title"], common["content"])


def test_search_ranks_title_match_above_body_match(temp_store: Store) -> None:
    temp_store.upsert_article({
        "url": "https://example.com/title", "title": "Pilbara lithium expansion",
        "content": "unrelated body", "published_at": "2026-01-02T00:00:00"})
    temp_store.upsert_article({
        "url": "https://example.com/body", "title": "Copper market update",
        "content": "mentions Pilbara lithium once", "published_at": "2026-01-02T00:00:00"})
    rows = temp_store.search_articles("Pilbara lithium", limit=5)
    assert rows, "应能检索到条目"
    assert "title" in rows[0]["url"]


def test_search_respects_day_window(temp_store: Store) -> None:
    from datetime import datetime, timedelta, timezone

    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S")
    temp_store.upsert_article({"url": "https://example.com/new", "title": "nickel recent",
                               "content": "nickel", "published_at": recent})
    temp_store.upsert_article({"url": "https://example.com/old", "title": "nickel old",
                               "content": "nickel", "published_at": old})
    rows = temp_store.search_articles("nickel", days=30, limit=10)
    urls = [row["url"] for row in rows]
    assert "https://example.com/new" in urls
    assert "https://example.com/old" not in urls


def test_chinese_query_tokenization(temp_store: Store) -> None:
    temp_store.upsert_article({
        "url": "https://example.com/cn", "title": "澳洲锂矿出口政策变化",
        "content": "澳洲锂矿出口政策出现调整", "published_at": "2026-01-01T00:00:00"})
    rows = temp_store.search_articles("锂矿 政策", limit=5)
    assert rows, "中文 2-gram 检索应能命中"


def test_http_cache_ttl_behaviour(temp_store: Store) -> None:
    key = Store.cache_key("GET", "https://example.com/x", {"a": 1})
    temp_store.cache_put(key, "https://example.com/x", 200, "payload")
    assert temp_store.cache_get(key, ttl_s=60)["body"] == "payload"
    assert temp_store.cache_get(key, ttl_s=-1)["body"] == "payload"      # 陈旧但可读
    # 人为过期
    import time
    with temp_store._lock:                                              # noqa: SLF001
        temp_store._conn.execute("UPDATE http_cache SET stored_at=? WHERE cache_key=?",
                                 (time.time() - 10_000, key))
        temp_store._conn.commit()
    assert temp_store.cache_get(key, ttl_s=60) is None
    assert temp_store.cache_get(key, ttl_s=-1) is not None


def test_price_upsert_is_idempotent_per_source(temp_store: Store) -> None:
    row = {"commodity": "copper", "trade_date": "2026-01-02", "source": "stooq",
           "price": 9000.0, "unit": "USD/t", "currency": "USD"}
    temp_store.upsert_prices([row])
    temp_store.upsert_prices([{**row, "price": 9100.0}])
    series = temp_store.get_series("copper", days=3650)
    assert len(series) == 1
    assert series[0]["price"] == 9100.0


def test_price_missing_source_does_not_crash(temp_store: Store) -> None:
    """离线夹具的行情点没有 source，必须落成明确的缺省值而不是主键报错。"""
    written = temp_store.upsert_prices([{
        "commodity": "lithium_carbonate", "trade_date": "2026-01-02", "price": 1000.0,
        "unit": "USD/t", "currency": "USD", "is_proxy": True}])
    assert written == 1
    assert temp_store.get_price("lithium_carbonate")["source"] == "unspecified"


def test_evolution_log_round_trip(temp_store: Store) -> None:
    temp_store.log_evolution("tool_call", "boom", tool="x.y", severity="error",
                             payload={"a": 1})
    rows = temp_store.read_evolution(limit=5)
    assert rows and rows[0]["tool"] == "x.y"
    assert '"a": 1' in rows[0]["payload"]
