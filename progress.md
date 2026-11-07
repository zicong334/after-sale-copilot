# progress.md — 售后 Copilot 台账(单一写入方:主会话;其他会话只给素材)

格式:`日期 阶段-任务 | 做了什么 | 测试数字(可复算) | 遗留`

- 2026-11-07 W0-规划 | PRD v1(含风险契约六项+6 条决策记录)+ 13 份任务书 + .venv(py3.13,requirements.lock)+ git 初始化 | 文档 14 份 | 待派 W1-A
- 2026-11-07 W1-A | 数据层:14 表模型+alembic 0001+37 测试(dsh V4-Pro 执行,12:19 卡死于沙箱 ACL 诊断被 cancel,产物完整,主会话亲测验收通过);沙箱护栏入 00_通用约定§6 | 37 passed;alembic 一次成功;15 表 | 无回执(以主会话验收记录代替)
- 2026-11-07 W1-B | 服务骨架:FastAPI+统一错误体+守卫式 CRUD+退款幂等链+seed(dsh V4-Pro 一次跑完,无沙箱事故) | 75 passed;/health 200;seed×2 幂等 | seed 需 PYTHONPATH=backend;add_message TOCTOU 记入 W3-C
- 2026-11-07 W1-C | 评测先行:10 条 golden(4/4/2)+rubric v1+validate/load 脚本(dsh V4-Pro) | 83 passed;validate=0;load×2 恒 10 | 无
- 2026-11-07 W1 阶段门 | 主会话亲测通过,准入 W2;维护性修复:conftest tmp_dir 移入 .tmp/(残留源头) | 复跑 83 passed | 无
- 2026-11-07 W2-A | 政策知识库:18 条语料(12RP+6LP)+条款分块+BM25(threshold=20.0 定标)+load 幂等(dsh V4-Pro,零事故) | 97 passed;实测 top1 全对、no_hit 拦截正常 | 无
- 2026-11-07 W2-B | 工具层五件套:三折(402 余额→SERVER 500→用户切 flash 续跑成功);5工具九字段+三件套+三层校验+确认卡+trace;env.py 修复追认 | 127 passed;全新库双迁移成功 | 缺陷修复包 F1/F2/F3 待派
- 2026-11-07 W2-B-FIX | flash+§7 首跑:F1 pattern 放宽(3处)+seed 双路回归、F2 StateConflictError.param、F3 ticket.summary+迁移0003+handoff 分列 | 131 passed;三迁移一次成功;越权/归属实测取证 | 无
- 2026-11-07 W2-C | 感知层+会话薄路由+FakeLLM 三场景(dsh flash,§7 全程遵守,调试期错误各修 1 次过) | 153 passed;check_w1_cases 9/10→治理修复后 10/10 | 无
- 2026-11-07 W2 阶段门 | 主会话亲测通过;数据治理#1:CS-POLICY-004 expected_tools 修正(W1-C 缺陷),版本 cs-eval-w1-20261107a | demo 三场景/引用带条款号/守卫测试实存 | 准入 W3
- 2026-11-07 17:5x W4-C 解锁 | 用户交 key(只入 .env,不进 git/日志/回执)+锁定 deepseek-v4.1-flash;主会话实测 API ID 应为 deepseek-flash(HTTP 200),reasoning 模型空正文风险写入 W4-C 任务书 | 连通性验证 42 tokens | 周末目标升级为 W4-C+最终验收+收尾
- 2026-11-07 W3-A | LangGraph 六节点图+SqliteSaver+interrupt 确认门,替换 w1_router(dsh flash,一次跑通) | 175 passed;check_w1_cases 10/10;重启恢复零重放;steps=9 熔断 | 确认流 HTTP 入口在 W3-C
- 2026-11-07 W3-B | 记忆层:memory 表(0004 迁移)+白名单+注入+定价指令隔离+衰减路由(dsh flash 一次跑通) | 196 passed;G6 端到端取证;10/10 不倒退 | 无
- 2026-11-07 W3-C | 确认卡闭环+转人工全链路+前端 SSE 壳+TOCTOU 定点修复(dsh flash 一次跑通) | 222 passed;10/10;五路由 200;确认卡四要素+verified 取证 | usage 为 MOCK 估算待 W4-C
- 2026-11-07 W3 阶段门 | 主会话亲测通过,准入 W4 | 四承诺全有测试证据 | 队列:W4-A→W4-B→W4-C→最终验收
- 2026-11-07 W4-A | 评测完备:48 条(19/14/10/5)+硬断言引擎+FakeJudge(防注入)+报告 6 指标+run_eval 一键(dsh flash) | 242 passed;FakeLLM 基线 0.9792 照实报;smoke 15/15 | CS-EDGE-005 成簇=W4-C 真模型基线缺口
- 2026-11-07 W4-B首发 | 21:00 挂死于沙箱 ACL 诊断 powershell(与 W1-A 同类),21:31 cancel;遗留 7 产物 1006 行可承接 | 无(未跑完) | 续作已重发(硬护栏:禁诊断/powershell、basetemp 新目录)
- 2026-11-07 W4-B | 交付(续作完成):serve/run_eval 一键双脚本+README(数字脚本生成 BYTE-EQUAL)+回滚演练+S1-S4 自检;主会话修复 .sh PYTHONPATH 分隔符 bug | dsh 侧 249 passed;我方 248+1(语法测试系沙箱限制);serve/run_eval 实测通过 | W4-C 就绪
- 2026-11-08 W4-C | 真实模型 E2E:real_client+真实裁判+48×3 一次跑完(24分钟,804,905 tokens);S6 三场景+回滚;**诚实判"未达到"**(0.6944/critical 6/agreement 0.875);主会话独立复算吻合+抽检 5 条程序通过 | 263 passed(dsh侧);key 0 泄露;密封 14 passed | 修复周期:确定性护栏包 W4-C-FIX 派发,修复后主会话亲自重跑全量一次,仍不达标即停线上交
- 2026-11-08 W4-C-FIX | 七项确定性护栏(guardrails.py 集中常量):强制核验链/refund策略门/注入感知/反编造/引用保真/reason枚举+治理#2(cs-eval-v1-20261108b)/记忆意图(dsh flash) | 334 passed(dsh);Fake 全量 0.9792 不降;check_w1 10/10 | 主会话执行第二轮真实全量重跑中
- 
