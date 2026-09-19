# 矿权日报 — Pilbara Lithium (Pilgangoora)

- **生成时间**：2026-09-18 09:20 UTC
- **主题**：给我生成一份关于 Pilbara 锂矿的今日简报
- **所属国家/地区**：AU
- **主要品种**：lithium
- **数据可靠性**：⚠️ 存在降级数据 （自检评分 8.4/10，通过）
- **工具调用**：7 步，成功 7，失败 0，跳过 0

## 0. 数据可靠性声明

本简报包含**降级数据**：部分数据来自缓存、离线合成序列或代理指标，不得作为交易或披露依据。

- 新闻检索：live_feeds_unreachable; seeded 6 SYNTHETIC sample articles for offline demonstration
- 新闻检索：所有配置的 RSS 源均不可达，结果可能来自缓存或合成样例。
- 储量抽取：以下字段报告中未给出或未能定位，保持 null：Indicated.metal, Inferred.grade, Inferred.metal
- 价格（lithium_carbonate）：未取得一手行情（offline mode），数据来源为 offline_fixture。离线合成序列仅供演示，不是真实行情，禁止作为报价依据。
- 价格（lithium_carbonate）：锂盐现货无免费公开日线源，使用合成代理序列，仅供趋势参考，不可作为报价依据。
- 价格（spodumene_sc6）：未取得一手行情（offline mode），数据来源为 offline_fixture。离线合成序列仅供演示，不是真实行情，禁止作为报价依据。
- 价格（spodumene_sc6）：SC6 现货报价无免费公开源，使用合成代理序列。

**未解决的自检问题（Critic 判定）**：

- `resource_partial`（major）：储量抽取为 partial（部分字段缺失）：这些字段不得在简报中给出数值

## 1. 新闻摘要

