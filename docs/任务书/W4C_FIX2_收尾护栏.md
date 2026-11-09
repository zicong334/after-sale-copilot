# 任务书 W4-C-FIX2:收尾护栏(两个系统性残余缺口)

> 先读 `docs/任务书/00_通用约定.md`(§6 沙箱护栏/§7 次模守则/熔断 2 次)并全程遵守。
> 背景:第二轮真实评测(docs/最终验收报告.md §3)残余 11 次失败,其中 2 条用例 0/3 系统性失败,用户已授权修复(D-11/裁决 B)。**5 次采样波动散发失败不在本任务书范围,不许为它们加特殊逻辑。**
> **红线:全程禁用真实 API(.env 的 key 不许读);不许改 data/eval/ 任何文件(断言冻结);禁止 case_id 特判——修复必须是通用机制,不是对着两条用例打补丁。**

## 冻结基线

commit(见 git log 最新):334 测试全绿;check_w1_cases 10/10;Fake 全量评测 0.9792;评测集 cs-eval-v1-20261108b(冻结)。

## FIX-8 越权查单的意图与标准话术(CS-REFUND-006 类,失败: intent ×3、must_contain ×1)

- 现象:查询他人订单时,FIX-1 强制核验链已生效(工具被调、返回 NOT_FOUND_OR_FORBIDDEN、零泄露),但真实模型 ①intent 分类漂移(判成 other/policy_qa),②拒绝话术自由发挥,未含标准短语。
- 修法(确定性,两层):
  1. **classify 高置信快捷路径**(设计文档允许"规则只做高置信快捷路径"):输入文本或 context 含订单号形态(guardrails 已有 pattern)且含查询/退/换/责类词 → 规则直判 `order_judge`(confidence=1.0, path=rule),不进 LLM 分类;
  2. **compose 标准拒绝话术拼接**:本轮 trace 中存在 `shop__get_order` 返回 `NOT_FOUND_OR_FORBIDDEN` 时,回复必须包含常量模板句(如"未能在当前会话下找到该订单";模板进 guardrails.py 常量区),模型话术只做补充不做替代。
- 测试:规则快捷路径三分支(命中/不命中/仅订单号无诉求词);NOT_FOUND 后回复含模板句(用 FakeLLM 故意输出不含模板的话术,断言被拼接)。

## FIX-9 记忆引用的确定性保真(CS-MEMORY-001 类,失败: must_contain"尺码" ×3)

- 现象:偏好记忆注入正常、隔离正常,但真实模型回复 3/3 未逐字引用偏好关键短语。
- 修法:复用 FIX-5 引用保真手法——compose 后置校验:State.memory 注入了 preference 且本轮意图与 scope 相关(尺码/推荐类)时,回复必须逐字包含偏好文本的关键短语(从注入文本提取,提取函数进 guardrails.py);缺失 → 确定性拼接一句引用("根据您的偏好记录:…")。
- 测试:注入偏好+相关意图 → 回复含关键短语(FakeLLM 故意不含,断言被拼接);注入偏好+无关意图(如问政策)→ 不强行拼接;空记忆 → 不拼接。

## 产物白名单

```
backend/app/guardrails.py(常量+纯函数扩展)
backend/app/graph/nodes.py(classify 快捷路径/compose 两处拼接)
backend/app/llm/base.py(仅 FakeLLM 测试配合规则,MOCK 标注保持)
backend/tests/test_guardrails.py / test_graph.py / test_chat_route.py(增量)
backend/README.md(7.1 护栏清单补两条)
```

## 回归红线(收尾全跑,一项不过即熔断汇报)

1. 全量 pytest ≥334+新增 全绿(FakeLLM 密封,零真实 API)
2. check_w1_cases.py 10/10 不倒退
3. run_eval.py --smoke(Fake)通过率 ≥ 第一轮同口径(15/15)
4. grep 新增代码无 TODO/FIXME、无 case_id 字面量特判(`grep -n "REFUND-006\|MEMORY-001\|CS-" 改动的源码文件` 应为空)
5. data/eval/ 与 .env 零触碰(git status 佐证)

## 回执(§4 五段+专项)

- 每项 FIX:文件:行号 + 专项测试名 + 输出原文
- 机制通用性自证:说明两处守卫对"非评测用例的同类输入"同样生效的测试(自造输入,不用 CS- 编号)
- 收尾:全量测试/check_w1/smoke 三件输出原文
