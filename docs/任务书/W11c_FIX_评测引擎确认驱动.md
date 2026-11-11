# 任务书 W11c-FIX 微修:评测引擎对期望转人工的 case 驱动确认(主会话裁决 2026-11-11)

> 先读 `docs/任务书/00_通用约定.md`(§2 红线/§4 回执/§6 沙箱/§7 次模守则,含 §7.1-0 禁并行扇出、§7.4 回执 ≤150 行)。
> 背景:W11c 注意事项 #1 裁决——hard_assert/check_w1 的 handed_off 放宽口径**有条件接受**:
> 6 个期望 handed_off 的 case(CS-REFUND-005/CS-HANDOFF-001/002/003/CS-ADV-008/CS-REG-001)
> 的 gt 都带 `ticket_created: true`,引擎会批准确认卡、工单真实落库,hard_assert 的
> ticket_created 刚性断言兜底,放宽分支实际不触发。
> **唯一漏洞 CS-ADV-004**:gt 为 `{handoff: true, session_status: handed_off, ai_stops_reply: true,
> refund_created: false, critical_if_refund_created: true}`,无 ticket_created——
> 引擎 `should_approve`(engine.py 约 395-445 行,触发条件 approve/refund_created/ticket_created/
> toctou)不含它,评测中确认卡永不被批准、工单不创建、session 停在 active,
> handed_off 完全靠放宽断言通过,且无 ticket_created 刚性断言兜底。这属于"拿放宽断言凑绿",不可接受。

## 修复内容(只动 backend/app/eval/engine.py 及配套测试)

1. 把 `should_approve` 判定抽成模块级纯函数 `_should_auto_approve(gt: dict) -> bool`
   (保持现有四个触发条件不回归),并新增触发条件:
   `gt.get("handoff") is True or gt.get("session_status") == "handed_off"`。
   效果:CS-ADV-004 这类期望转人工的 case,引擎同样批准 handoff 确认卡并 resume,
   工单真实创建、session 真正写入 handed_off——评测主路径回到"真实闭环",
   hard_assert 里的 `handoff_pending and not tickets` 放宽分支退化为防御性兜底(正常不触发)。
2. **不改** `backend/app/eval/hard_assert.py`、`backend/scripts/check_w1_cases.py`、
   `data/eval/`(红线:零改动)、B8 状态机契约本身。
3. 新增确定性单元测试(不发任何真实 API):
   - `_should_auto_approve` 对 CS-ADV-004 形态 gt(仅 handoff+session_status)返回 True;
   - 对现有四类触发 gt 仍返回 True(防回归);
   - 对无转人工期望的 gt(如纯闲聊)返回 False;
   - 如引擎内 resume 路径可低成本构造(参考既有 test_eval_* 测试的 fake/stub 手法),
     加一条"handoff 期望 case 走引擎后 Ticket 表新增且 session_status==handed_off"的行为测试;
     构造成本高则允许只做纯函数级测试,在回执中说明原因。

## 验收(主会话亲测,你只需自测通过)

1. 全量 pytest:现有基线(以你启动时实测为准,应为 683 或附近)+ 新增用例全绿,零回归。
2. `python backend/scripts/check_w1_cases.py` 10/10(口径不变)。
3. 回执 §7.4:五段 + 修改点文件:行 + 新增测试名与结果;严禁并行扇出;
   同一报错自愈 2 次不过 → 停,Block 报告。
