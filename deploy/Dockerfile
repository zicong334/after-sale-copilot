# syntax=docker/dockerfile:1.7
# W12-3 交付物:售后 Copilot 后端镜像(多阶段 / 非 root / 自带健康检查)。
#
# 设计取舍:
# - builder 阶段只负责按 requirements.lock.txt 装依赖,运行阶段不带编译链;
# - 运行阶段用 python:3.13-slim(与开发机 .venv 的 3.13.15 对齐);
# - 以 UID 10001 的非 root 用户运行,数据目录 /app/data 与临时目录 /app/.tmp 归该用户;
# - HEALTHCHECK 用 python 标准库探 /health(slim 镜像不带 curl);
# - 迁移与空库 seed 放在 deploy/entrypoint.sh,容器启动即可用。
#
# ⚠️ 本文件未在本机做过 docker build 运行时验证(本机无 Docker),仅有结构级 lint。
ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# 少数依赖在目标平台没有预编译轮子时需要编译器;仅 builder 阶段安装。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.lock.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.lock.txt


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PATH="/opt/venv/bin:${PATH}" \
    TMPDIR=/app/.tmp \
    METRICS_ENABLED=1 \
    METRICS_ALLOW_REMOTE=0

RUN groupadd --gid 10001 copilot \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin copilot

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=copilot:copilot backend ./backend
COPY --chown=copilot:copilot frontend ./frontend
# R19(2026-11-21 真浏览器验收抓出):原先**只 COPY backend/frontend**,
# 政策语料 data/policies/*.md 根本没进镜像,entrypoint 也不跑 load_policies.py
# ⇒ 部署出来知识库是空的,**任何政策问题都只回"检索无命中"兜底**。
# 本机对照:不装语料 58 字兜底;装了同一问题 336 字真答案 + [1] 引用。
# 语料是**运行时资产**(不是数据卷里的业务数据),必须随镜像发布。
# ⚠️ 刻意放 /app/policies 而**不是** /app/data/policies:compose 把命名卷挂在 /app/data,
# 放里面会被卷遮蔽(首次 Docker 会把镜像内容拷进新卷,但**已存在的旧卷就不会**),
# 于是"改了语料重新 build 却不生效"。资产与可变数据分开放,没这个坑。
COPY --chown=copilot:copilot data/policies ./policies
COPY --chown=copilot:copilot deploy/entrypoint.sh /app/entrypoint.sh

RUN mkdir -p /app/data /app/.tmp \
    && chown -R copilot:copilot /app/data /app/.tmp \
    && chmod 0755 /app/entrypoint.sh

USER copilot

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["/app/entrypoint.sh"]
