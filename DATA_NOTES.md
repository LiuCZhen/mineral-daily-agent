# DATA_NOTES.md — schema / 字段 / 主键 / 去重策略

本文档对应题面交付清单中的 `DATA_NOTES.md`，说明数据模型、去重与降级规则。
所有持久化都在单个 SQLite 文件中（默认 `data/cache/store.sqlite3`，路径可用 `MDA_CACHE_DIR` 覆盖），
启用 WAL 模式，便于 MCP server 子进程并发访问。

---

## 1. 表结构

### 1.1 `articles` — 新闻与政策网页

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `url_hash` | TEXT **PK** | `sha1(normalize_url(url))`，见 §2.1 |
| `url` | TEXT | 归一化后的 URL |
| `source` | TEXT | 来源名（RSS 频道标题或域名） |
| `title` | TEXT | 标题 |
| `summary` | TEXT | RSS/OG 摘要，或正文前若干字符 |
| `content` | TEXT | 正文（优先一手抓取；否则为空） |
| `author` | TEXT | 作者（可得时） |
| `published_at` | TEXT | **UTC `YYYY-MM-DDTHH:MM:SS`**，统一格式便于字符串比较 |
| `fetched_at` | TEXT | 入库时间（UTC） |
| `lang` | TEXT | `en` / `zh`，按正文是否含 CJK 判定 |
| `commodities` | TEXT(JSON) | 品种标签数组，如 `["lithium"]` |
| `regions` | TEXT(JSON) | 地区标签数组，如 `["AU"]` |
| `content_hash` | TEXT | `sha1(title + 正文前 2000 字符)`，见 §2.2 |

索引：`published_at DESC`（时间窗检索）、`source`（按源过滤）、FTS5 虚表 `articles_fts`
（`title, summary, content`，`unicode61` 分词）。FTS5 不可用时自动退化为 `LIKE` 检索。

### 1.2 `price_points` — 行情日线

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `commodity` | TEXT **PK₁** | 标准化品种 ID（`copper` / `lithium_carbonate` / `spodumene_sc6` / `iron_ore_62fe` …） |
| `trade_date` | TEXT **PK₂** | `YYYY-MM-DD`（交易日，非自然日；周末与节假日无点） |
| `source` | TEXT **PK₃** | 数据源标识（`stooq` / `unspecified`(离线夹具) …） |
| `price` | REAL | 收盘/结算价 |
| `unit` | TEXT | `USD/t`、`USD/oz` 等 |
| `currency` | TEXT | 币种，默认 `USD` |
| `open/high/low/close/volume` | REAL | 可空 |
| `fetched_at` | TEXT | 入库时间 |
| `is_proxy` | INTEGER | 1 = 代理指标（如 COMEX 铜期货代理 LME 铜） |

索引：`(commodity, trade_date DESC)`。

### 1.3 `http_cache` — 原始响应缓存

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cache_key` | TEXT **PK** | `sha1(method + url + sorted(params))` |
| `url` | TEXT | 请求 URL |
| `status` | INTEGER | HTTP 状态码 |
| `body` | TEXT | 原始响应体 |
| `headers` | TEXT(JSON) | 响应头（含 `ETag` / `Last-Modified`，便于后续做条件请求） |
| `stored_at` | REAL | Unix 时间戳，用于 TTL 判定 |

### 1.4 `evolution` — 自省日志

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER **PK AUTOINCREMENT** | |
| `ts` / `stage` / `tool` / `severity` | TEXT | stage 取值：`tool_call`、`plan`、`briefing_revise`、`briefing_published` |
| `reason` | TEXT | 失败/降级的可读原因 |
| `payload` | TEXT(JSON) | 上下文（参数、轮次、问题清单） |

用途：每次工具失败与每轮补采都留痕；`briefing_revise` 记录「哪一轮因为什么问题补采了什么」，
可直接复跑这条 log 做 few-shot 改进（与题 #3 的 `evolution.jsonl` 同源思路）。

---

## 2. 去重策略（三道闸）

### 2.1 第一道：URL 归一化

`normalize_url()` 做四件事：

1. 丢弃 fragment（`#...`）；
2. scheme 与 host 转小写（`HTTPS://Mining.com` → `https://mining.com`）；
3. 去掉跟踪参数：`utm_*`、`gclid`、`fbclid`、`mc_cid`、`mc_eid`、`ref`、`spm`；
4. 剩余 query 按 key 排序，path 去掉尾部 `/`。

