# 任务书 W3-C:确认卡闭环 + 转人工全链路 + 前端聊天壳(SSE)

> 先读 `docs/任务书/00_通用约定.md`。前置:W3-A、W3-B 已验收。

## 目标

买家侧一个聊天页:流式回复、`[n]` 引用可点、确认卡四要素+倒计时+超时拒绝、转人工后界面明确"人工接管中,AI 停止回复";每轮显示真实 token/成本/时延(FakeLLM 阶段显示 mock 计量并标注)。

## 只需读这几段(只读)

1. `docs/PRD_v1.md` §2(转人工出口)、§4 第 5 项(确认卡四要素/超时)
2. G7 卡全文(确认卡内容范例、超时=拒绝留草稿、工单字段、停止 AI 判定点、领单守卫)
3. 正例:`私有化AI助手\backend\services\chat_service.py` 前 80 行(SSE 四类事件)+ `私有化AI助手\frontend\js\chat.js` 第 86-99 行([n] 点击高亮)
4. 反例:`电商客服完整项目\backend\services\chat.py` 第 179-180 行("已为您接入人工"假接管)

## 产物文件(白名单)

```
backend/app/api/confirm.py        # 确认卡 API:GET 待确认卡(按 thread_id)/POST 确认/POST 拒绝;超时判定(10 分钟,服务端时间)=拒绝+留草稿;确认后走 graph resume→invoke_confirmed;卡内容四要素齐全(做什么/影响面/不可逆性/替代方案)
backend/app/api/handoff.py        # 客服侧:GET 工单列表(queued 优先)/POST claim(守卫式,被抢返回 409 提示"已被他人领取")/POST close/GET 会话摘要
backend/app/api/chat.py           # 允许改:SSE 流式(事件四类:message/citation/confirm_request/handoff_notice)+ 每轮 usage{tokens_in,tokens_out,cost_usd,latency_ms}(FakeLLM=按字符数估的 mock 计量,字段带 estimated:true)
backend/app/main.py               # 允许改:挂新路由 + StaticFiles(frontend/)
frontend/chat.html                # 单页:消息流、引用 [n] 点击高亮对应条款、确认卡(四要素+倒计时+确认/拒绝按钮)、handed_off 横幅、每轮 usage 小字
frontend/app.js / frontend/style.css
backend/tests/test_confirm.py     # ≥10:确认→退款单一张+回执 verified;拒绝→无写操作;超时(可注入假时钟)→拒绝+草稿在;重复确认(幂等)→不重复执行;四要素字段齐全断言
backend/tests/test_handoff_flow.py# ≥8:handed_off 后 chat 返回固定话术且图未运行(trace 无新事件);claim 并发一个成功;关单守卫
```

## 验收标准

1. pytest 全量绿,贴输出
2. 无 key 起服务,浏览器或 curl 演示脚本跑完整链路:提问→确认卡出现(贴卡内容 JSON,四要素逐项指认)→确认→回执(verified status)→回复;另一条:转人工→横幅→再发消息 AI 不答→客服 claim→close。贴每步原始响应
3. 超时路径用假时钟测试证明,贴测试输出
4. usage 字段带 estimated:true(FakeLLM 阶段),贴响应原文
5. 前端不引入构建工具(原生 HTML/JS/CSS,CDN 也不许——离线可开)

## 禁止

- 不接真实 LLM;不做 B 端管理页(只有工单列表/claim/close 最小 API,无界面也行,界面在 W4-B 可选)
- 确认卡超时不许默认通过
