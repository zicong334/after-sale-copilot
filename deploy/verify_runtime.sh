#!/usr/bin/env bash
# R3 一键运行时验证(2026-11-21 企业工程评审)
#
# ⚠️⚠️ **本脚本从未被执行过** ⚠️⚠️
# 产出它的机器上 `docker` / `psql` / `redis-server` **全部不存在**,
# 所以只做了 `bash -n` 语法校验与结构自检,**没有任何一步是"已验证"的**。
# 在**有 Docker 的机器**上跑它,才算把 R3 与 Redis 三项的「未运行时验证」标记清掉。
#
# 用法(项目根执行):
#   bash deploy/verify_runtime.sh            # 全阶段
#   bash deploy/verify_runtime.sh minimal    # 只跑最小栈冒烟
#   bash deploy/verify_runtime.sh pg         # 只跑 PG 迁移与测试
#   bash deploy/verify_runtime.sh obs        # 只跑可观测
#
# 每阶段失败即停并打印"哪一步、为什么",不吞错。结束后打印待验清单对照。
set -euo pipefail

STAGE="${1:-all}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
COMPOSE="docker compose -f deploy/docker-compose.yml"
# nginx 映射的是 8080:80(不是 80);第一版脚本 curl 80 是错的,已修。
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
# compose 的 PG 默认口令是 change-me-in-env;这里显式固定,保证与下面 PG_URL 一致。
export POSTGRES_USER="${POSTGRES_USER:-copilot}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-copilot}"
export POSTGRES_DB="${POSTGRES_DB:-copilot}"
FAIL=0

say()  { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  [OK]   %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=1; }

require_docker() {
  command -v docker >/dev/null 2>&1 || { echo "本机无 docker,无法验证 R3。请在有 Docker 的机器上跑。" >&2; exit 2; }
  docker info >/dev/null 2>&1 || { echo "docker daemon 不可用。" >&2; exit 2; }
}

wait_healthy() { # $1=service $2=tries
  local i
  for i in $(seq 1 "${2:-40}"); do
    if $COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | grep -q "^$1 healthy"; then
      return 0
    fi
    sleep 3
  done
  return 1
}

stage_minimal() {
  say "阶段1:最小栈(backend + nginx)真起 + 冒烟"
  $COMPOSE build backend
  $COMPOSE up -d backend nginx
  if wait_healthy backend 40; then ok "backend healthcheck 转 healthy"; else bad "backend 未在 120s 内 healthy"; return; fi
  # 经 nginx 走一遍真实链路:静态页 / liveness / readiness / 登录 / 一轮对话
  curl -fsS $BASE_URL/login.html >/dev/null && ok "nginx 提供 login.html" || bad "login.html 取不到"
  curl -fsS $BASE_URL/health >/dev/null && ok "/health(liveness)200" || bad "/health 非 200"
  curl -fsS $BASE_URL/healthz/ready >/dev/null && ok "/healthz/ready(readiness)200" || bad "/healthz/ready 非 200"
  TOKEN=$(curl -fsS -X POST $BASE_URL/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"username":"buyer_demo","password":"demo1234"}' | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
  [ -n "$TOKEN" ] && ok "登录拿到 token" || { bad "登录失败"; return; }
  curl -fsS -X POST $BASE_URL/api/c/chat \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"text":"退货运费谁承担"}' >/dev/null && ok "一轮对话 200(容器内 FakeLLM)" || bad "对话失败"
  # R7:readiness 必须**不**被 nginx 用于摘流(单机摘流=全站下线),这里只验它可达
  ok "提醒:单机形态 readiness 只用于告警,不配主动摘流(见 nginx.conf 注释)"
}