结果哈希成 `url_hash` 作为主键。**同一篇文章被多个 feed（含带跟踪参数的变体）收录时只入库一次。**

### 2.2 第二道：内容指纹（转载/联合供稿）

`content_key()` = `sha1(title.lower() + 正文前 2000 字符.lower())`。
命中已有记录时判定为 `syndicated_duplicate_of:<原URL>`，**不新增行**，
避免「同一报道被 5 家媒体转载 → 检索结果被同一件事刷屏」。

### 2.3 第三道：同 URL 正文补全

RSS 只给摘要时先入库摘要；后续 `fetch_article` 拿到全文时，
仅当新正文更长才 `UPDATE`（`set_article_content` 带 `length(content) < ?` 条件），
避免短摘要覆盖长正文。

### 2.4 行情去重

主键 `(commodity, trade_date, source)` + `ON CONFLICT DO UPDATE`：
同一品种同一天同一源重复抓取只更新价格，**不产生重复行**；
不同源同日数据并存（保留冲突可见性，而不是静默覆盖）。

---

## 3. 字段口径与单位

| 概念 | 口径 | 备注 |
| --- | --- | --- |
| `tonnes_mt` | 百万吨（Mt） | 报告中以 kt 表述时自动 ÷1000 归一 |
| `grade_unit` | `g/t` / `%` / `ppm` | 等价品位（CuEq/AuEq）**不作为** `grade`，另存 `grade_is_equivalent` |
| `metal_tonnes` | 公吨 | 由 `metal_value × 单位换算系数` 得到，换算表见 `extractor.METAL_UNIT_TO_TONNE`（oz=31.1034768 g、Mlb=453.59237 t…） |
| `included_in` | 嵌套关系 | NI 43-101 常把 Inferred 写成含于 Indicated 的增量，**不可相加** |
| `is_total` | 合计行 | 合计行单独存放，不参与分类取值 |
| 缺失值 | `null` | 报告中写 "—" / "Nil" 一律为 `null`；**任何环节都不补 0** |

---

## 4. 降级与来源标注（provenance）

每个工具返回统一信封：

```json
{
  "tool": "lme-price-mcp.get_trend",
  "data": { "...": "业务字段" },
  "provenance": { "sources": [...], "degraded": true, "confidence": 0.45 },
  "warnings": [{ "code": "fallback_source", "message": "...", "severity": "warning" }],
  "error": null
}
```

- `degraded=true` 表示**没有拿到一手数据**，取值来源见 `sources[].type`：
  `live`（实时） / `cache`（缓存，含 `age_s`） / `offline_fixture`（合成样例） /
  `store`（库内既有） / `local_file`（本地报告）。
- `confidence` 是工具自评的把握度（0–1）：读取失败、缓存过期、代理指标、字段缺失都会降低它；
  储量抽取中若低于 `MDA_ABSTAIN_BELOW`（默认 0.55）则直接 `abstain`。
- 简报的「数据可靠性声明」章节由这些字段驱动，**不允许 Agent 自行判断是否披露**。

---

## 5. 已知边界（如实声明，不掩盖）

1. **合成样例**：`data/fixtures/*.pdf` 与 `data/fixtures/offline/*` 全部是合成数据，
   用于离线演示与回归测试，不是真实新闻/行情/矿权信息；任何使用它们的结果都会被标注。
2. **代理指标**：LME 铜用 COMEX 期货代理，锂盐/锂辉石/铁矿石使用合成代理序列；
   单位与价差与真实报价存在系统性差异，`is_proxy=true` + `proxy_note` 已标注。
