# 部署交付物(W12-3)

本目录是"单机可交付"的容器化产物:**多阶段后端镜像 + nginx 静态入口 + 可选 PG/Redis/监控 profile**。

> ⚠️ **未在本机运行时验证**(但**已由 CI 真跑**,两者别混为一谈)
>
> 开发机没有 Docker、Docker Compose、PostgreSQL、Redis,Grafana/Prometheus 也未启动过。
> 在**本机**,本目录只经过结构级静态校验
> (`python deploy/lint_artifacts.py` + `backend/tests/test_deploy_artifacts.py`):
> YAML 可解析、服务/端口/卷/profiles 键齐全、Dockerfile 多阶段/非 root/HEALTHCHECK、
> nginx 的 `/metrics` allow/deny 指令存在。
>
> ✅ **2026-11-21 起,GitHub Actions 承担了真实运行时验证**(runner 本身就是 Docker 机器):
> - `pg-redis` job:真 PG16 + 真 Redis7,11 条迁移双向 + 全量测试跑在 PG 上 + 真 Redis 四条断言;
> - `container` job:**真 build 本项目镜像** + `compose up backend nginx` + 经 nginx 冒烟
>   + Prometheus 抓取/指标/告警规则 + SIGTERM 优雅停机看退出码。
>
> 清「未运行时验证」标记请以**那两个 job 的原始输出**为准。
> **run 44902421693 六 job 全绿**,容器真起 / 可观测真通 / 优雅停机三项已清标记。
> ⚠️ `container` job **首轮就抓出真缺陷**:nginx 此前只反代 `/api/`,
> `/auth` `/orders` `/refunds` `/tickets` `/v1` 共 19 个端点全落静态 404 —— **部署栈登不进去**。
> 已修,并加两条永久守卫(API 不得落静态 / 静态不得被反代吞)。
> ⚠️ 优雅停机那步**第一版是假验证**(只断言退出码非 137,而停机时没有在飞长连接,0s 退出说明不了排空);
> 已改成**挂住一条真 WS 再停机**,实测 `stop_seconds=0.7 / exit_code=0 / 关闭帧 code=1012`。
> **20s grace 上限本身仍未被触发**,真模型长请求下的充足性属待压测项。
> 首次在有 Docker 的机器上执行时,仍请按下面"首次落地核对单"逐条确认。

## 1. 目录内容

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 多阶段镜像:builder 按 `requirements.lock.txt` 装依赖;runtime 用 `python:3.13-slim`,UID 10001 非 root,`HEALTHCHECK` 探 `/health` |
| `entrypoint.sh` | 容器入口:`alembic upgrade head` → 空库 seed(可关)→ `uvicorn app.main:app` |
| `docker-compose.yml` | `backend` + `nginx` 为最小栈;`postgres`/`redis`(profile `full`)、`prometheus`/`grafana`(profile `observability`)可选 |
| `nginx.conf` | 静态前端 + 反代 `/api/`、`/auth`、`/orders`、`/refunds`、`/tickets`、`/v1`、`/ws/`、`/health*` + `/metrics` 仅回环/私网。**新增 router 必须同步这里**,两条守卫会卡 |
| `prometheus.yml` | 抓取 `backend:8000/metrics`,15s 间隔 |
| `lint_artifacts.py` | 交付物静态自检(无 Docker 可跑),CI 与本机共用 |

## 2. 快速开始(需 Docker)

```bash
# 最小栈:backend(SQLite,命名卷)+ nginx,浏览器开 http://127.0.0.1:8080
docker compose -f deploy/docker-compose.yml up -d --build

# 额外带 PG/Redis 容器(后端仍走 DATABASE_URL;PG 方言已由 CI pg-redis job 实证)
docker compose -f deploy/docker-compose.yml --profile full up -d

# 额外带 Prometheus/Grafana
docker compose -f deploy/docker-compose.yml --profile observability up -d
# Grafana http://127.0.0.1:3000(密码取 GRAFANA_ADMIN_PASSWORD,默认 change-me-in-env,务必覆盖)
```

## 3. 关键环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:////app/data/dev.db` | 镜像内数据落在命名卷 `copilot-data`;切 PG 时形如 `postgresql+psycopg://...`(方言与迁移已由 CI 在真 PG16 上验过;驱动 `psycopg` 已进生产锁) |
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
- [ ] `docker compose --profile full up -d` 后 PG/Redis 容器 healthy(切 PG 需显式设 `DATABASE_URL`;方言已由 CI 验,本机切换仍需自查)
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

`.github/workflows/ci.yml` 含**六门禁**,每 job 都设 `timeout-minutes`(无超时那次 PG 步骤卡死 121 分钟无信号):

| job | 做什么 |
| --- | --- |
| `lint` | compileall 全量语法 + 交付物 lint |
| `pytest` | 密封 FakeLLM 全量测试 + **增量覆盖率门禁**(diff line ≥ 80%) |
| `eval` | `run_eval.py --smoke`,报告写 runner 临时目录,不脏 `data/eval/reports` |
| `deploy` | 交付物 lint 单独成 job |
| `pg-redis` | **真 PG16 + 真 Redis7**(service container):11 条迁移 upgrade→downgrade base→再 upgrade、关键列核对、全量测试跑在 PG 上、真 Redis 四条断言 |
| `container` | **真 build 本项目镜像** + `compose up backend nginx` + 经 nginx 冒烟 + Prometheus 抓取/新指标/告警规则 + `docker stop` 看退出码(137 = 被 SIGKILL,判优雅停机失效) |

全部 job 不注入任何真实模型 key(无 key 时 `build_llm` 自动回退 FakeLLM)。

> 远端:https://github.com/zicong334/after-sale-copilot (2026-11-21 已公开,workflow 已真跑)。

## 8. 已知限制

- **未在本机运行时验证**(见文首);真实运行时验证由 CI 的 `pg-redis` / `container` 两 job 承担。
- 后端**默认仍是 SQLite**。PG 方言与迁移已在真 PG16 上验过,但**业务默认口径没切**;
  Redis 限流(`ratelimit_redis.py`)与 L2 缓存(`cache_l2.py`)已实现且经真 Redis 断言,
  但**按 env 开关启用,默认走进程内实现** —— 单机形态下进程内实现才是正确默认。
- **横向扩容对 SQLite 形态无效**(单写者 + 进程内限流/缓存/队列三坑咬合):
  顺序必须 **先 PG → 再 Redis → 才多实例**。依据见 `docs/性能基线_20261121.md`(写路径负伸缩实测)。
- 本编排**无 Alertmanager**:告警规则只在 Prometheus UI 可见,**不发通知**(用户裁定:无专职运维时多一个组件是负担)。
- Grafana 未预置 dashboard/datasource provisioning,需手工加 Prometheus 数据源。
- 镜像未做 SBOM/漏洞扫描;`python:3.13-slim` 与前端 `nginx:1.27-alpine` 的补丁需按组织基线定期刷新。
- `verify_runtime.sh` **自身仍未被执行过**(只过 `bash -n`);它与 CI 两 job 覆盖同样的地面,留作本机复现用。