1. **Pilbara lithium shipments recover as spodumene prices stabilise** — [example.com](https://example.com/synthetic/pilbara-shipments)（降级数据）
   - mining.com (synthetic sample) [SYNTHETIC] · 2026-09-18T07:26:46 · lithium · AU，CN
   - 摘要：Pilbara Minerals reported higher spodumene concentrate shipments while spot prices steadied after a two-quarter decline.
   - 命中词：pilbara, lithium
2. **Pilbara exploration results extend lithium mineralisation at depth** — [example.com](https://example.com/synthetic/pilbara-exploration)（降级数据）
   - S&P Global (synthetic sample) [SYNTHETIC] · 2026-09-06T07:26:46 · lithium · AU
   - 摘要：Drilling extended high-grade spodumene mineralisation below the current resource pit shell.
   - 命中词：pilbara, lithium
3. **Australia tightens critical minerals investment screening** — [example.com](https://example.com/synthetic/disr-critical-minerals-guidance)（降级数据）
   - DISR (synthetic sample) [SYNTHETIC] · 2026-09-17T07:26:46 · AU
   - 摘要：The Department of Industry, Science and Resources updated guidance on foreign investment in critical minerals projects.
   - 命中词：minerals

**重点文章全文要点**（mining.com (synthetic sample) [SYNTHETIC]，降级数据）：

> Pilbara lithium shipments recover as spodumene prices stabilise
>
> SYNTHETIC SAMPLE ARTICLE — generated for offline testing, not a real news report.
>
> The producer reported higher spodumene concentrate shipments quarter on quarter as
offtake partners in China restocked, while realised prices remained broadly flat
after two consecutive quarters of decline.
>
> Analysts cautioned that Chinese lithium carbonate inventories remain elevated, and
that any price recovery depends on downstream battery demand in the second half of
the year.
>
> On the policy side, updated guidance on foreign investment screening may lengthen
review timelines for offshore bidders in critical minerals projects.
>

## 2. 储量数据（NI 43-101 Indicated / Inferred）

- 抽取状态：⚠️ 部分字段缺失（置信度 0.8）
- 数据来源：`local_file` — C:\Users\臻\Desktop\面试题\mineral-daily-agent\data\fixtures\pilbara_ni43-101_synthetic.pdf（3 页）
- 解析统计：候选行 2 行，识别分类 Indicated，页内扫描 2/3 页，品位×矿量一致率 None

| 分类 | 矿石量 (Mt) | 品位 | 金属量 | 反算金属吨 | 页码 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Indicated | 108.00 | 1.18 % | 缺失 | 缺失 | 2 |
| Inferred（含于 Indicated 之内） | 42.50 | 缺失 | 缺失 | 缺失 | 3 |

**嵌套说明**：

- Inferred 的 42.50 Mt 标注为含于 Indicated 之内，不应与其相加。

> ⚠️ LLM 抽取路径未启用：llm_not_configured（未设置 MDA_LLM_API_KEY，走确定性解析器）
> ⚠️ 以下字段报告中未给出或未能定位，保持 null：Indicated.metal, Inferred.grade, Inferred.metal

## 3. 价格走势（近 30 天）

| 品种 | 起止日期 | 起始 | 最新 | 涨跌幅 | 区间低/高 | 年化波动率 | 方向 | 数据来源 |
| --- | --- | ---: | ---: | ---: | --- | ---: | --- | --- |
| Lithium carbonate (spot, CIF Asia) | 2026-08-10 → 2026-09-18 | 10,662.43 | 11,141.23 | +4.49% | 9,384.01 / 11,141.23 | 43.7% | 上涨 | offline_fixture（降级/代理指标） |
| Spodumene concentrate 6% (SC6) | 2026-08-10 → 2026-09-18 | 1,028.44 | 1,187.45 | +15.46% | 1,028.44 / 1,212.27 | 48.3% | 上涨 | offline_fixture（降级/代理指标） |

**代理指标说明**：

- Lithium carbonate (spot, CIF Asia)：锂盐现货无免费公开日线源，使用合成代理序列，仅供趋势参考，不可作为报价依据。
- Spodumene concentrate 6% (SC6)：SC6 现货报价无免费公开源，使用合成代理序列。

## 4. 风险提示

- **[价格波动]** Lithium carbonate (spot, CIF Asia) 年化波动率 43.7%，储量估值对价格假设高度敏感。
- **[价格波动]** Spodumene concentrate 6% (SC6) 价格近 30 天上涨 +15.46%，需关注对项目经济性的影响。
- **[价格波动]** Spodumene concentrate 6% (SC6) 年化波动率 48.3%，储量估值对价格假设高度敏感。
- **[数据完整性]** Indicated 缺少金属量字段，无法验证品位与矿量的自洽性。
- **[披露口径]** Inferred 资源量以含于 Indicated 的增量形式披露，不可与上级分类直接相加。
- **[数据完整性]** Inferred 缺少金属量字段，无法验证品位与矿量的自洽性。
- **[项目层面]** 锂价波动率高，储量经济性假设对价格敏感
- **[项目层面]** 澳洲关键矿产外资审查与出口政策变动
- **[项目层面]** 中游转化产能集中度带来的议价风险
- **[数据可靠性]** 数据可靠性：新闻检索：所有配置的 RSS 源均不可达，结果可能来自缓存或合成样例。
- **[数据可靠性]** 数据可靠性：价格（lithium_carbonate）：未取得一手行情（offline mode），数据来源为 offline_fixture。离线合成序列仅供演示，不是真实行情，禁止作为报价依据。
- **[数据可靠性]** 数据可靠性：价格（lithium_carbonate）：锂盐现货无免费公开日线源，使用合成代理序列，仅供趋势参考，不可作为报价依据。
- **[数据可靠性]** 数据可靠性：价格（spodumene_sc6）：未取得一手行情（offline mode），数据来源为 offline_fixture。离线合成序列仅供演示，不是真实行情，禁止作为报价依据。
- **[数据可靠性]** 数据可靠性：价格（spodumene_sc6）：SC6 现货报价无免费公开源，使用合成代理序列。

## 5. 引用源

**新闻**

[1] Pilbara lithium shipments recover as spodumene prices stabilise — mining.com (synthetic sample) [SYNTHETIC]（2026-09-18T07:26:46）：https://example.com/synthetic/pilbara-shipments

[2] Pilbara exploration results extend lithium mineralisation at depth — S&P Global (synthetic sample) [SYNTHETIC]（2026-09-06T07:26:46）：https://example.com/synthetic/pilbara-exploration

[3] Australia tightens critical minerals investment screening — DISR (synthetic sample) [SYNTHETIC]（2026-09-17T07:26:46）：https://example.com/synthetic/disr-critical-minerals-guidance

[4] 全文：Pilbara lithium shipments recover as spodumene prices stabilise — https://example.com/synthetic/pilbara-shipments

[5] NI 43-101 报告：C:\Users\臻\Desktop\面试题\mineral-daily-agent\data\fixtures\pilbara_ni43-101_synthetic.pdf（local_file，3 页）

[6] 价格序列来源：offline_fixture（单位/代理说明见表内标注）

## 6. 方法与可复现说明

本简报由 `mineral-daily-agent` 通过 MCP 协议编排三个 server 生成：

| 步骤 | 工具 | 参数 | 状态 | 耗时 |
| --- | --- | --- | --- | ---: |
| 检索新闻：Pilbara lithium | `mining-news-mcp.search` | query=Pilbara lithium, days=14, limit=6 | ok | 20 ms |
| 检索新闻：spodumene | `mining-news-mcp.search` | query=spodumene, days=14, limit=6 | ok | 0 ms |
| 检索新闻：Pilbara Minerals | `mining-news-mcp.search` | query=Pilbara Minerals, days=14, limit=6 | ok | 0 ms |
| 抓取重点文章全文 | `mining-news-mcp.fetch_article` | url=https://example.com/synthetic/pilbara-s…, max_chars=6000 | ok | 23 ms |
| 抽取储量：Pilbara-style NI 43-101 (SYNTHETIC) | `mineral-pdf-mcp.extract_resources` | pdf_url=C:\Users\臻\Desktop\面试题\mineral-daily-ag…, include_raw_rows=False | ok | 18 ms |
| 价格趋势：lithium_carbonate | `lme-price-mcp.get_trend` | commodity=lithium_carbonate, days=30 | ok | 13 ms |
| 价格趋势：spodumene_sc6 | `lme-price-mcp.get_trend` | commodity=spodumene_sc6, days=30 | ok | 4 ms |


所有数值均直接取自工具返回的结构化字段，渲染层不做推算或补全；缺失值一律标注为「缺失」，不填 0。
