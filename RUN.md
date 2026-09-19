# RUN.md — 5 分钟跑起来

> 题面要求：`RUN.md` — 我们能在 5 分钟内跑起来（含一条 docker-compose）。
> 本文件给两条路径：**路径 A：本地零依赖**（约 90 秒）与 **路径 B：docker compose**。
> 两条路径都先在**离线模式**下自证，再说明如何切到联网取真实数据。

---

## 0. 前置条件

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.10+（开发环境 3.13） | **无任何第三方依赖**：MCP 协议栈与 PDF 文本层都是自研纯标准库实现 |
| Docker | 可选 | 仅路径 B 需要；已验证 Docker 29.x |
| 网络 | 可选 | 离线也能跑通全流程（见下文降级设计） |

> 为什么零依赖：MCP 的 stdio 传输本质是换行分隔的 JSON-RPC 2.0；
> 把协议栈自研后，`git clone` 即可运行，不需要 `pip install`，评审不会卡在装包上。

### 0.1 解释器怎么选（IDE 用户必读）

**推荐：直接把项目解释器指向已有的 Python 3.10+ 环境，不要新建 venv。**

- 真正必需的依赖只有 3 个：`httpx`（联网抓取 + 调 LLM）、`reportlab`（生成合成样例 PDF）、
  `pytest`（测试）。若现有环境已具备，**无需任何 pip install 即可跑通全流程**；
  离线 demo 连 `reportlab` 都可以不要（样例 PDF 已在 `data/fixtures/` 里）。
- 开发机上验证过的可用解释器：`C:\AITool\Anaconda3-2025\python.exe`（Python 3.13.5）。

PyCharm 常见坑：新建项目时弹出的 **"Creating Virtual Environment"** 对话框，
默认 Base interpreter 可能指向 `AppData\Local\Programs\Python\Python37`（3.7）。
用它装不上 `requirements.txt`（`mcp` 要求 3.10+、`pytest 8` 要求 3.8+）。
若要建 venv，请把 **Base interpreter 改成 Python 3.10+**。

> 代码本体兼容性：各文件顶部都有 `from __future__ import annotations`，
> 所以 `X | None` 注解在 3.7 也能解析；限制来自**依赖包**的版本要求，不是语法。

### 0.2 受限环境下的 venv 与 TEMP

`python -m venv` 会调用 `ensurepip`，后者需要把 pip wheel **解包到系统 TEMP 目录**。
在 TEMP 被重定向到不可枚举目录的环境（某些沙箱/受控终端）里，`ensurepip` 会以
`PermissionError: [WinError 5]` 失败，导致 venv 建出来**没有 pip**。

- 普通用户会话不受影响，可正常建 venv；
- 若遇到该错误且无法改 TEMP：改用 `python -m venv --without-pip <dir>`，
  或直接用系统解释器（推荐，见 0.1）。

> 补充：`pip install` 也可能因镜像源不可达而长时间卡住。本机实测阿里云镜像会卡死，
> 因此本交付把「能不能在评审机上装包」从关键路径上彻底移除了。

---

## 路径 A：本地零依赖（推荐，约 90 秒）

在项目根目录 `mineral-daily-agent/` 下执行。

### A0. 先自检环境（约 2 秒，强烈建议）

```bash
python scripts/check_env.py          # 加 --json 可拿机器可读结果
```

它会真实 import 每个依赖（不是只看文件在不在），并直接告诉你：

- 当前解释器版本是否 ≥3.10；
- `httpx`（必需）/ `pytest` / `reportlab` / `lxml` 各自的状态与用途；
- 合成样例、三个 server 的 import 是否正常，共暴露几个工具；
- **「离线上跑 Demo」和「跑完整测试套件」分别可不可以**；
- 缺什么就给对应的补装命令（conda 与 pip 两种写法）。

输出示例（环境就绪时）：

