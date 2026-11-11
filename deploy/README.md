# 部署交付物(W12-3)

本目录是"单机可交付"的容器化产物:**多阶段后端镜像 + nginx 静态入口 + 可选 PG/Redis/监控 profile**。

> ⚠️ **未在本机运行时验证**
>
> 开发机没有 Docker、Docker Compose、PostgreSQL、Redis,Grafana/Prometheus 也未启动过。
> 本目录中的镜像构建、编排启动、抓取链路**只经过结构级静态校验**
> (`python deploy/lint_artifacts.py` + `backend/tests/test_deploy_artifacts.py`),
> 包括 YAML 可解析、服务/端口/卷/profiles 键齐全、Dockerfile 多阶段/非 root/HEALTHCHECK、
> nginx 的 `/metrics` allow/deny 指令存在。**没有任何一条在本机真实跑通过。**
> 首次在有 Docker 的机器上执行时,请按下面"首次落地核对单"逐条确认。

## 1. 目录内容

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 多阶段镜像:builder 按 `requirements.lock.txt` 装依赖;runtime 用 `python:3.13-slim`,UID 10001 非 root,`HEALTHCHECK` 探 `/health` |
| `entrypoint.sh` | 容器入口:`alembic upgrade head` → 空库 seed(可关)→ `uvicorn app.main:app` |
| `docker-compose.yml` | `backend` + `nginx` 为最小栈;`postgres`/`redis`(profile `full`)、`prometheus`/`grafana`(profile `observability`)可选 |
| `nginx.conf` | 静态前端 + `/api/`、`/ws/` 反代 + `/metrics` 仅回环/私网 |
| `prometheus.yml` | 抓取 `backend:8000/metrics`,15s 间隔 |
| `lint_artifacts.py` | 交付物静态自检(无 Docker 可跑),CI 与本机共用 |

## 2. 快速开始(需 Docker)

```bash
# 最小栈:backend(SQLite,命名卷)+ nginx,浏览器开 http://127.0.0.1:8080
docker compose -f deploy/docker-compose.yml up -d --build

# 额外带 PG/Redis 容器(注意:后端当前仍走 DATABASE_URL,PG 方言适配是 W12-4)
docker compose -f deploy/docker-compose.yml --profile full up -d

# 额外带 Prometheus/Grafana
docker compose -f deploy/docker-compose.yml --profile observability up -d
# Grafana http://127.0.0.1:3000(密码取 GRAFANA_ADMIN_PASSWORD,默认 change-me-in-env,务必覆盖)
```

## 3. 关键环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:////app/data/dev.db` | 镜像内数据落在命名卷 `copilot-data`;切 PG 时形如 `postgresql+psycopg://...`(未验证) |
| `SEED_ON_EMPTY` | `1` | 容器启动时 merchant 表为空才 seed,避免覆盖业务数据 |
| `SERVE_HOST` / `SERVE_PORT` | `0.0.0.0` / `8000` | uvicorn 监听;对外只经 nginx |
| `METRICS_ENABLED` | `1` | 关闭后 `/metrics` 返回 404 |
| `METRICS_ALLOW_REMOTE` | compose 里 `1` | 容器内客户端不是回环,Prometheus/nginx 抓取必须为 `1`;对外暴露面由 nginx `allow/deny` 与 compose 网络隔离承担。**若把 backend 直接映射到公网,请改回 0** |
| `POSTGRES_PASSWORD` / `GRAFANA_ADMIN_PASSWORD` | `change-me-in-env` | 仅示例占位,部署前必须覆盖 |

## 4. `/metrics` 的三层口径

1. **应用层**:`client_is_local()` 只放行 `127.0.0.1`/`::1`/`localhost`/`testclient`;
   非本机且 `METRICS_ALLOW_REMOTE=0` 时返回 403。
2. **nginx 层**:`location = /metrics` 只 `allow` 回环与 `10/8`、`172.16/12`、`192.168/16`,其余 `deny all`。
3. **网络层**:compose 中 backend 只 `expose` 8000,不映射宿主端口。

## 5. 首次落地核对单(本机无法代劳)

- [ ] `docker compose -f deploy/docker-compose.yml build` 成功(首次会装依赖,耗时较长)
- [ ] `docker compose ... up -d` 后 `docker compose ps` 中 backend 为 `healthy`
- [ ] `curl -fsS http://127.0.0.1:8080/health` 返回 200
- [ ] `curl -fsS http://127.0.0.1:8080/metrics | head` 出现 `copilot_metrics_last_refresh_unixtime`
- [ ] 从非私网地址访问 `/metrics` 返回 403
- [ ] `docker compose --profile full up -d` 后 PG/Redis 容器 healthy(业务链路未切 PG,见 W12-4)
- [ ] `--profile observability` 后 Prometheus target 为 UP、Grafana 能加数据源
- [ ] 重启 backend 容器后数据仍在(`copilot-data` 卷)

## 6. 本机可做的校验(已验证口径)

```bash
# 交付物静态自检:结构/YAML/lint 规则
.venv/Scripts/python deploy/lint_artifacts.py

# 对应 pytest(与 CI 同一套断言)
.venv/Scripts/python -m pytest backend/tests/test_deploy_artifacts.py -q
```

## 7. CI

`.github/workflows/ci.yml` 含四门禁:`lint`(compileall + 交付物 lint)、`pytest`(密封
FakeLLM 环境,不注入任何真实 key)、`eval`(`run_eval.py --smoke`,报告写 runner 临时目录,
不脏 `data/eval/reports`)、`deploy`(交付物 lint 单独成 job)。

> 本仓库本地**没有配置 remote**,该 workflow 在推送后才真正生效;本地只能做 YAML 解析校验。

## 8. 已知限制

- 未在本机运行时验证(见文首)。
- 后端默认 SQLite;`postgres`/`redis` 服务目前只是"容器形态就位",业务代码尚未接 PG 方言与 Redis 队列(W12-4 范围)。
- Grafana 未预置 dashboard/datasource provisioning,需手工加 Prometheus 数据源。
- 镜像未做 SBOM/漏洞扫描;`python:3.13-slim` 与前端 `nginx:1.27-alpine` 的补丁需按组织基线定期刷新。
