FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY app.py extract.py main.py bulk.py ./
COPY static ./static

RUN uv sync --frozen --no-dev

# 模型缓存与字体指向数据卷；目标目录不存在时由启动命令补建（全新机器首次自动下载模型）
RUN mkdir -p /root/.paddlex \
 && ln -s /qn-server-data/paddlex-models /root/.paddlex/official_models \
 && ln -s /qn-server-data/paddlex-fonts /root/.paddlex/fonts

EXPOSE 8000

CMD ["sh", "-c", "mkdir -p /qn-server-data/paddlex-models /qn-server-data/paddlex-fonts && uv run uvicorn app:app --host 0.0.0.0 --port 8000"]
