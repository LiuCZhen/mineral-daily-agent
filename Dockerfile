# 零依赖镜像：本项目所有 server 均为纯标准库实现（MCP 协议栈与 PDF 文本层自研），
# 因此不需要 pip install，构建快、离线可重建。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    MDA_DATA_DIR=/app/data

WORKDIR /app

# 先拷代码（无第三方依赖，故无需 requirements 分层缓存）
COPY . /app

# 运行期数据目录（去重库、缓存、简报、夹具）
RUN mkdir -p /app/data/cache /app/data/logs /app/data/briefings /app/data/fixtures

# 生成合成样例报告（不联网即可完成，便于离线演示与测试）
RUN python scripts/make_sample_pdfs.py

# 默认起一个 server；docker-compose 会为每个 server 覆盖 command
CMD ["python", "-m", "servers.mining_news_mcp.server"]