```
[标准库] 通过
[第三方依赖]
  httpx       已安装 0.28.1    — HTTP 客户端：抓取新闻/价格/PDF、调用 LLM
  pytest      已安装 8.3.4     — 运行测试套件
  reportlab   已安装 4.5.1     — 生成合成 NI 43-101 样例 PDF（样例已随仓库提供，可跳过）
[三个 server import] 通过，共暴露 10 个工具
离线上跑 Demo      : 可以
跑完整测试套件      : 可以
```

> **依赖的最小边界**（实测）：跑 Demo 只需 `httpx`（离线 demo 连它都不是必需的，
> 因为夹具已随仓库提供）；跑测试需要 `pytest`；`reportlab` 仅用于
> `scripts/make_sample_pdfs.py` 生成样例与一条「扫描件 abstain」测试。
> 因此「新建干净环境后要装几个包」这件事有确定答案，不必猜。

### A1. 生成合成 NI 43-101 样例与配置（约 5 秒）

```bash
python scripts/make_sample_pdfs.py     # 生成 3 份合成 NI 43-101 PDF + ground truth
python scripts/make_configs.py         # 生成 mcp-config.json + docker-compose.yml
```

> **为什么是合成样例**：题面承诺提供 3 份真实 NI 43-101 PDF（Newmont / Barrick / Pilbara）。
> 本仓库不附带任何第三方版权文件，因此提供**等价的合成样例**：表格布局、单位混用
> （g/t、%、oz、Mlb）、合计行、Inferred 嵌套、分页续表、CuEq 等价品位干扰、
> 字段缺失（"—"）这些真实难点**全部复刻**。换成真实 PDF 只需把 URL 传给工具，代码无需改动。

### A2. 一条命令出日报

Windows PowerShell：

```powershell
$env:MDA_OFFLINE = "1"
python -m agent.cli --topic "给我生成一份关于 Pilbara 锂矿的今日简报"
```

macOS / Linux / Git Bash：

```bash
MDA_OFFLINE=1 python -m agent.cli --topic "给我生成一份关于 Pilbara 锂矿的今日简报"
```

预期：终端打印完整 Markdown 简报，并在 `data/briefings/` 下写出
`briefing-<slug>-<时间戳>.md`（成稿）与同名 `.trace.json`（完整调用轨迹，可复核每个数字来源）。

结尾应看到：

```
自检评分：8.4/10 (通过)
数据降级：是
工具调用：mining-news-mcp.search, mining-news-mcp.fetch_article,
         mineral-pdf-mcp.extract_resources, lme-price-mcp.get_trend
已写入：.../data/briefings/briefing-pilbara-<时间戳>.md
```

### A3. 三个 MCP server 逐个验证（约 20 秒）

```bash
python scripts/mcp_smoke.py            # 握手 + 列出每个 server 暴露的工具
python scripts/verify_tools.py         # 真实调用关键工具，检查返回体
```

`verify_tools.py` 预期输出（离线）：

```
[news.search] ok=True results=2 degraded=True
[news.fetch_article] ok=True content_chars=...
[pdf.extract_resources] ok=True categories=['Indicated', 'Inferred']
[price.get_price] ok=True price=... origin=offline_fixture
[price.get_trend] ok=True change_pct=...
failures=0
```

### A4. 全量测试（约 10 秒）

```bash
python scripts/run_tests.py       # 推荐：等价于 python -m pytest，但会先屏蔽全局 user site
# 或直接：
python -m pytest
```

预期：`77 passed`。测试覆盖 MCP 协议合规、三个 server 的必需工具、
储量抽取 accuracy/abstain、双路交叉校验（LLM 与确定性解析冲突时的处理）、
正文抽取与段落保真、去重与检索、缓存 TTL、Agent 端到端与 Critic 规则。

