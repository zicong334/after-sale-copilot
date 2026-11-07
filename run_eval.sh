#!/usr/bin/env bash
# 一键评测:调 backend/scripts/run_eval.py(FakeLLM + FakeJudge,无 key)
# 用法:./run_eval.sh                       (48 条 x repeat=3)
#       ./run_eval.sh --smoke              (golden 前 5 条)
#       ./run_eval.sh --layer adversarial --repeat 1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"
export TMP="${TMP:-$ROOT/.tmp}"
export TEMP="${TEMP:-$ROOT/.tmp}"
mkdir -p "$TMP"

if [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
  PY="$ROOT/.venv/Scripts/python.exe"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  echo "[run_eval] 未找到 .venv 解释器,请先安装 requirements.lock.txt" >&2
  exit 1
fi

exec "$PY" "$ROOT/backend/scripts/run_eval.py" "$@"