3. **PDF 文本层**：自研解析器覆盖文本型 PDF。两类失败会被**显式 abstain**，
   不输出任何数值（这是题面最看重的行为）：
   - **扫描件**（无文本层）→ `reason=scanned_pdf_without_text_layer`，提示需要 OCR；
   - **字体编码无法还原**（有文本但解出噪声）→
     `reason=text_layer_undecodable_font_encoding`。

   真实案例（已复现）：SEC 上某发行人技术报告用 8 个 Type0/Identity-H 子集字体，
   每个都带 ToUnicode，但内容流字节按声明码宽解码后得到
   `rAp p$T"o®68` 这类噪声。**注意：这种噪声的统计特征与正常文本几乎重合**
   （printable 0.979 vs 0.978、space 0.151 vs 0.147、alnum 0.94 vs 0.95），
   因此判定最终依赖「领域词检查」：矿产报告解码成功必然出现
   resource/grade/tonnes/indicated 等词，一个都没有即判为解码失败。
4. **中文检索**：SQLite `unicode61` 分词器不切分中文，故中文/混合查询走 2-gram + `LIKE`
   子串匹配（数据量小，性能无虞），纯英文查询走 FTS5 + bm25 排序。
5. **未接入的源**：LME / SHFE / 上海钢联官方接口需登录或授权，本实现未包含凭据管理，
   仅提供 `MDA_LME_API_KEY` / `MDA_METALS_API_KEY` 两个预留位与降级路径。

## 6. 官方数据源的 User-Agent 要求（实测，两者正好相反）

| 源类型 | 要求 | 配置项 |
| --- | --- | --- |
| 商业媒体（mining.com 等） | **浏览器 UA** 可用；自报家门的爬虫 UA 返回 403（Akamai） | `MDA_USER_AGENT`（默认已是浏览器 UA） |
| 官方源（SEC.gov / EDGAR） | **必须声明式 UA**（含联系方式）；浏览器 UA 返回 403 *"Undeclared Automated Tool"* | `MDA_DECLARED_USER_AGENT` |

`servers/common/http.py::user_agent_for()` 按域名自动选用，因此同一个下载函数
既能取商业站点的 PDF，也能取 SEC 的 PDF。实测证据：

```
SEC 规范 UA(带联系方式)  status=200  bytes=221918  magic=b'%PDF-'
默认浏览器 UA            status=403  bytes=4817    magic=b'<!DOC'
```

> 复现：`scripts/check_real_43_101.py` 用 SEC EDGAR 全文检索找到真实的
> NI 43-101 附件（如 IAMGOLD 2022-02-24 提交的 EX-99.1 PDF），并逐个探测可下载性。
> 注意 EDGAR 全文检索要求 UA 声明联系方式，否则返回 403。

## 7. 真实新闻源实测可用性（2026-09，用 `scripts/check_network.py` 复现）

| 源 | 状态 | 处置与理由 |
| --- | --- | --- |
| `mining.com/feed/`<br>`mining.com/category/critical-minerals/feed/` | ✅ 200，各 36 条 | 仅在**浏览器 UA** 下可用；爬虫 UA 返回 403（`Server: AkamaiGHost`）。故 `MDA_USER_AGENT` 默认改为浏览器 UA |
| `northernminer.com/feed/` | ✅ 200，20 条 | 实测稳定，已加入默认源作为兜底 |
| `spglobal.com/marketintelligence/en/rss/all` | ❌ 403 | Akamai 拦截，**换浏览器 UA 亦无效**；保留在 `MDA_NEWS_FEEDS` 中以暴露真实降级路径，不假装可用 |
| `stooq.com` 日线 CSV | ⚠️ 曾出现 200 + 0 字节 | 属网络网关拦截，与解析器无关；预检脚本已将「空 body」与「CSV 列名不符」分开报告 |
| 锂盐 / 锂辉石 / 铁矿石现货 | ⚠️ 无免费公开源 | 使用合成代理序列并标注 `is_proxy=true`；LME/SHFE/钢联官方接口需授权 |

> 复现命令：`python scripts/check_network.py --only feeds` 与
> `python scripts/check_user_agent.py`。后者对同一 URL 用两种 UA 各请求一次，
> 输出状态码、`Server` 头和 Cloudflare/Akamai 特征串，直接判定「换 UA 可解」
> 还是「IP/WAF 级限制，需换源」。