> **为什么提供 `run_tests.py`**：Windows 上 `%APPDATA%\Python\Python3XX\site-packages`
> 会在 conda 环境之前进入 `sys.path`。若那里装着带 pytest 插件的包（实测：`langsmith`），
> pytest 加载 entry-point 插件时会 import 它，一旦它自身依赖缺失
> （`langsmith → requests → urllib3`）就会在**收集阶段直接崩溃**：
>
> ```
> ModuleNotFoundError: No module named 'urllib3'
> ```
>
> 这与本项目无关。`run_tests.py` 在启动 pytest **之前**设置 `PYTHONNOUSERSITE=1`
> 并把 user site 从 `sys.path` 摘掉，因此不受影响。
> 关键细节：**写在 `conftest.py` 里来不及** —— pytest 先加载 entry-point 插件、
> 后导入 conftest（已实测），所以必须由启动器提前设置。
>
> `pytest.ini` 已内置 `-p no:tmpdir`：本环境下系统 TEMP 不可枚举，
> `tests/conftest.py` 提供了落在 workspace 内的等价 `tmp_path` / `tmp_path_factory`。

### A5. 可选：验证 LLM 路径（无需真实 API key）

```bash
python scripts/verify_llm_path.py
```

预期 `failures=0`。它启动一个本地 mock 的 OpenAI 兼容服务，
故意返回与确定性解析器**冲突**的品位，验证系统会告警、降置信度，
并且**不采信模型的错误值**（这是双路交叉校验存在的意义）。

---

## 路径 B：docker compose

```bash
cp .env.example .env          # 可选：填 LLM key 后即为联网+模型增强模式
docker compose up -d --build  # 起三个 MCP server 容器
docker compose run --rm agent python -m agent.cli --topic "Pilbara 锂矿"
```

`docker-compose.yml` 由 `scripts/make_configs.py` 从 `agent/mcp_registry.py` 生成，
每个 server 一个常驻容器（stdio 协议要求一进程一 server），并挂载 `./data` 持久化
去重库、缓存与简报。

---

## 接到 Claude Desktop / Cursor 验证（题面交付清单第 4 项）

`scripts/make_configs.py` 生成的 `mcp-config.json` 内容形如：

```json
{
  "mcpServers": {
    "mining-news-mcp": {
      "command": "<python 解释器绝对路径>",
      "args": ["-m", "servers.mining_news_mcp.server"],
      "cwd": "<项目根目录绝对路径>"
    },
    "mineral-pdf-mcp": { "command": "...", "args": ["-m", "servers.mineral_pdf_mcp.server"], "cwd": "..." },
    "lme-price-mcp":   { "command": "...", "args": ["-m", "servers.lme_price_mcp.server"],   "cwd": "..." }
  }
}
```

- **Claude Desktop**：把 `mcpServers` 整块并入
  `%APPDATA%\Claude\claude_desktop_config.json`（macOS：`~/Library/Application Support/Claude/`），
  重启后应看到三个 server 与其中的工具（`search`、`fetch_article`、`extract_resources`、
  `get_price`、`get_trend` 等）。
- **Cursor**：并入 MCP 配置中的 `mcpServers` 字段，重载窗口。
- 两个客户端都通过 stdio 拉起子进程，因此 `cwd` 必须是项目根目录（配置里已写绝对路径）。

> 协议实现说明：`initialize` 回显客户端请求的 `protocolVersion` 并声明
> `capabilities.tools`；工具返回同时提供 `content[].text` 与 `structuredContent`，
> 以兼容不同客户端的取值偏好。日志一律写 stderr，stdout 只承载协议消息。

---

## 切到联网模式（真实数据）

```bash
# 默认就是联网模式，无需设置；下面是显式写法
MDA_OFFLINE=0 python -m agent.cli --topic "Pilbara 锂矿"
```

### 先做联网预检（强烈建议）

```bash
python scripts/check_network.py              # 逐源检查连通性、耗时与响应结构
python scripts/check_network.py --only feeds # 只查新闻源
python scripts/check_user_agent.py           # 对比爬虫 UA / 浏览器 UA，判断反爬策略
```

