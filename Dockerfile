FROM python:3.12-slim

# 腾讯云镜像源加速：apt（Debian bookworm deb822 源）
RUN sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources

# OpenCV/PaddlePaddle runtime deps: libgl1 -> libGL.so.1, libglib2.0-0 -> OpenCV, libgomp1 -> PaddlePaddle OpenMP
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple uv

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
