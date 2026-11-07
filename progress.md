# progress.md — 售后 Copilot 台账(单一写入方:主会话;其他会话只给素材)

格式:`日期 阶段-任务 | 做了什么 | 测试数字(可复算) | 遗留`

- 2026-11-07 W0-规划 | PRD v1(含风险契约六项+6 条决策记录)+ 13 份任务书 + .venv(py3.13,requirements.lock)+ git 初始化 | 文档 14 份 | 待派 W1-A
- 2026-11-07 W1-A | 数据层:14 表模型+alembic 0001+37 测试(dsh V4-Pro 执行,12:19 卡死于沙箱 ACL 诊断被 cancel,产物完整,主会话亲测验收通过);沙箱护栏入 00_通用约定§6 | 37 passed;alembic 一次成功;15 表 | 无回执(以主会话验收记录代替)
- 2026-11-07 W1-B | 服务骨架:FastAPI+统一错误体+守卫式 CRUD+退款幂等链+seed(dsh V4-Pro 一次跑完,无沙箱事故) | 75 passed;/health 200;seed×2 幂等 | seed 需 PYTHONPATH=backend;add_message TOCTOU 记入 W3-C
- 2026-11-07 W1-C | 评测先行:10 条 golden(4/4/2)+rubric v1+validate/load 脚本(dsh V4-Pro) | 83 passed;validate=0;load×2 恒 10 | 无
- 2026-11-07 W1 阶段门 | 主会话亲测通过,准入 W2;维护性修复:conftest tmp_dir 移入 .tmp/(残留源头) | 复跑 83 passed | 无
- 2026-11-07 W2-A | 政策知识库:18 条语料(12RP+6LP)+条款分块+BM25(threshold=20.0 定标)+load 幂等(dsh V4-Pro,零事故) | 97 passed;实测 top1 全对、no_hit 拦截正常 | 无
- 2026-11-07 W2-B | 工具层执行中途失败:DeepSeek 账户余额不足(ACCOUNT_QUOTA 402),部分产物 3/9 文件留盘,基线未破坏 | 97 passed(存量) | 硬阻塞:等用户充值或授权换 provider;重发按规则沿用原参数(v4-pro/high)
- 