`check_network.py` 会给出每个源的状态码、耗时、字节数、解析出的条目数、
最新发布时间与示例标题，并把失败翻译成可执行结论（403 是反爬、404 是 URL 失效、
200+空 body 是网关拦截——三者修法完全不同）。

**本机实测结果（2026-09，供对照）**：

| 源 | 结果 | 说明 |
| --- | --- | --- |
| `mining.com/feed/` | ✅ 200 / 36 条 | 仅浏览器 UA 可用（见下） |
| `mining.com/category/critical-minerals/feed/` | ✅ 200 / 36 条 | 同上 |
| `northernminer.com/feed/` | ✅ 200 / 20 条 | 实测稳定，已加入默认源作为兜底 |
| `spglobal.com/.../rss/all` | ❌ 403 | Akamai 拦截，**换浏览器 UA 也无效**；保留在列表里以暴露真实降级 |
| `stooq.com`（价格） | ⚠️ 200 / 0 字节 | 本次被网络网关拦截，非 Stooq 格式问题 |
| 锂盐/锂辉石/铁矿石价格 | ⚠️ 合成代理 | 免费公开日线源不存在，属设计上的已知边界 |

### 一个关键的反爬发现：默认 UA 必须像浏览器

题面把源 1 列为「反爬中等」。实测根因很具体：

- 用自报家门的爬虫 UA（`Mozilla/5.0 (compatible; MineralDailyAgent/1.0; ...)`）
  访问 `mining.com/feed/` → **HTTP 403（AkamaiGHost）**；
- 换成浏览器 UA → **HTTP 200，36 条完整条目**。

因此 `MDA_USER_AGENT` 的默认值已改为浏览器 UA，并在 `servers/common/config.py`
里写明了理由。需要以爬虫身份访问时用环境变量覆盖即可：

```bash
MDA_USER_AGENT="MyBot/1.0 (+https://example.com/bot)" python -m agent.cli --topic "Pilbara 锂矿"
```

`scripts/check_user_agent.py` 就是为定位这类问题写的：它对同一 URL 用两种 UA 各请求
一次，输出状态码、响应长度、`Server` 头与 Cloudflare/Akamai 特征串，并直接给出结论
（「403 由 UA 触发，换 UA 即可」vs「两种 UA 均 403，疑似 WAF/IP 级限制，建议换源」）。

### 降级链在联网模式下的实际表现

联网后各数据源的降级链会自动生效：

| 数据源 | 一手路径 | 降级链 |
| --- | --- | --- |
| 矿业新闻 | RSS（mining.com / Northern Miner）+ 正文 HTML 抽取 | 实时 → 新鲜缓存 → 陈旧缓存 → 离线合成语料（显式标注 `[SYNTHETIC]`） |
| 关键矿产政策 | 官网页面（结构不规整 HTML） | 实时 → 缓存 → 摘要降级（识别付费墙/登录墙） |
| 价格 | Stooq 免费日线 CSV（每域名限速 + 退避重试） | 实时 → 库内新鲜 → 陈旧缓存 → 离线合成序列（`is_proxy=true`） |
| NI 43-101 | 报告 PDF URL（自研 PDF 文本层解析） | URL 下载 → 本地样例目录；扫描件 → abstain 并提示需要 OCR |

LME / SHFE / 上海钢联官方接口需要登录或授权，未配置授权时的取值策略见 `DATA_NOTES.md`。

---

## 可选：开启 LLM 增强（Extractor + Critic 双模型）

```bash
cp .env.example .env
# 编辑 .env，至少填 MDA_LLM_API_KEY
```

开启后：

- `mineral-pdf-mcp` 会在**确定性解析器之外**再跑一遍 LLM 抽取（默认 `deepseek-chat`），
  两路结果做字段级交叉校验，冲突则降置信度并写入告警；
