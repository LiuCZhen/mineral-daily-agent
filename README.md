# 矿权日报 Agent（mineral-daily-agent）

面试题 #2 的完整实现：**3 个 MCP server + 1 个 Agent client**，输出「矿权日报」Markdown 简报
（新闻摘要 + 储量数据 + 价格走势 + 风险提示 + 引用源链接）。

输入一句话 → 输出一份可复核的简报：

```bash
MDA_OFFLINE=1 python -m agent.cli --topic "给我生成一份关于 Pilbara 锂矿的今日简报"
```

---

## 1. 交付清单对照

| 题面要求 | 本仓库实现 | 位置 |
| --- | --- | --- |
| Agent 主流程（自己设计） | 计划-执行-Critic 补采-渲染四段式显式编排；失败与降级全部留痕 | `agent/loop.py`、`agent/briefing.py` |
| 输入「给我生成一份关于 Pilbara 锂矿的今日简报」 | CLI / Python API 均支持该句式，内置别名解析 | `agent/cli.py`、`agent/registry.py` |
| 输出 Markdown 简报（新闻+储量+价格+风险）+ 引用链接 | 六个章节，含数据可靠性声明与调用轨迹 | `agent/reporter.py` |
| 3 个 MCP server（Python or TS） | Python，且**零第三方依赖** | `servers/` |
| ① `mining-news-mcp`：`search(query, days)` · `fetch_article(url)` | ✅ 另加 `ingest` / `list_sources` | `servers/mining_news_mcp/server.py` |
| ② `mineral-pdf-mcp`：`extract_resources(pdf_url)`（NI 43-101 Indicated/Inferred） | ✅ 另加 `verify_extraction` / `list_sample_reports` | `servers/mineral_pdf_mcp/server.py` |
| ③ `lme-price-mcp`：`get_price(commodity, date)` · `get_trend(commodity, days)` | ✅ 另加 `list_commodities` | `servers/lme_price_mcp/server.py` |
| 1 个 client 端 Agent 编排 | 自写 MCP client（stdio JSON-RPC）+ 计划-执行编排 | `servers/common/mcp_client.py`、`agent/loop.py` |
| `mcp-config.json` 可直接接 Claude Desktop / Cursor | 由 `agent/mcp_registry.py` 生成，含绝对解释器与 cwd | `mcp-config.json` |
| `RUN.md`：5 分钟跑起来 | 两条路径（本地零依赖 ≈90s / docker compose），含预期输出 | `RUN.md` |
| 含一条 docker-compose | 三 server 常驻 + `agent` profile | `docker-compose.yml`、`Dockerfile` |
| （加分）`DATA_NOTES.md` | schema / 字段 / 主键 / 去重策略 / 降级规则 | `DATA_NOTES.md` |

---

## 2. 架构

```
                        ┌──────────────────────────────────────────┐
   自然语言 ──────────▶ │ Agent client（agent/）                    │
 "Pilbara 锂矿今日简报"  │  Planner → Executor → Critic → Renderer   │
                        └───────┬──────────────┬──────────────┬────┘
                                │ MCP stdio    │              │
                   ┌────────────▼───┐  ┌───────▼────────┐  ┌──▼─────────────┐
                   │ mining-news-mcp│  │ mineral-pdf-mcp│  │ lme-price-mcp  │
                   │ search         │  │ extract_       │  │ get_price      │
                   │ fetch_article  │  │  resources     │  │ get_trend      │
                   └───────┬────────┘  └───────┬────────┘  └──┬─────────────┘
                           │                   │              │
                     RSS + 正文抽取      NI 43-101 PDF      行情日线
                           │             文本层解析 +       免费源 + 限速
                           │             LLM 交叉校验       退避重试
                           └───────────────┬──────────────┘
                                           ▼
                        共享层：HTTP(频控/退避) · SQLite(去重/缓存) · LLM · 离线夹具
```

### 2.1 为什么是「显式编排」而不是让模型自由 ReAct

面试场景下最容易被追问的是**结果可不可复核**。所以设计上做了取舍：

- **Planner 是确定性的**：一句主题 → 固定四类步骤（新闻检索 → 全文抓取 → 储量抽取 →
  行情趋势）。主题不在项目目录里时自动泛化（按品种关键词检索），不会崩。
