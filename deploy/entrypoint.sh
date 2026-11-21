#!/bin/sh
# W12-3 容器入口:迁移 -> 空库 seed -> uvicorn。
# 与 serve.sh 的行为对齐,但不依赖 bash / .venv 探测(镜像里的 venv 已在 PATH)。
set -eu

: "${DATABASE_URL:=sqlite:////app/data/dev.db}"
: "${SERVE_HOST:=0.0.0.0}"
: "${SERVE_PORT:=8000}"
: "${SEED_ON_EMPTY:=1}"
export DATABASE_URL SERVE_HOST SERVE_PORT

echo "[entrypoint] DATABASE_URL=${DATABASE_URL}"
echo "[entrypoint] alembic upgrade head ..."
python -m alembic -c /app/backend/alembic.ini upgrade head

if [ "$SEED_ON_EMPTY" = "1" ]; then
    set +e
    python -c "from app.db import SessionLocal;from app.models import Merchant;from sqlalchemy import select,func;s=SessionLocal();n=s.scalar(select(func.count()).select_from(Merchant));s.close();raise SystemExit(3 if n==0 else 0)"
    rc=$?
    set -e
    if [ "$rc" -eq 3 ]; then
        echo "[entrypoint] 空库,装载 seed 数据 ..."
        python /app/backend/scripts/seed.py
    elif [ "$rc" -ne 0 ]; then
        echo "[entrypoint] 数据库检查失败(rc=$rc)" >&2
        exit "$rc"
    fi
fi

echo "[entrypoint] 监听 ${SERVE_HOST}:${SERVE_PORT}(/health 健康检查,/metrics 仅本机)"
# R11(2026-11-21):优雅停机超时 20s —— **必须小于 compose 的 stop_grace_period(30s)**,
# 否则 uvicorn 还在等 WS/SSE 长连接就被 SIGKILL 截断,grace 白设。
# 不设该参数时 uvicorn 会无限等长连接(WS proxy_read_timeout 3600s),停机必被强杀。
exec python -m uvicorn app.main:app --host "$SERVE_HOST" --port "$SERVE_PORT" \
  --timeout-graceful-shutdown 20
