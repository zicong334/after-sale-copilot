# 任务书 W15-FIX 微修:stepper 时间戳 UTC→本地换算(主会话真截图实测发现,2026-11-12)

> 先读 `docs/任务书/00_通用约定.md`(§2 红线/§4 回执/§6 沙箱/§7 次模守则)。
> **实测证据(主会话真截图)**:`.tmp/w15_shots/14_agent_detail_stepper.png`——工单头部"创建时间 11-12 06:26"正确,
> 但处理进度 stepper 首节点显示"提交申请 22:28"(=UTC 原值,本地应为 06:28)。差 8 小时。

## 根因(已定位,勿重复排查)

`GET /refunds/{id}` 的 `RefundOut.created_at/decided_at` 是 naive UTC datetime,
FastAPI 序列化为无 Z 后缀 ISO 串(如 `2026-11-11T22:28:00`);
`frontend/ui.js:43 fmtClock` 直接 `new Date(iso)`,JS 对无时标串按**本地时间**解析 → 显示 UTC 原值。
工单头部 `created_at_utc` 字段+`fmtDateTime` 是另一条正确路径,不受影响。
买家侧(app.js)与工作台侧(agent.js:553)共用 ui.js,一处修复双端生效。

## 修复内容(只动 ui.js 与测试)

1. `frontend/ui.js` `fmtClock`:对形如 `YYYY-MM-DDTHH:MM(:SS)(.fff)?` 且**无** `Z`/`±HH:MM` 时标的串,
   补 `Z` 后再 `new Date`(即按 UTC 解析,再取本地时分);带时标的串维持原行为;非法值仍返回空串。
2. `backend/tests/test_frontend_polish.py` 的 W15 时间戳用例(`test_w15_stepper_timestamp_local_hhmm` 附近):
   新增/改为覆盖 **naive UTC 输入**断言换算正确(用固定 UTC 串+断言渲染文本=对应本地 HH:MM,
   测试环境 TZ 处理沿用既有假 DOM 探针手法;若探针跑在 UTC 宿主,断言应容忍 TZ 参数化——以能证明
   "无 Z 串被按 UTC 解析"为准,例如显式设 TZ 或断言 fmtClock('2026-01-01T00:00:00') 在 TZ=Asia/Shanghai 下为 08:00)。
3. 不动 app.js/agent.js 调用点、不动后端序列化(改 API 字段属白名单外大动作,不做)。

## 验收(主会话亲测)

1. 全量 pytest 零回归(基线收集 744)+ 新断言绿;check_w1 10/10。
2. 回执 §7.4 ≤150 行:五段+修改点文件:行+测试名+一条"naive UTC→本地"手工演算示例。

## 熔断

同一报错自愈 2 次不过→停,Block 报告;严禁并行扇出;沙箱禁 playwright/powershell。