- **模型只在两处参与**：① 配了 key 时跑一遍 LLM 储量抽取做交叉校验；
  ② 为简报写 2–3 句引言，且被硬约束「不得引入材料中没有的数字」。
- **数字只来自工具返回的结构化字段**，渲染层不做任何推算或补全。

### 2.2 Critic 与 Revise Loop

复用题 #3 的「抽取-挑刺-评分」思想，但针对日报的失败模式做了工程化收敛：
日报的主要风险不是抽错字段，而是**某个维度根本没取到数据**。
因此 Critic 是规则化数值/引用核查（可离线、可复现、不花 token），修订动作是**定向补采**：

| Critic 检查 | 规则 | 未过时的补采动作 |
| --- | --- | --- |
| `grade_metal_consistency` | Mt × 品位 ≈ 金属量（±5%） | 带原始行重试抽取 |
| `resource_status` | abstain/partial 不得当 ok 报 | — |
| `citations` | 每条新闻有 URL、每条价格有来源 | 放宽时间窗重检索 |
| `degradation_disclosed` | degraded 数据必须在告警中出现 | — |
| `coverage` | 新闻/储量/价格/风险四维度齐全 | 缺失维度定向补采 |
| `price_consistency` | `change_pct` 与首末价自洽 | 补采更长区间 |

评分：起始 10 分，critical −2.5 / major −1.2 / minor −0.4；**≥8 分通过**，否则进入下一轮
（默认 ≤2 轮）。仍不达标则照常出稿，但在第 0 节列出未解决问题。
每轮都写 `evolution` 表，可复跑做 few-shot 改进。

### 2.3 三个 server 各自的工程重点

| server | 题面痛点 | 本实现的应对 |
| --- | --- | --- |
| `mining-news-mcp` | 全文需爬 + 结构化抽取；政府站点 HTML 不规整 | RSS/Atom 解析 → 正文容器 XPath 选择 → 标准库 `HTMLParser` 兜底；识别付费墙/登录墙并标注正文完整度；两道去重；**实测定位并解决 mining.com 的 UA 反爬（爬虫 UA→403，浏览器 UA→200）** |
| `mineral-pdf-mcp` | 储量表格抽取，最看重「该 abstain 时敢弃权」 | 自研 PDF 文本层 + **cell 版式 / line 版式双路解析**；显式处理跨页续表、合计行、Inferred 嵌套、CuEq 等价品位干扰、单位混用（oz/Mlb/Mt/g·t⁻¹/%）；置信度低于阈值 → `abstain`；配了 key 时与 LLM 结果交叉校验 |
| `lme-price-mcp` | 登录墙 / 接口频控 | 三级降级链（库内新鲜 → 免费源 → 陈旧缓存 → 合成序列）；同域名串行 + 最小间隔 + `Retry-After` 退避；代理指标显式 `is_proxy` 标注 |

### 2.4 真实数据源的实测结论（2026-09）

不是"写完就算"，而是逐源验证过的（`python scripts/check_network.py` /
`scripts/check_user_agent.py`）：

| 源 | 实测结果 | 处置 |
| --- | --- | --- |
| `mining.com/feed/` | 爬虫 UA → **403 (Akamai)**；浏览器 UA → **200 / 36 条** | 默认 UA 改为浏览器 UA，理由写进 `config.py` |
| `northernminer.com/feed/` | 200 / 20 条，稳定 | 加入默认源作为兜底 |
| `spglobal.com/.../rss/all` | 两种 UA 均 **403** | 保留在列表里以暴露真实降级，不假装可用 |
| `stooq.com`（价格） | 曾出现 200 + 0 字节（网关拦截） | 预检脚本把「空 body」与「格式不符」分开报告 |
| 锂盐/锂辉石/铁矿石 | 无免费公开日线源 | 使用**显式标注的合成代理序列**，绝不冒充实盘价 |

> 联网 demo 已实跑：抓到真实头版（如 *Zimbabwe seeks $115 million Afreximbank loan*、
> *Argentina mining exports hit record $4.74 billion*），摘要、品种/地区标签与引用链接均正确，
> 价格章节因 Stooq 被拦而如实标注为降级。

---

## 3. 零依赖的实现取舍（重要）

本机 pip 无法访问镜像源（`pip install` 卡死），而交付要求「5 分钟内跑起来」。
因此两处关键能力改为自研，反而让交付物更强：

