# 售后 Copilot

[![ci](https://github.com/zicong334/after-sale-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/zicong334/after-sale-copilot/actions/workflows/ci.yml)

> **这是一个学习/作品集项目,不是生产系统。** 默认跑 FakeLLM(零 API 花费),
> 数据是 seed 演示数据,README 里的账号是**演示凭据不是生产凭据**。
> 已知缺口与未验证项**如实列出**,不粉饰:见 [`docs/待验清单_20261121.md`](docs/待验清单_20261121.md)。

单店电商售后客服 Copilot:买家一个聊天入口 + 客服一个工单工作台,
把「政策问答 / 订单判责 / 转人工」跑成一条**有回执、可复跑、写操作必须人工确认**的端到端链路。

品牌「安心退」。FastAPI + SQLAlchemy 2.0 + SQLite(WAL) + LangGraph 六节点图 + 原生前端(零构建)。

**工程化现状**:965 个 pytest 用例;CI 六门禁(语法/密封测试+增量覆盖率/评测 smoke/交付物规则/
**真 PG 迁移双向 + 真 Redis 断言**/真 build 镜像 + compose 冒烟 + Prometheus + 优雅停机
——第六项 2026-11-21 新增,**首轮即抓出 nginx 只反代 /api/ 的真缺陷,修复后重跑中**);
四轮安全审计 + 一轮企业工程评审,**发现与修复过程全部留痕**(见下表)。

## 文档入口(按这个顺序读)

| 想知道什么 | 看哪份 |
|---|---|
| **新人上手**:定位 / 技术选型 / 架构图 / 逐模块拆解 | [`docs/PROJECT_OVERVIEW.md`](docs/PROJECT_OVERVIEW.md) |
| **产品口径**(唯一事实源,含 D-01~D-17 决策记录) | [`docs/PRD_v1.md`](docs/PRD_v1.md) |
| **交付现状与已知缺口** | [`docs/交接_20261112.md`](docs/交接_20261112.md)(含 2026-11-16 增补) |
| **怎么跑 / API / 账号 / MOCK 边界 / 扩展点** | [`backend/README.md`](backend/README.md) |
| **逐棒验收证据** | [`docs/验收记录.md`](docs/验收记录.md)、[`docs/最终验收报告.md`](docs/最终验收报告.md) |
| **部署交付物**(⚠️ 运行时未验证) | [`deploy/README.md`](deploy/README.md) |
| **安全审计四轮**:找真缺陷的过程与证据 | [`11-16`](docs/审计发现_20261116.md) / [`11-17`](docs/审计发现_20261117.md) / [`11-18`](docs/审计发现_20261118.md) |
| **企业工程评审**(生产就绪轴,14 项整改) | [`docs/企业工程评审_20261121.md`](docs/企业工程评审_20261121.md) |
| **性能基线**:写路径负伸缩实测 | [`docs/性能基线_20261121.md`](docs/性能基线_20261121.md) |
| **还没验的到底是什么**(诚实清单) | [`docs/待验清单_20261121.md`](docs/待验清单_20261121.md) |
| **公开前安全检查**(命令+原始输出) | [`docs/公开前安全检查_20261121.md`](docs/公开前安全检查_20261121.md) |

## 两条命令跑起来

```bash
serve.bat
```
迁移 + 空库 seed + uvicorn → http://127.0.0.1:8000,登录页 4 个演示账号一键填入。

```bash
run_eval.bat
```
FakeLLM 基线评测。**`--real` 会花真实 API 钱,动用前需用户批准。**

```bash
.venv/Scripts/python -m pytest backend/tests -q
```
全量测试(763 个用例,约 21–25 分钟;`TMP`/`TEMP` 建议指向项目 `.tmp/`)。

## 三条红线

1. `data/eval/` 是**冻结区**,改动只走版本升级程序;历史报告是审计证据,不删。
2. 密钥只在 gitignored 的 `.env`,仓库内零密钥。
3. 只有主会话 commit 和写 `progress.md`。

> 退款永远只是「创建申请单 + 人工审核」,**永不自动打款**。