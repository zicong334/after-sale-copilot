#!/usr/bin/env bash
# 一键起服务:迁移 -> 空库 seed -> uvicorn(无 key 可起)
# 用法:./serve.sh            (默认 127.0.0.1:8000)
#       SERVE_PORT=8080 ./serve.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# .venv 是 Windows Python:PYTHONPATH 分隔符必须用 ';' 而非 POSIX 的 ':'
# (2026-11-07 主会话维护性修复:预置 PYTHONPATH 时 ':' 拼接会让 app 包不可导入)
export PYTHONPATH="$ROOT/backend${PYTHONPATH:+;$PYTHONPATH}"
export TMP="${TMP:-$ROOT/.tmp}"
export TEMP="${TEMP:-$ROOT/.tmp}"
mkdir -p "$TMP"

if [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
  PY="$ROOT/.venv/Scripts/python.exe"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  echo "[serve] 未找到 .venv 解释器,请先安装 requirements.lock.txt" >&2
  exit 1
fi

SERVE_HOST="${SERVE_HOST:-127.0.0.1}"
SERVE_PORT="${SERVE_PORT:-8000}"

echo "[serve] Alembic upgrade head ..."
"$PY" -m alembic -c "$ROOT/backend/alembic.ini" upgrade head

# 仅当 merchant 表为空时才 seed,避免每次启动覆盖已有业务数据。
set +e
"$PY" -c "from app.db import SessionLocal;from app.models import Merchant;from sqlalchemy import select,func;s=SessionLocal();n=s.scalar(select(func.count()).select_from(Merchant));s.close();raise SystemExit(3 if n==0 else 0)"
rc=$?
set -e
if [ "$rc" -eq 3 ]; then
  echo "[serve] 空库,装载 seed 数据 ..."
  "$PY" "$ROOT/backend/scripts/seed.py"
elif [ "$rc" -ne 0 ]; then
  echo "[serve] 数据库检查失败(rc=$rc)" >&2
  exit "$rc"
fi

echo "[serve] 地址: http://${SERVE_HOST}:${SERVE_PORT}"
echo "[serve] 健康检查: http://${SERVE_HOST}:${SERVE_PORT}/health"
echo "[serve] Ctrl+C 停止"
exec "$PY" -m uvicorn app.main:app --host "$SERVE_HOST" --port "$SERVE_PORT"