| 能力 | 常规做法 | 本仓库做法 | 代价 |
| --- | --- | --- | --- |
| MCP 协议栈 | 官方 `mcp` SDK | `servers/common/mcp_protocol.py`：stdio JSON-RPC 2.0，实现 `initialize` / `tools/list` / `tools/call` / 通知 / 错误码 | 需自行维护协议细节（已用 11 项协议测试覆盖） |
| PDF 文本抽取 | `pdfplumber` / PyMuPDF | `servers/mineral_pdf_mcp/pdf_text.py`：对象扫描、Flate/ASCII85/ASCIIHex 解码、页面树、完整文本算子、ToUnicode CMap | 扫描件不支持 → 明确 `abstain` 并提示需要 OCR |

RSS 解析用标准库 `xml.etree`（未用 `feedparser`），HTML 抽取优先 `lxml`（若存在）、
否则退化为标准库 `HTMLParser`。**没有任何 import 会让程序在缺包时崩**。

> 说明：`requirements.txt` 中的 `mcp` / `pdfplumber` 等列为**可选增强**；
> 装上后 `servers/mineral_pdf_mcp/pdf_text.py` 的解析能力不会自动切换（当前实现已经够用），
> 保留该清单是为了标注「如果允许装包，官方组件可替换哪一层」。
>
> Docker 镜像构建在开发机未完成验证：本机 shell 无法访问 Docker Hub 拉取
> `python:3.12-slim` 基础镜像（`docker compose config` 校验已通过）。

---

## 4. 目录结构

```
mineral-daily-agent/
├── agent/                     # Agent client（题面要求的编排层）
│   ├── cli.py                 # 命令行入口
│   ├── loop.py                # Planner + Executor + Evidence 收集
│   ├── critic.py              # 规则化交叉复核与评分
│   ├── reporter.py            # Markdown 简报渲染
│   ├── briefing.py            # 管线：规划→执行→补采→渲染→落盘
│   ├── registry.py            # 项目目录（主题 → 报告/品种/关键词）
│   └── mcp_registry.py        # 三个 server 的唯一事实来源
├── servers/
│   ├── common/                # 共享层：config/http/store/llm/logging/mcp_protocol/mcp_client/offline
│   ├── mining_news_mcp/       # server.py + feeds.py
│   ├── mineral_pdf_mcp/       # server.py + extractor.py + pdf_text.py
│   └── lme_price_mcp/         # server.py
├── scripts/                   # 环境自检、样例生成、协议冒烟、工具验证、测试入口、调试脚本
├── tests/                     # 77 项测试（协议/工具/抽取/正文/去重/价格/Agent/LLM）
├── data/fixtures/             # 合成 NI 43-101 PDF、ground truth、离线语料
├── mcp-config.json            # 直接粘贴进 Claude Desktop / Cursor
├── docker-compose.yml         # 三 server + agent profile
├── RUN.md                     # 5 分钟跑起来
├── DATA_NOTES.md              # schema / 主键 / 去重 / 降级
└── requirements.txt           # 仅可选增强依赖（不装也能跑）
```

---

## 5. 快速命令

```bash
python scripts/check_env.py           # 环境自检：能否跑 Demo / 测试，缺什么包
python scripts/make_sample_pdfs.py    # 生成合成 NI 43-101 样例 + ground truth
python scripts/make_configs.py        # 生成 mcp-config.json + docker-compose.yml
python scripts/mcp_smoke.py           # 三个 server 握手 + 工具清单
python scripts/verify_tools.py        # 关键工具真实调用验证
python scripts/verify_llm_path.py     # LLM 双路交叉校验（用本地 mock，无需 key）
python scripts/run_tests.py           # 77 passed（已屏蔽全局 user site 干扰）
python -m agent.cli --offline --topic "Pilbara 锂矿"   # 出一份日报
python -m agent.cli --list-projects   # 看内置项目目录
```

`make demo` / `make verify` / `make test` 为等价快捷方式（见 `Makefile`）。

---

## 6. 已知边界

- 三份 NI 43-101 报告与全部离线语料均为**合成数据**，仅用于测试抽取与编排逻辑
  （真实报告直接传 URL 即可，代码无需改动）；
- LME / SHFE / 上海钢联官方接口需授权，未配置时使用免费源或**显式标注的代理指标**；
- 扫描版 PDF 不做 OCR，直接 `abstain`；
- 详情见 `DATA_NOTES.md` 第 5 节。
