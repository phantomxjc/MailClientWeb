# MailClient Web —— 多邮箱统一收件箱
# 构建：docker build -t mailclient-web .
# 运行：见 docker-compose.yml（推荐）或 README.md
#
# 两个构建参数（都有合理默认值，一般不用管）：
#   PY_BASE   基础镜像。国内网络连不上 Docker Hub 时，改成镜像站地址即可，例如
#             --build-arg PY_BASE=docker.1panel.live/library/python:3.12-slim
#   PIP_INDEX pip 源。默认走清华镜像，失败会自动回落到官方源。
ARG PY_BASE=python:3.12-slim
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

FROM ${PY_BASE}

# ARG 声明在 FROM 之前时，只对 FROM 那一行生效；想在 RUN 里用必须再声明一次
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    APP_HOST=0.0.0.0 \
    APP_PORT=8090 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，代码改动时能复用这一层缓存。
# 先用国内 pip 源（快且稳），失败再退回官方源 —— 只装三个包，两种都够用。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -i ${PIP_INDEX} --timeout 60 \
    || pip install --no-cache-dir -r requirements.txt --timeout 60 \
    ; rm -rf /var/cache/apt /var/lib/apt/lists/*

COPY . .
RUN mkdir -p /data

# 说明：这里以 root 运行，是为了让「把宿主机目录挂进 /data」这种最常见的
# NAS 部署方式开箱可用（宿主机目录常属 root，非 root 用户会写不进去）。
# 若你想收紧权限，可取消下面两行注释，并确保宿主机 data 目录属主为 1000:1000：
# RUN useradd -m -u 1000 mailuser && chown -R mailuser:mailuser /app /data
# USER mailuser

# 镜像元数据：记上作者与出处，`docker inspect` 就能反查到项目
LABEL org.opencontainers.image.title="MailClient Web" \
      org.opencontainers.image.description="多邮箱统一收件箱（Web 版）。后续更新详情请关注微信公众号「软件推手」" \
      org.opencontainers.image.authors="软件推手（phantomxjc）" \
      org.opencontainers.image.url="https://github.com/phantomxjc/MailClientWeb" \
      org.opencontainers.image.source="https://github.com/phantomxjc/MailClientWeb"

EXPOSE 8090
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('APP_PORT','8090')+'/healthz',timeout=4)"

# 单 worker + 多线程：后台自动同步线程只有一个，会话内存态也不会被多进程拆散
CMD ["sh", "-c", "gunicorn -w 1 -k gthread --threads 8 -b 0.0.0.0:${APP_PORT:-8090} --timeout 600 --access-logfile - app:app"]