- `agent` 会用另一个模型族（默认 `qwen-plus`）为简报写 2–3 句引言，
  并受硬约束：**不得引入材料中没有的数字**；
- 未配置 key 时全部自动走确定性路径，功能不缺失、只是少了交叉校验的第二意见。

---

## 常见问题

**Q：`python -m pytest` 在收集阶段报 `ModuleNotFoundError: No module named 'urllib3'`？**
这是全局 user site-packages 污染，不是本项目的问题：Windows 上
`%APPDATA%\Python\Python3XX\site-packages` 会先于 conda 环境进入 `sys.path`，
其中某个包（实测 `langsmith`）注册了 pytest 插件，而它自身依赖缺失。
**改用 `python scripts/run_tests.py`**（启动前设置 `PYTHONNOUSERSITE=1`）即可，
详见 A4 小节；也可临时用 `$env:PYTHONNOUSERSITE=1` 后再跑 pytest。

**Q：`python -m pytest` 报临时目录权限错误？**
本仓库的 `tests/conftest.py` 已覆盖 `tmp_path` / `tmp_path_factory`：
不使用系统 TEMP，也不使用 `tempfile.mkdtemp`（在某些受限环境下其目录不可写）。

**Q：新闻检索返回 `[SYNTHETIC]` 标注的结果？**
说明实时 RSS 不可达（断网或被拦），系统按降级链使用了明确的合成语料，
并在简报第 0 节「数据可靠性声明」中标注，不会伪装成真实新闻。

**Q：价格怎么和 LME 官网对不上？**
未配置授权时使用的是代理指标（如 COMEX 铜期货代理 LME 铜），返回体里
`is_proxy=true` 且附代理说明，简报表格中也会打上「代理指标」标记。

**Q：想换成自己的矿权/公司？**
编辑 `data/fixtures/projects.json`（首次运行自动生成），把条目指向你的报告路径即可；
Agent 的规划逻辑不需要改动。

**Q：`conda create` 报 `HTTP 403 FORBIDDEN`，包下载不下来？**
现象是 `Collecting package metadata` 与 `Solving environment` 都成功，但下载阶段
每个 `.conda` 文件都返回 403 —— 说明**元数据镜像可达、包文件被镜像侧拒绝**
（镜像同步滞后、限流，或渠道策略变化），与你的网络本身无关。

按代价从低到高依次尝试（三条命令可直接复制）：

```powershell
# 1) 重试（403 有时确实是瞬时的）
conda create -n mineral-daily-agent python=3.13 -y

# 2) 换镜像源（北外 / 中科大）
conda create -n mineral-daily-agent python=3.13 -y --override-channels `
  -c https://mirrors.bfsu.edu.cn/anaconda/cloud/conda-forge `
  -c https://mirrors.ustc.edu.cn/anaconda/cloud/conda-forge

# 3) 绕开 conda 下载：venv + 清华 PyPI（本项目三个依赖在 PyPI 上都有 wheel）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple httpx pytest reportlab
```

**更省事的第四条路**：不要新环境。本项目在 `C:\AITool\Anaconda3-2025`（Python 3.13.5）
的 base 环境中已验证可直接运行（`httpx` / `reportlab` / `pytest` 齐备），
只需把 IDE 解释器指向它即可，零下载。

> 判断是否真的需要新环境，跑一次 `python scripts/check_env.py` 最快：
> 它直接告诉你当前解释器能不能跑 Demo、能不能跑测试。

**Q：新建的 conda/venv 环境里 `python -m agent.cli` 报 `ModuleNotFoundError: httpx`？**
新环境是干净的，需要装依赖。最少一条：
`conda install -c conda-forge httpx -y`（或 `python -m pip install httpx`）。
注意：`reportlab` 只在生成样例 PDF 时需要，而样例已随仓库提供，
所以**没有 reportlab 也能跑 Demo**。

