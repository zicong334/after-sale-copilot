# progress.md — 售后 Copilot 台账(单一写入方:主会话;其他会话只给素材)

格式:`日期 阶段-任务 | 做了什么 | 测试数字(可复算) | 遗留`

- 2026-11-07 W0-规划 | PRD v1(含风险契约六项+6 条决策记录)+ 13 份任务书 + .venv(py3.13,requirements.lock)+ git 初始化 | 文档 14 份 | 待派 W1-A
- 2026-11-07 W1-A | 数据层:14 表模型+alembic 0001+37 测试(dsh V4-Pro 执行,12:19 卡死于沙箱 ACL 诊断被 cancel,产物完整,主会话亲测验收通过);沙箱护栏入 00_通用约定§6 | 37 passed;alembic 一次成功;15 表 | 无回执(以主会话验收记录代替)
- 2026-11-07 W1-B | 服务骨架:FastAPI+统一错误体+守卫式 CRUD+退款幂等链+seed(dsh V4-Pro 一次跑完,无沙箱事故) | 75 passed;/health 200;seed×2 幂等 | seed 需 PYTHONPATH=backend;add_message TOCTOU 记入 W3-C
- 2026-11-07 W1-C | 评测先行:10 条 golden(4/4/2)+rubric v1+validate/load 脚本(dsh V4-Pro) | 83 passed;validate=0;load×2 恒 10 | 无
- 2026-11-07 W1 阶段门 | 主会话亲测通过,准入 W2;维护性修复:conftest tmp_dir 移入 .tmp/(残留源头) | 复跑 83 passed | 无
- 2026-11-07 W2-A | 政策知识库:18 条语料(12RP+6LP)+条款分块+BM25(threshold=20.0 定标)+load 幂等(dsh V4-Pro,零事故) | 97 passed;实测 top1 全对、no_hit 拦截正常 | 无
- 2026-11-07 W2-B | 工具层五件套:三折(402 余额→SERVER 500→用户切 flash 续跑成功);5工具九字段+三件套+三层校验+确认卡+trace;env.py 修复追认 | 127 passed;全新库双迁移成功 | 缺陷修复包 F1(pattern 拒 seed 单号)/F2(StateConflictError 签名)/F3(ticket.summary 缺列)待派
- 