stage_pg() {
  say "阶段2:PG 真起 + 11 条迁移方言 + 测试跑在 PG 上"
  $COMPOSE --profile full up -d postgres redis
  if wait_healthy postgres 40; then ok "postgres healthy"; else bad "postgres 未 healthy"; return; fi
  if wait_healthy redis 40; then ok "redis healthy"; else bad "redis 未 healthy"; fi
  # 迁移方言:这一步才是 R3 的"方言部分",**不是** create_all 建表
  PG_URL="${PG_URL:-postgresql+psycopg://copilot:copilot@127.0.0.1:5432/copilot}"
  say "2a) alembic upgrade head 到 PG(验 11 条迁移的方言)"
  if DATABASE_URL="$PG_URL" PYTHONPATH="$ROOT/backend" \
     python -m alembic -c backend/alembic.ini upgrade head; then
    ok "11 条迁移在 PG 上 upgrade head 成功"
  else
    bad "PG 迁移失败 —— 这就是 W12-4 方言缺口,按报错逐条修"
    return
  fi
  say "2b) 验关键列真在(0011 只加列不建表)"
  DATABASE_URL="$PG_URL" PYTHONPATH="$ROOT/backend" python - <<'PY' && ok "order.delivered_at 存在" || bad "order.delivered_at 缺失"
import os
from sqlalchemy import create_engine, inspect
eng = create_engine(os.environ["DATABASE_URL"])
cols = [c["name"] for c in inspect(eng).get_columns("order")]
raise SystemExit(0 if "delivered_at" in cols else 1)
PY
  say "2c) 全量测试跑在 PG 上(SQLite 专属文件自动跳过)"
  # ⚠️ 需要 PG 驱动:requirements.lock.txt 目前**不含** psycopg,按报错装 dev 侧驱动后再跑
  if TEST_DATABASE_URL="$PG_URL" PYTHONPATH="$ROOT/backend" TMPDIR="$ROOT/.tmp" \
     python -m pytest backend/tests -q -p no:cacheprovider; then
    ok "测试在 PG 上通过(SQLite 专属两文件已跳过)"
  else
    bad "PG 上有失败 —— 逐条看是方言问题还是真缺陷,别直接改断言"
  fi
  say "2d) 迁移可回滚(PG 上的 downgrade)"
  DATABASE_URL="$PG_URL" PYTHONPATH="$ROOT/backend" \
    python -m alembic -c backend/alembic.ini downgrade -1 && ok "downgrade -1 成功" || bad "PG downgrade 失败"
  DATABASE_URL="$PG_URL" PYTHONPATH="$ROOT/backend" \
    python -m alembic -c backend/alembic.ini upgrade head && ok "再 upgrade 回来成功" || bad "再 upgrade 失败"
}

stage_obs() {
  say "阶段3:可观测真通(Prometheus 抓取 + 新指标 + 告警规则加载)"
  $COMPOSE --profile observability up -d prometheus grafana
  sleep 20
  if curl -fsS http://127.0.0.1:9090/api/v1/targets | grep -q '"health":"up"'; then
    ok "Prometheus 抓到 backend target(up)"
  else
    bad "Prometheus target 不是 up —— 查 METRICS_ALLOW_REMOTE 与 compose 网络"
  fi
  if curl -fsS 'http://127.0.0.1:9090/api/v1/query?query=copilot_job_dead_oldest_age_seconds' | grep -q '"result":\[{'; then
    ok "R6 新指标 copilot_job_dead_oldest_age_seconds 可查"
  else
    bad "新指标查不到"
  fi
  if curl -fsS http://127.0.0.1:9090/api/v1/rules | grep -q 'CopilotDeadLetterAging'; then
    ok "R6 告警规则已加载"
  else
    bad "告警规则未加载 —— 查 alerts.yml 挂载与 rule_files"
  fi
  printf '  [NOTE] 本编排**无 Alertmanager**:规则只在 UI 可见,**不发通知**。\n'
  printf '  [NOTE] 要真闭环需另加 Alertmanager + receivers(运维决策,不在本轮)。\n'
}

teardown() {
  say "收尾:停栈(保留卷,便于复查)"
  $COMPOSE --profile full --profile observability down || true
}

require_docker
case "$STAGE" in
  minimal) stage_minimal ;;
  pg)      stage_pg ;;
  obs)     stage_minimal; stage_obs ;;
  all)     stage_minimal; stage_pg; stage_obs ;;
  *) echo "未知阶段: $STAGE(可选 all|minimal|pg|obs)" >&2; exit 2 ;;
esac
teardown

say "结果"
if [ "$FAIL" -eq 0 ]; then
  echo "全部阶段通过。**此时才可**把评审报告里 R3 与 Redis 三项的「未运行时验证」标记清掉。"
else
  echo "有阶段失败(见上面 [FAIL])。**不要**把未验证标记清掉;按报错逐条修后重跑。"
fi
exit "$FAIL"