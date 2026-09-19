# 极简 Makefile：评审只要记住 `make demo` 一条命令。
# Windows 用户可用 `make`（Git Bash/WSL）或直接照 RUN.md 里的等价 PowerShell 命令。

PY ?= python

.PHONY: help setup fixtures verify test demo mcp-config compose clean

help:
	@echo "make setup      安装依赖并生成合成样例 PDF 与配置"
	@echo "make check      环境自检：能否跑 Demo / 测试，缺什么包"
	@echo "make fixtures   仅生成合成 NI 43-101 样例与 ground truth"
	@echo "make verify     三个 MCP server 的关键工具端到端验证（离线可跑）"
	@echo "make test       pytest 全量测试（经 run_tests.py 屏蔽全局 user site）"
	@echo "make demo       离线生成一份 Pilbara 锂矿日报"
	@echo "make mcp-config 重新生成 mcp-config.json 与 docker-compose.yml"
	@echo "make compose    用 docker compose 起三个 MCP server"

setup:
	$(PY) -m pip install -r requirements.txt
	$(PY) scripts/make_sample_pdfs.py
	$(PY) scripts/make_configs.py

check:
	$(PY) scripts/check_env.py

fixtures:
	$(PY) scripts/make_sample_pdfs.py

verify:
	$(PY) scripts/verify_tools.py

test:
	$(PY) scripts/run_tests.py

demo:
	$(PY) -m agent.cli --offline --topic "Pilbara 锂矿"

mcp-config:
	$(PY) scripts/make_configs.py

compose:
	docker compose up -d --build

clean:
	rm -rf data/cache data/logs data/briefings .tmp-* __pycache__
