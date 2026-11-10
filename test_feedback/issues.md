## sheet: 你帮我弄一个工单表格吧，把这些修改项都放到里面，作为测试身份一 (71x26)
工单编号 | 所属页面 / 模块 | 缺陷类型 | 严重程度 | 问题现象 (Current) | 预期表现 (Expected) | 建议修改位置 / 说明
ISS-01 | 全局 / 品牌标识 | 品牌认知 | P2 | 项目名称仍为技术味浓重的“售后 Copilot”，左上角使用单字「客」「台」「登」小方块，无辨识度。 | 1. 品牌更名为更具业务亲和力的名称（如：「安心退」 / 「智赔宝」）。2. 替换为统一的高清矢量 SVG 图标（买家端：智能助手；坐席端：控制台/工单图标；登录端：品牌主 Logo）。 | chat.htmlagent.htmllogin.html
ISS-02 | 买家聊天页 (chat.html) | 调试暴露 | P2 | 右上角直接显示开发排查用的 trace: - 字段。 | 普通消费者端完全隐藏 trace 调试字段；若排查需要，仅在控制台或特定错误卡片内部留存。 | chat.htmlapp.js
ISS-03 | 买家聊天页 (chat.html) | 调试暴露 | P2 | 页面右下角长文本提示“本页为原生 HTML/CSS/JS……MOCK 演示环境：无登录密码，数据来自 seed；不接真实支付与物流”，破坏沉浸感。 | 移除所有面向开发者的 MOCK 及 seed 说明，替换为标准的消费级保障文案（如：本服务由智能售后大模型提供支持 · 全天候服务保障）。 | chat.html 底部 .meta 区域
ISS-04 | 买家聊天页 (chat.html) | 视觉 / 语义 | P3 | 1. 欢迎屏主图标为带三个点的对话气泡，呆滞无 AI 属性。2. 欢迎词使用了半角英文逗号 你好,我是售后 Copilot。3. 介绍语着重强调了“需要人工接入可说转人工”。 | 1. 替换为带呼吸微光的双星 AI / 助手徽章图标。2. 修复中文标点：您好，我是「安心退」智能管家。3. 介绍语强化解决力：“把繁琐售后交给我，把放心结果还给你。” 胶囊快捷词剔除转人工，改为真实业务高频场景。 | chat.html 欢迎卡片区域
ISS-05 | 业务逻辑 / 对话流 | 业务逻辑硬伤 | P0 | 未付款/未发货的 created 状态订单，买家诉称“衣服有线头要求退货”，AI 居然判定为“因未签收 delivered 所以拒绝退货”，并向买家直吐英文底层状态。 | 1. 严禁输出英文枚举，必须转译为中文（待付款/待发货/已签收）。2. 业务逻辑校准：未发货状态不可能收到商品，应引导用户核对单号；若确需拦截，应提示未发货可极速全额取消退款，而非生硬驳回。 | backend/app/graph/nodes.pycompose 节点与 Prompt
ISS-06 | 业务逻辑 / 意图识别 | 交互体验 | P1 | 用户明确输入 人工客服，系统未能触发转人工确认卡，而是机械复读欢迎引导菜单，导致用户输入第二次才触发。 | 在意图分类层（Fast-Path 规则库）将 人工客服、转人工 设为强匹配硬规则，命中后直接挂起并弹出确认卡，禁止二次多轮空转。 | backend/app/guardrails.py意图路由规则
ISS-07 | 买家聊天页 (chat.html) | 调试暴露 | P1 | 1. 确认卡替代方案直接暴露函数名 policy_search。2. 退款/工单回执单直接打印出 36 位长 UUID、verified:true、queued 及长 trace 串。 | 1. 替代方案改为人话：您也可直接描述问题，由 AI 极速为您自助办理。2. 隐藏技术字段，UUID 截取为后 6 位工单号（如 #649725），状态汉化为“排队中”。 | backend/app/tools/handoff.pybackend/app/schemas/receipt.py
ISS-08 | 买家聊天页 (chat.html) | UI / 交互 | P2 | 转人工后买家追加留言，下方仅有一行居中的浅橙色极小字提示“留言已追加到人工工单……”，视觉微弱且割裂。 | 移除散落的独立文字，在买家发送的气泡下方直接附带浅灰微型状态标 ✓ 留言已同步至工单，保持信息流一致。 | frontend/app.js
ISS-09 | 登录页面 (login.html) | 规范性 | P2 | 1. 密码字段命名为古早生硬的“口令”。2. 快捷账号按钮直露开发变量 buyer_demo、agent_demo。3. 页面满屏“演示环境”、“演示口令”提示，无系统感。 | 1. 统一修改为常规的“密码”。2. 快捷账号封装为人性化标签（如：体验买家-张三、值班坐席-1001）。3. 弱化演示字样，收纳为底部独立的“快速体验通道”组件。 | login.htmllogin.js
ISS-10 | 客服工作台 (agent.html) | 术语 / 黑话暴露 | P1 | 1. “热更新”术语泛滥成灾（按钮叫热更新、提示叫热更新不可见）。2. 条款状态将已下线称为“退役/恢复退役”。3. 数据库英文状态 draft/active/retired 未汉化直接写在副标题。 | 1. 彻底剔除“热更新”黑话；保存自动同步生效，手动刷新按钮改叫“重新发布”。2. 条款停用/下线统一命名为**「已停用」，操作按钮为「停用」与「重新启用」**。3. 抹除所有英文枚举，副标改为纯中文操作指引。 | agent.htmlagent.js
ISS-11 | 客服工作台 (agent.html) | 数据与脱敏 | P2 | 1. 统计栏出现莫名其妙的脏字段 准确率 15。2. 顶栏显示语病文本 已登坐席 agent_demo(seed-agent-1)。3. 左侧列表直接展示 36 位原生 UUID。 | 1. 清理算法参数残留，统计栏规范为：`全部 18 | 生效 16
ISS-12 | 客服工作台 (agent.html) | 交互 / 智能体体验 | P2 | 1. 坐席回复输入框占位符写着“演示环境中会伪造消息”。2. 下方 Copilot 辅助建议带 [MOCK 估算] 标签，且推荐文本把分析逻辑和对买家的话混杂在一起。 | 1. 占位符规范为：输入回复内容，Enter 发送，Shift+Enter 换行...。2. 移除 MOCK 标签，更换为 ✨ AI 建议回复，建议内容只输出干练可直接发送给买家的话术。 | agent.htmlagent.js
ISS-13 | 会话转录 (agent.html) | 数据断层硬伤 | P0 | 会话转录区域只有转人工后追加的 1~2 句消息，缺失前序买家与 AI 机器人的完整交互记录。坐席完全看不见用户之前说了什么。 | 必须实现前序对话全量穿透：工作台拉取消息时，需按 session_id 联查 chat_message 与 ticket_message。前半截用灰色/机器人角标展示 AI 问答，后半截展示转人工后的人工对话，完整复原案发现场。 | backend/app/api/tickets.py ⏎  ⏎ services/tickets.py 消息拉取逻辑
ISS-14 | 诉求摘要 (agent.html) | 逻辑瘫痪 | P0 | 几乎所有工单的诉求摘要大卡片均显示 暂无摘要(转人工时未生成)，摘要功能完全失效。 | 1. 转工单时必须触发一次轻量 LLM/规则摘要提取（提炼买家核心诉求、订单号、涉诉原因）。 ⏎  ⏎ 2. 若实在为空，前端应优雅降级为提取买家首句诉求文本，严禁直接把“转人工时未生成”这种内部异常文字甩在卡片上。 | backend/app/tools/handoff.py ⏎  ⏎ frontend/agent.js 渲染逻辑
ISS-15 | 转人工原因 (agent.html) | 数据与语义 | P1 | 1. 多个工单的转人工原因为空（显示 -）。 ⏎  ⏎ 2. 退款申请、物流问题等不同诉求全部粗暴归类为 其他原因。 | 1. 细化并校准 handoff_reason 映射字典，支持标准业务分类（如：退款争议、物流超时、商品瑕疵、买家明确要求转人工）。 ⏎  ⏎ 2. 修复 seed.py 中历史工单的分类字段，杜绝出现空值 -。 | backend/app/tools/handoff.py ⏎  ⏎ backend/scripts/seed.py
ISS-16 | Copilot 智能建议 | 智能化不足 | P1 | 生成的 AI 建议回复完全千篇一律（全是“已收到问题，尽快核实处理，请以实际为准”等套话废话），毫无根据当前工单具体问题的针对性。 | 增强 Prompt 动态生成能力：把工单关联订单号、前序对话关键摘要、匹配到的退换货条款动态喂给建议生成器，生成具体有针对性的答复话术（如：“您好，关于订单 seed-order-012 的退款，我们需要核实退货物流单号...”）。 | backend/app/api/tickets.py ⏎  ⏎ 或 services/copilot_service.py
ISS-17 | 工单队列列表 | UI / 体验 | P2 | 左侧列表很多工单直接显示大字 无摘要，且状态徽章（如“待处理”、“跟进中”）缺乏业务场景信息，信息密度过低。 | 列表副标题截取真实对话首句（如：“衣服有线头要求退款”），去掉突兀的“无摘要”文本；列表项附带买家姓名与核心诉求标签。 | frontend/agent.js
ISS-18 | 工作台操作栏 | 调试用语暴露 | P2 | 坐席回复框占位符写着 以【人工客服】身份回复买家...（演示环境中会伪造消息），技术调试味道太浓。 | 移除括号内全部开发黑话，占位符规范为标准的：输入回复买家的内容（Enter 发送，Shift + Enter 换行）... | frontend/agent.html ⏎  ⏎ #agentReplyText
ISS-19 | 全局路由 / 鉴权 | 死循环拦截 (致命) | P0 | 在买家页面点击“客服工作台”，或在工作台点击“买家聊天”，均被强制重定向踢回 login.html 登录页，无法进行页面互通。 | 1. 修复角色鉴权校验逻辑，沙箱/联调模式下允许在已有会话状态下平滑跨角色查看，或在跳转链接中自动透传对应的演示身份 Token。 ⏎  ⏎ 2. 修复 href 路径缺失当前 session_id / merchant_id 参数导致的上下文丢失。 | frontend/login.js ⏎  ⏎ frontend/app.js ⏎  ⏎ frontend/agent.js
ISS-20 | 业务闭环 / 状态机 | 功能缺失 | P1 | 用户在已转人工状态下发送 我想转AI客服 / 返回机器人，系统没有任何响应，依然机械地把该指令作为留言追加到工单中，导致用户被永久困在人工状态。 | 增加入工反向唤醒机制（逆向路由）： ⏎  ⏎ 当检测到会话处于 handed_off 且用户明确输入“退出人工 / 转回AI / 结束人工”时，触发软拦截：将工单标记为 cancelled 或释放，会话状态切回 active，AI 重新接管回复。 | backend/app/graph/nodes.py ⏎  ⏎ backend/app/api/chat.py 路由守卫
ISS-21 | 话术合规 / 脱敏 | 业务合规 | P1 | 人工坐席（包括 Copilot 推荐）回复买家的话术中，直接包含了内部政策条款代号 (RP-06、LP-04)，直接暴露内部法务索引。 | 1. 净化面向消费者的文本：禁止输出任何 RP-xx、LP-xx 格式的英文代号，必须转义为通俗业务名称（如“运费险条款”、“商品瑕疵政策”）。 ⏎  ⏎ 2. 建议回复生成器（Copilot）在生成对客文本时强制做条款代码剔除过滤。 | backend/app/services/copilot_service.py ⏎  ⏎ 或对应 Prompt 约束
ISS-22 | 买家聊天页 (chat.html) | 视觉 / 冗余 | P2 | 页面顶部已有黄条提醒转人工，聊天流中央又反复插播黄色系统气泡，出现“提示轰炸”与视觉打架。 | 1. 顶栏常驻黄色 Banner 即可，聊天流内无需反复追加居中的大块黄色提示气泡。 ⏎  ⏎ 2. 买家追加留言时，去掉下方长达两行的 #974e1bff... 冗余解释文本，改为气泡底部简洁微标 ✓ 已同步至坐席。 | frontend/app.js ⏎  ⏎ renderSystemNotice 逻辑
ISS-23 | 买家聊天页 (chat.html) | 调试暴露 | P2 | 右上角直接裸露 32 位长追踪哈希 trace: 6c3fe0261154460cda9a2701464e3462。 | 彻底移除该 DOM 展示；生产级买家端严禁出现技术排障码。 | frontend/chat.html 顶栏
ISS-24 | 客服工作台 (agent.html) | 布局 | P2 | 工作台采用固定居中窄容器，屏幕左右两侧存在大面积无意义留白，左侧工单信息被挤压变形。 | 改为流式全屏响应式布局（max-width: 1440px 或 100%），提升坐席审单的信息密度。 | 
ISS-25 | 买家聊天页 (chat.html) | 状态语义 | P1 | 顶栏左侧显示“已转人工”，右侧却挂着带绿点的“实时已连接”，导致买家误以为坐席已在线接听。 | 抹除网络连接状态，转为业务连接状态：显示为等待坐席接入中...。 | 
ISS-26 | 客服工作台 (agent.html) | 业务安全 | P1 | “关闭工单”操作为红色裸按钮，点击即生效，缺乏确认弹窗与结单原因填写，极易误触关单导致买家端被锁死。 | 增加结单 Secondary Modal 弹窗，要求坐席确认并选择/填写关闭原因。 | 
ISS-27 | 客服工作台 (agent.html) | 交互逻辑 | P2 | “内部备注”复选框强行绑定了“工单状态设为跟进中”，将内容类型与状态流转混淆在一起。 | 拆解逻辑：输入区提供【公开发送】与【内部备忘】独立选项卡；工单状态由独立的下拉菜单控制。 | 
ISS-28 | 全局通用 | 规范性 | P2 | 全系统时间戳格式极度混乱，回执卡甚至直出 2026-11-10T08:14:38.313334+00:00。 | 封装统一的 formatTime 管道，统一转换为用户本地友好的自然语言时间（如 今天 16:14），清除毫秒与时区尾巴。 | 
ISS-29 | 客服工作台 (agent.html) | 协同体验 | P1 | 买家在已转工单下追加新留言后，工作台左侧队列没有未读提示、消息红点或自动置顶，坐席难以及时感知买家动态。 | 工单收到买家新追评/留言时，左侧对应卡片增加 ● 新消息 标签并自动漂移置顶。 | 
ISS-30 | 知识库 (agent.html) | CSS 布局 | P1 | 右侧编辑表单设置 position: sticky; top: 80px，防止底部点编辑时看不见表单。 |  | 
ISS-31 | 全局顶栏 (Topbar) | 视觉层级 | P2 | 区分状态指示器（纯文本/无边框）、导航 Tab（胶囊底色）与退出按钮（危险色/图标）。 |  | 
ISS-32 | 工单列表 (agent.html) | CSS 排版 | P2 | 单号与摘要启用单行省略号截断，防止折行挤压右侧状态胶囊与时间。 |  | 
ISS-33 | 全局字体与配色 | 无障碍 (A11y) | P2 | 调深辅助文字与时间戳颜色对比度，最小字号不低于 12px。 |  | 
ISS-34 | 聊天流 (app.js) | 文本格式化 | P1 | 支持对回复中的序号（①②③）、换行符与加粗语法做基础分段格式化渲染。 |  | 
ISS-35 | 确认卡 (app.js) | JS 定时器 Bug | P1 | 用户点击确认或拒绝后，必须调用 clearTimeout 销毁 tick 倒计时定时器。 |  | 
ISS-36 | 聊天流 (app.js) | 交互抢焦点 | P2 | 仅当滚动条位于底部阈值内时才自动滚底，允许用户在生成中途向上翻阅历史。 |  | 
ISS-37 | 引用面板 (app.js) | 交互逻辑 | P2 | 改造 renderCitations，点击 [n] 仅独立高亮/展示对应条款卡片，避免全量混排。 |  | 
ISS-38 | 知识库 (agent.html) | 防误触保护 | P2 | 编辑表单存在未保存修改时，切换条款或新建前弹出“是否放弃未保存修改”确认。 |  | 
ISS-39 | 知识库 (agent.html) | 表单状态 | P2 | 明确区分“新建模式”与“编辑模式”标题，增加条款号格式校验提示。 |  | 
ISS-40 | 工单队列 (agent.html) | 信息密度 | P2 | 在“待领取 / 跟进中 / 待客户回复”等筛选 Tab 上实时展示对应状态的工单数量。 |  | 
ISS-41 | 买家端 (chat.html) | 核心体验缺失 | P1 | 增加“我的订单”侧边栏或顶部快捷选择卡，支持一键点选订单发起售后，免手敲单号。 |  | 
ISS-42 | 工作台 (agent.html) | 业务闭环缺失 | P1 | 在工单详情页接入退款审批操作按钮（同意退款/拒绝退款），打通坐席处理闭环。 |  | 
ISS-43 | 工作台 (agent.html) | 状态守卫 UI | P1 | 未领取（queued）工单禁用回复输入框，并在原位居中提示“请先领取工单后回复”。 |  | 
ISS-44 | 知识库 (agent.html) | 检索筛选 | P3 | 在知识库列表顶部增加关键词搜索框与 全部 / 退货(RP) / 物流(LP) / 已停用 过滤标签。 |  | 
ISS-45 | frontend/app.js ⏎  ⏎ frontend/agent.js ⏎  ⏎ frontend/login.html | 跨页鉴权自杀 (ISS-19根因) | P0 | 两端共用 localStorage 的 sc_token/sc_user；从买家页点“客服工作台”时，agent.js 因 role!=="agent" 调 gotoLogin() 直接 removeItem 删库，导致原买家页也同步掉线。 | 1. 拆分存储键为 sc_buyer_token/user 与 sc_agent_token/user（或 login.html 登录时按角色写入对应槽位）。 ⏎  ⏎ 2. 严禁因跨角色打开页面而清除另一角色的本地凭证，支持同浏览器双标签页并存联调。 | 
ISS-46 | backend/app/cache.py | 多租户缓存串数据 | P0 | SemanticCache._records 仅以 _normalize(text) 为键，未隔离 merchant_id。不同商家的买家问同类政策问题会跨店命中彼此的自定义条款缓存。 | lookup、store、clear 增加 merchant_id 维度（如主键改为 f"{merchant_id}:{normalized}"），严守租户隔离铁律。 | 
ISS-47 | backend/app/cache.py | 停用词误删核心语义 | P0 | STOPWORDS 含单字 "没/会/还/被/把"，且 _strip_stopwords 用 .replace(word, "") 全文子串抹除，把 "没有收到" 删成 "有收到"（否定变肯定）、"会员" 删成 "员"。 | 从 STOPWORDS 中剔除 "没/会/还/被/把/要/能" 等实义构词单字，仅保留句末纯语气词，防止语义反转误命中。 | 
ISS-48 | backend/app/main.py | WebSocket 越权窃听 (BOLA) | P0 | _serve_websocket 的 buyer 分支未校验 session_id 的归属，任意登录买家可连接 /ws/buyer/{他人session_id} 窃听他人实时消息。 | 连接建立前查询 ChatSession，校验 row is not None and row.customer_id == identity.customer_id，不符立即 close(code=4403)。 | 
ISS-49 | backend/app/main.py | 异步队列未启动 | P1 | main.py 的 lifespan 未调用 jobs.start_worker(engine) 与 stop_worker(engine)，导致生产环境下 SQLiteJobQueue 永远退化为同步内联执行。 | 在 main.py 的 lifespan 启动时调用 jobs.start_worker(engine)，退出前调用 jobs.stop_worker(engine)（注意兼容测试 fixture）。 | 
ISS-50 | backend/app/guardrails.py | PII 地址正则吞噬意图 | P1 | PII_ADDRESS_RE 末尾 [\u4e00-\u9fa5A-Za-z0-9 \-]{0,20} 贪婪匹配，用户写 "文三路123号帮我改一下" 时，"帮我改一下" 被当成地址吞进 [ADDR_1]。 | 收紧门牌号后缀正则（限制为数字/号/室/栋/层/单元），禁止无标点时向后吞噬任意中文动词短语。 | 
ISS-51 | frontend/app.js | 刷新页面丢失历史对话 | P1 | bootSession 恢复 sessionId 后未拉取历史 chat_message，且 pollSummary 渲染人工气泡时漏调 hideEmpty()，也没有恢复渲染挂起中的 ConfirmRequest。 | 1. 恢复会话时拉取并渲染历史消息流与 /api/c/confirm/pending 确认卡； ⏎  ⏎ 2. pollSummary 渲染消息前必须调用 hideEmpty()。 | 
ISS-52 | frontend/agent.js | 知识库编辑丢弃条款号 | P1 | kbEditItem 允许修改 kbClauseId 输入框，但 kbSaveForm 在 PATCH 前执行了 delete body.clause_id，导致修改被静默丢弃。 | 移除 delete body.clause_id（后端 PATCH 已支持改 clause_id 升版），或将编辑态下的 kbClauseId 设为 readOnly。 | 
ISS-53 | frontend/agent.js | WS 刷新打断当前审单 | P2 | scheduleQueueRefresh 在收到任何工单 WS 事件时都无条件重调 loadDetail(state.selectedId)，导致详情面板清空闪烁、滚动条归零、Copilot 面板被隐藏。 | 1. 仅当事件 ticket_id === state.selectedId 时才刷新详情； ⏎  ⏎ 2. 静默更新转录消息与状态徽章，禁止清空重建整个 detailBody DOM。 | 
ISS-54 | frontend/app.js | 确认卡报错后倒计时死锁 | P2 | renderConfirmCard 的 decide() 置 entry.stopped = true 后，若接口返回 >=400 报错，未复位 entry.stopped = false 并重启 tick()。 | 请求失败恢复按钮可用时，同步复位 entry.stopped = false 并重新调度 tick()。 | 
ISS-55 | backend/app/cache.py | 缓存无限膨胀与锁阻塞 | P2 | SemanticCache 无容量上限（max_size），且每次 lookup 在 RLock 内对全量缓存重跑 TfidfVectorizer().fit_transform。 | 增加容量上限（如 max_entries=512，超出按 FIFO/LRU 淘汰），避免高并发下锁内 CPU 耗时线性增长。 | 
ISS-56 | backend/app/tools/handoff.py ⏎  ⏎ backend/app/tools/refund.py | 确认卡硬编码分与函数名 | P2 | handoff.py:99-101 硬编码 "queued" 与 "policy__search"；refund.py:183 硬编码 "{amount_cents} 分"。 | 将确认卡文案改为消费者友好表达（如 "¥119.00 元"、"排队等待坐席接入"、"通过政策问答自助查询"），同时保持四要素非空以通过单测。 | 
ISS-57 | frontend/agent.html ⏎  ⏎ frontend/login.html ⏎  ⏎ backend/app/main.py | Favicon 图标全站写死「客」 | P3 | agent.html、login.html 的 data-URI favicon 与 main.py 的 _FAVICON_SVG 全写死汉字「客」，与工作台顶栏的「台」及新品牌标识脱节。 | 统一替换为无文字的图形化品牌矢量 SVG Favicon。 | 
ISS-58 | backend/app/guardrails.py ⏎  ⏎ backend/app/graph/nodes.py | 真评唯一残余失败项 (CS-REFUND-005) | P1 | 含订单号+“投诉”的金额争议被 is_order_query_shortcut 抢先判为 order_judge，且 act 将原因标为 amount_dispute，导致真评 CS-REFUND-005 2/3 失败。 | 在 classify 规则路径中让风险升级词（投诉/12315/曝光）优先判定 intent="handoff"，并在金额争议叠加投诉词时优先映射为 complaint（保持已有 494 单测全绿）。 | 
ISS-59 | backend/app/guardrails.py | 文本排版破坏 (ISS-38根因) | P1 | audit_status_claims 使用 _SENTENCE_RE = r"[^。！？\n]+[。！？]?" 切句后直接 "".join(out)，把 AI 回复中所有的换行符 \n 全部抹除，导致长回复永远挤成一团。 | 修正切句正则以保留换行符 \n，确保多段落与条款原文分段正常展示。 | 
ISS-60 | backend/app/security.py ⏎  ⏎ backend/app/main.py | 未授权访问后门 | P0 | required_role(path) 仅拦截 /api/c/ 与 /api/b/，其余路径一律返回 None。导致 /orders、/tickets、/refunds、/v1/maintenance/decay 在 enforce 模式下完全无鉴权裸奔，/api/kb/* 绕过中间件限流。 | 改为白名单机制：除 PUBLIC_PATHS 外，所有业务与维护路由一律强制鉴权与令牌桶限流。 | 
ISS-61 | backend/app/guardrails.py | 正则误杀正常用户 | P1 | INJECTION_RULES 的 command 规则把普通反引号 `[^`]{1,120}` 与 && 设为 is_hard=True，买家输入 `seed-order-020` 会被当成命令注入硬拦截。 | 收紧 command 与 tool_injection 规则，仅在反引号/分号内包含高危系统命令词时才硬拦截。 | 
ISS-62 | backend/app/tools/registry.py | 线程安全与并发污染 | P1 | ainvoke 将非线程安全的同步 ctx.session 传入 asyncio.to_thread，超时后后台孤儿线程继续读写并 commit() 同一 Session。 | 避免跨线程共享同一 SQLAlchemy Session，或在线程内使用独立短期 Session。 | 
ISS-63 | backend/app/tools/registry.py | 事件序号撞车风险 | P2 | _write_event 写入 ToolEvent 后未执行 ctx.session.flush()（而 SessionLocal 配置了 autoflush=False），同事务连续写事件会导致 _next_step 查出重复 step。 | 与 trace.py 保持一致，在 write_event 后立即调用 session.flush()。 | 
ISS-64 | backend/app/tools/refund.py | 策略门参数未对齐 | P1 | _policy_gate_passed 仅检查同 trace 下 policy__check_return 是否返回过 eligible=True，未核对当时的 order_status 和 reason 是否与本次 refund__create 一致。 | 校验 check_args["reason"] == args["reason"] 且 check_args["order_status"] == order.status。 | 
ISS-65 | backend/app/security.py ⏎  ⏎ frontend/app.js | 时钟偏差误踢用户 | P2 | require_replay_guard 在时间戳偏差 > 5 分钟时返回 401，触发前端 apiFetch 的 status === 401 -> gotoLogin() 逻辑，把用户直接踢出登录；且每次写请求执行全表范围 DELETE。 | 重放时间戳校验失败改返 400/422；过期 Nonce 清理改为低频抽样或后台异步清理。 | 
ISS-66 | backend/app/pii.py | 流式输出卡死隐患 | P2 | MaskingLLM._safe_cut 遇到未闭合的 [ 会一直阻塞后续所有流式切片，直至整段生成结束。 | 当未闭合 [ 距离末尾超过 16 字符（非合法占位符长度）时立即放行输出。 | 
ISS-67 | frontend/app.js ⏎  ⏎ frontend/agent.js | URL 泄露敏感 Token | P2 | WebSocket 连接直接将 JWT 拼在 URL ?token=... 中，导致访问日志明文记录 Token。 | 改用后端 _ws_authenticate 已支持的首帧 {"type": "auth", "token": "..."} 方式完成鉴权。 | 
ISS-68 | frontend/app.js | 气泡语序颠倒与空壳 | P2 | confirm_request 早于文本帧到达时，AI 气泡会渲染在确认卡下方；空回复时渲染生硬的 (本轮无自动回复)。 | 确保 AI 气泡容器始终位于确认卡上方；当 ai_replied===false 时不渲染空壳 AI 气泡。 | 
ISS-69 | frontend/app.js ⏎  ⏎ frontend/agent.js | 无限刷新死循环风险 | P3 | verifySession() 在 setItem 失败被吞掉后仍无条件执行 location.reload()。 | 仅在 setItem 成功且确需重置状态时刷新，或直接就地更新内存中的 ME 变量而不整页 reload。 | 
ISS-70 | backend/scripts/run_eval.py ⏎  ⏎ data/eval/reports/* | 宿主绝对路径泄露 | P3 | 评测报告 JSON 的 meta.cases_file 与 meta.capture 硬编码了宿主机绝对路径 D:\科研工作台\...。 | 报告生成脚本统一转为相对于项目 ROOT 的相对路径（path.relative_to(ROOT).as_posix()）。 | 
