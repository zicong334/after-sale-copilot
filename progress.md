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
- 2026-11-08 W4-C第二轮 | 主会话亲自重跑全量真实评测(D-10 唯一一次):0.9236/critical 0/agreement 0.9722,独立复算吻合;判"部分达到(偏上)",硬断言未 100%+cost 缺价目 → **按 D-10 停线上交** | 133/144;806,451 tokens | 残余:REFUND-006(0/3)、MEMORY-001(0/3)两个系统性+5 次波动;周一用户裁决第三轮或带缺口交付
- 2026-11-09 用户裁决 BC | B=第三轮小修+重跑一次(已授权);C=口径治理→PRD D-11(critical类硬断言100%+整体≥0.9+agreement≥0.8+critical=0=达到),第二轮按新口径复判"达到",最终判定以第三轮为准;价目表存档 pricing_deepseek_20261109.md→cost 补齐:两轮合计≈2.53元(空闲时段上界),第三轮预算≈2.5元(高峰) | FIX2 任务书已写(2项通用守卫,禁case特判,断言冻结) | 派发 W4-C-FIX2
- 2026-11-09 W4-C-FIX2 | 收尾双守卫(意图快捷路径+标准拒答拼接/记忆引用保真);会话 10:59 被用户侧中止于收尾,产物完整,主会话实测验收 | 348 passed;10/10;零特判 | 第三轮重跑推迟到 W5 产品修正后(一次重跑测最终产品)
- 2026-11-09 用户手测报缺陷 | ①转人工后 AI 仍吐固定话术+每消息黄横幅轰炸(违背闭嘴精神,实现照 G7 字面"回固定话术"而测试也按字面编码,验收漏了精神)②无客服工作台页面(PRD 承诺的客服侧入口只做了 API)③聊天壳视觉低于用户标准④blocked 轮显示 MOCK usage 行误导扣费感(实测零真实花费,FakeLLM estimated) | — | W5 产品修正任务书待用户拍板视觉方向与范围
- 2026-11-09 W5 | 产品修正四件落地:转人工真静默/客服工作台(agent.html)/温暖C端视觉重做/blocked零usage(dsh flash+三子代理分工) | 367 passed(dsh)/366+1(我方);静默与闭环库级实测通过 | 用户实机验收 http://127.0.0.1:8000 中;过后再跑第三轮真实评测
- 2026-11-09 UI实机验收失败→根因确诊 | 用户截图+主会话 headless Edge 实测:视觉本身成立,死穴=summary 按客户取最近工单+Ticket 无 session_id→历史 closed 工单毒化新会话(disabled composer/误徽章);另缺工作台入口/404/无 no-cache | — | W5-FIX 派发(hrun-mw8ut2ec);用户大厂清单 triage 落 docs/大厂差距triage与采纳.md(W6 八项破坏性测试/W7 五项增强/缓做留痕/架构反思自写)
- 2026-11-09 W5-FIX | 前端状态机根修:迁移0005 ticket.session_id+会话域状态机+双页入口+favicon+no-cache(dsh flash) | 380 passed(dsh)/379+1(我方);headless 零错误;截图 10 张 | 等用户 UI 复核;W6 破坏性测试包派发中
- 2026-11-09 授权边界(用户) | UI 复核延后不阻塞;W6→W7 连续做完直接汇报;**仅第三轮全量真实评测需用户批准才动**;W7 任务书已立(PII脱敏/LLM降级/别名扩展/坐席建议/审计导出) | — | W6 执行中
- 
