"""W14 前端 UI/UX 体验包:静态 DOM 快照 + 关键字断言。

本环境跑不了 playwright/headless(沙箱限制),因此前端行为统一用**源码级断言**证明:
- DOM 结构:页面里应存在的 id / class / aria / 文案,不应存在的调试字段;
- 展示层映射:长 UUID → 短码、英文枚举 → 中文、金额分 → 元、调试字段 → 业务话术;
- 交互契约:确认卡定时器释放、刷新恢复、队列差量、知识库守卫等关键代码路径。

真截图由主会话用自有 playwright 补拍,本文件同时导出一份 DOM 快照到
`.tmp/w14/dom_snapshots.json` 作为结构证据。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


CHAT_HTML = _read("chat.html")
AGENT_HTML = _read("agent.html")
LOGIN_HTML = _read("login.html")
APP_JS = _read("app.js")
AGENT_JS = _read("agent.js")
CSS = _read("style.css")


# ---------------------------------------------------------------------------
# ISS-01 / 04 / 57:品牌拟人化
# ---------------------------------------------------------------------------


def test_brand_name_on_all_pages():
    """ISS-01:三页统一使用具亲和力的业务品牌名,不再出现工程代号。"""
    for name, html in (("chat.html", CHAT_HTML), ("agent.html", AGENT_HTML), ("login.html", LOGIN_HTML)):
        assert "安心退" in html, f"{name} 缺少品牌名"
        assert "售后 Copilot" not in html, f"{name} 仍残留工程代号"


def test_role_svg_marks():
    """ISS-01:三个角色各用 SVG 图形标(买家/坐席/登录),不再是单字方块。"""
    for name, html in (("chat.html", CHAT_HTML), ("agent.html", AGENT_HTML), ("login.html", LOGIN_HTML)):
        assert '<span class="brand-mark' in html, f"{name} 缺少品牌标容器"
        mark = html.split('class="brand-mark', 1)[1][:600]
        assert "<svg" in mark, f"{name} 品牌标必须是 SVG 图标"


def test_favicon_is_graphic_brand():
    """ISS-57:三页 favicon 统一为无文字图形标(不含「客」字)。"""
    for name, html in (("chat.html", CHAT_HTML), ("agent.html", AGENT_HTML), ("login.html", LOGIN_HTML)):
        line = [ln for ln in html.splitlines() if "rel=\"icon\"" in ln][0]
        assert "image/svg+xml" in line, f"{name} favicon 应为 SVG"
        assert ">客<" not in line, f"{name} favicon 仍是单字方块"


def test_welcome_screen_copy():
    """ISS-04:欢迎屏文案业务化、中文标点、无「转人工」快捷词。"""
    assert "您好，我是「安心退」智能管家。" in CHAT_HTML
    assert "把繁琐售后交给我，把放心结果还给你。" in CHAT_HTML
    examples = re.findall(r'class="example-chip"[^>]*data-text="([^"]+)"', CHAT_HTML)
    assert len(examples) == 3
    assert all("转人工" not in text for text in examples), "快捷词不应引导用户转人工"
    assert "empty-badge" in CHAT_HTML


def test_chat_footer_no_debug_meta():
    """ISS-03:页脚去掉 MOCK / 原生技术栈 / 不接支付物流等开发话术。"""
    assert "assure-line" in CHAT_HTML
    assert "全天候服务保障" in CHAT_HTML
    for banned in ("MOCK", "原生 HTML", "不接真实支付", "seed"):
        assert banned not in CHAT_HTML, f"聊天页脚仍暴露 {banned}"


# ---------------------------------------------------------------------------
# ISS-02 / 23 / 68 / 08 / 22:聊天页调试暴露与气泡语序
# ---------------------------------------------------------------------------


def test_no_trace_in_chat_dom():
    """ISS-02/23:右上角 trace 占位与 32 位哈希不得出现在 DOM;console 可留。"""
    assert 'id="trace"' not in CHAT_HTML
    assert "trace: -" not in CHAT_HTML
    assert "els.trace" not in APP_JS
    # 仅允许 console.debug 记录,不渲染给用户
    assert 'console.debug("[trace]"' in APP_JS


def test_handoff_uses_inline_sync_note():
    """ISS-08/22:转人工不再插大块系统气泡,改为气泡底部浅灰微标。"""
    assert "addSyncNote" in APP_JS
    assert '"✓ 留言已同步至工单"' in APP_JS
    assert '"✓ 已同步至坐席"' in APP_JS
    assert "sync-note" in CSS
    # 顶部常驻提示仍在(test_session_scope 亦依赖该串)
    assert "留言将追加到人工工单" in APP_JS


def test_confirm_request_creates_ai_bubble_first():
    """ISS-68:确认卡永远位于 AI 气泡下方,且不渲染空壳气泡。"""
    assert 'textContent = "(本轮无自动回复)"' not in APP_JS
    block = APP_JS.split('case "confirm_request"')[1][:600]
    assert "ui.ensureAi" in block


# ---------------------------------------------------------------------------
# ISS-07 / 56:回执业务化(短码、中文状态、元)
# ---------------------------------------------------------------------------


def test_receipt_business_copy():
    assert "function shortCode" in APP_JS
    assert ".slice(-6)" in APP_JS
    assert "业务单号 " in APP_JS
    assert "退款金额 " in APP_JS
    assert "receiptStatusText" in APP_JS
    for text in ("排队中", "已确认", "已拒绝", "已超时"):
        assert text in APP_JS
    assert "amount_cents" in APP_JS and "/ 100" in APP_JS
    for banned in ("verified:true", "policy_search 函数"):
        assert banned not in APP_JS
    # 时间必须经统一格式化后才展示,不直接输出 ISO 原文
    assert "fmtTime(receipt.occurred_at_utc)" in APP_JS


def test_receipt_no_long_uuid_or_cents():
    """回执不得直接拼接 36 位 UUID 或输出「xx 分」。"""
    assert "shortCode(id)" in APP_JS
    assert " 分)" not in APP_JS


# ---------------------------------------------------------------------------
# ISS-35 / 54:确认卡倒计时释放与失败恢复
# ---------------------------------------------------------------------------


def test_confirm_timer_cleanup():
    assert "function stopTick" in APP_JS
    assert "clearTimeout" in APP_JS
    decide = APP_JS.split("function decide(")[1][:1600]
    assert "entry.stopped = false" in decide, "失败时必须解除倒计时锁"
    assert "tick()" in decide, "失败后必须重启倒计时"
    on_ws = APP_JS.split("function onWsConfirmStatus(")[1][:900]
    assert "clearTimeout" in on_ws


# ---------------------------------------------------------------------------
# ISS-51:刷新恢复
# ---------------------------------------------------------------------------


def test_refresh_restore_pending_card_and_orders():
    assert "/api/c/confirm/pending?thread_id=" in APP_JS
    assert "restorePendingConfirm" in APP_JS
    assert 'apiFetch("/orders")' in APP_JS
    assert "o.customer_id === CUSTOMER_ID" in APP_JS
    boot = APP_JS.split("function bootSession(")[1][:700]
    assert "restorePendingConfirm()" in boot and "loadMyOrders()" in boot
    # 渲染人工消息前先收起空态
    poll = APP_JS.split("function pollSummary(")[1][:2500]
    assert "hideEmpty()" in poll


# ---------------------------------------------------------------------------
# ISS-36 / 34 / 37:滚动、富文本、条款引用
# ---------------------------------------------------------------------------


def test_scroll_only_when_near_bottom():
    assert "function nearBottom" in APP_JS
    assert "< 120" in APP_JS.split("function nearBottom")[1][:300]


def test_rich_text_rendering():
    assert "function renderRichText" in APP_JS
    assert 'el("strong"' in APP_JS
    assert "bubble-line" in APP_JS and "bubble-line" in CSS
    assert "bubble-item" in APP_JS and "bubble-item" in CSS


def test_citations_open_single_card():
    assert 'setAttribute("aria-expanded"' in APP_JS
    assert "clause-card" in APP_JS


# ---------------------------------------------------------------------------
# ISS-41:我的订单快捷卡
# ---------------------------------------------------------------------------


def test_order_strip_dom():
    assert 'id="orderStrip"' in CHAT_HTML and 'id="orderChips"' in CHAT_HTML
    assert "order-chip" in CSS and "order-strip" in CSS
    assert "ORDER_STATUS_TEXT" in APP_JS
    for text in ("待付款", "待发货", "待收货", "已完成", "已退款", "已取消"):
        assert text in APP_JS


# ---------------------------------------------------------------------------
# ISS-09:登录页
# ---------------------------------------------------------------------------


def test_login_page_business_copy():
    assert "密码" in LOGIN_HTML
    assert "口令" not in LOGIN_HTML
    assert "快速体验通道" in LOGIN_HTML
    assert "体验买家" in LOGIN_HTML and "值班坐席" in LOGIN_HTML
    # 一键填入仍写真实账号
    for account in ("buyer_demo", "agent_demo"):
        assert f'data-username="{account}"' in LOGIN_HTML


# ---------------------------------------------------------------------------
# ISS-10 / 25 / 31 / 12 / 18:工作台话术
# ---------------------------------------------------------------------------


def test_workbench_no_dev_jargon():
    joined = AGENT_HTML + AGENT_JS
    for banned in ("热更新", "向量维", "BM25", "MOCK 规则模板", "draft 草稿", "retired 退役"):
        assert banned not in joined, f"工作台仍暴露开发术语: {banned}"
    assert "重新发布" in AGENT_HTML
    assert "已停用" in AGENT_HTML and "已停用" in AGENT_JS
    assert "新建模式" in AGENT_HTML or "新建模式" in AGENT_JS
    assert "编辑模式" in AGENT_JS


def test_topbar_visual_hierarchy():
    """ISS-25/31:导航用胶囊、状态纯文本、退出用危险色。"""
    assert "topbar-nav" in AGENT_HTML and "nav-pill" in AGENT_HTML
    assert ".nav-pill" in CSS
    assert ".conn-badge {" in CSS and "border: 0;" in CSS.split("W14 前端 UI/UX 体验包")[1]
    assert ".topbar-exit" in CSS and "color: var(--danger);" in CSS


def test_conn_badge_business_wording():
    """ISS-31:不再出现「实时已连接」等实现细节。"""
    assert "实时已连接" not in APP_JS
    assert "实时已连接" not in AGENT_JS
    assert "等待坐席接入中…" in APP_JS
    assert "服务已连接" in AGENT_JS


def test_suggest_panel_copy():
    """ISS-12/18:AI 建议回复,无 MOCK 标签,占位符去演示话术。"""
    assert "AI 建议回复" in AGENT_HTML
    assert "输入回复内容，Enter 发送，Shift+Enter 换行……" in AGENT_JS
    assert "演示环境会伪造消息" not in AGENT_HTML
    assert "enhanceSuggestedReply" in AGENT_JS


# ---------------------------------------------------------------------------
# ISS-15 / 16:转人工原因与建议增强
# ---------------------------------------------------------------------------


def test_handoff_reason_dictionary():
    for key in ("amount_dispute", "logistics_delay", "quality_defect", "explicit_request", "complaint"):
        assert key in AGENT_JS
    assert "退款金额争议" in AGENT_JS
    assert "物流超时未更新" in AGENT_JS
    assert "商品瑕疵" in AGENT_JS
    assert "暂未标注原因" in AGENT_JS
    reason_block = AGENT_JS.split("function reasonLabel")[1][:400]
    assert '"—"' not in reason_block


def test_suggestion_summary_fallback():
    assert "function ticketSummary" in AGENT_JS
    assert "function enhanceSuggestedReply" in AGENT_JS


# ---------------------------------------------------------------------------
# ISS-17 / 29 / 32 / 40:队列
# ---------------------------------------------------------------------------


def test_queue_short_code_and_counts():
    assert "function shortCode" in AGENT_JS
    assert "renderQueueCounts" in AGENT_JS
    assert "chip-count" in AGENT_JS and "chip-count" in CSS
    assert '"● 新消息"' in AGENT_JS and "new-dot" in CSS
    assert "ticket-summary" in CSS and "text-overflow: ellipsis" in CSS
    # 始终拉全量,客户端过滤,计数才可能准确
    load = AGENT_JS.split("async function loadQueue(")[1][:900]
    assert "status=" not in load


def test_queue_filter_rerenders_locally():
    assert "state.statusFilter" in AGENT_JS
    click = AGENT_JS.split("els.queueFilters.addEventListener")[1][:900]
    assert "renderQueue()" in click


# ---------------------------------------------------------------------------
# ISS-27 / 43:回复区与未领取锁定
# ---------------------------------------------------------------------------


def test_reply_tabs_and_independent_status():
    assert "reply-tabs" in AGENT_JS and ".reply-tabs" in CSS
    assert "公开发送给买家" in AGENT_JS
    assert "内部备忘（仅坐席可见）" in AGENT_JS
    assert 'id="replyStatus"' in AGENT_JS or "replyStatus" in AGENT_JS
    assert "reply-status" in CSS
    assert 'statusSelect.title = "状态随领取/关单自动流转"' in AGENT_JS


def test_queued_ticket_locks_reply():
    assert "reply-locked" in AGENT_JS and ".reply-locked" in CSS
    assert "请先领取工单后回复" in AGENT_JS


# ---------------------------------------------------------------------------
# ISS-53:静默刷新,保护草稿与滚动
# ---------------------------------------------------------------------------


def test_detail_silent_refresh():
    assert "function refreshDetailInPlace" in AGENT_JS
    assert "silent: true" in AGENT_JS
    assert "scrollTop" in AGENT_JS


# ---------------------------------------------------------------------------
# ISS-30 / 38 / 39 / 44 / 52:知识库
# ---------------------------------------------------------------------------


def test_kb_editor_sticky_and_search():
    assert ".kb-editor { top: 80px; }" in CSS
    assert 'id="kbSearch"' in AGENT_HTML
    assert 'id="kbFilters"' in AGENT_HTML
    assert 'data-kb-filter="retired"' in AGENT_HTML
    assert "kbFilteredItems" in AGENT_JS


def test_kb_discard_guard():
    assert "function kbConfirmDiscard" in AGENT_JS
    assert "当前条款还有未保存的修改，确定放弃吗？" in AGENT_JS
    assert "window.confirm(" in AGENT_JS


def test_kb_clause_validation():
    assert "RP|LP" in AGENT_JS
    assert "条款号格式：RP-数字（退货）或 LP-数字（物流）" in AGENT_JS


def test_kb_patch_keeps_clause_id():
    """ISS-52:编辑保存不得丢弃 clause_id。"""
    assert "delete body.clause_id" not in AGENT_JS
    save = AGENT_JS.split("async function kbSaveForm(")[1][:700]
    assert "clause_id" in save


# ---------------------------------------------------------------------------
# ISS-24 / 33:响应式与无障碍
# ---------------------------------------------------------------------------


def test_workbench_full_width():
    """ISS-24:1440px 设计宽度上限居中;更窄视口自动 100%(需求允许 ≈1440px 或 100%)。"""
    assert ".workbench { max-width: 1440px" in CSS
    assert "max-width: 1440px; margin: 0 auto" in CSS
    assert ".wb-main { max-width: none; }" not in CSS, "不得保留无效媒体查询"


def test_a11y_contrast_and_min_font():
    assert ".meta { font-size: 12px; color: #5F5852; }" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert "badge-breathe" in CSS


# ---------------------------------------------------------------------------
# 契约:跨角色入口与既有 id 不得丢失
# ---------------------------------------------------------------------------


def test_cross_role_links_preserved():
    assert 'href="/agent.html"' in CHAT_HTML
    assert 'href="/chat.html"' in AGENT_HTML


def test_dom_snapshot_export():
    """导出三页 DOM 结构快照(标签/id/class/可见文案摘要),供验收与回归比对。"""
    snapshot = {}
    for name, html in (("chat", CHAT_HTML), ("agent", AGENT_HTML), ("login", LOGIN_HTML)):
        ids = sorted(set(re.findall(r'\bid="([^"]+)"', html)))
        classes = sorted(set(re.findall(r'\bclass="([^"]+)"', html)))
        text = re.sub(r"<script[\s\S]*?</script>", " ", html)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        snapshot[name] = {
            "ids": ids,
            "classes": classes,
            "text_len": len(text),
            "text_head": text[:400],
            "has_svg_brand": 'class="brand-mark' in html and "<svg" in html,
        }
    out_dir = ROOT / ".tmp" / "w14"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dom_snapshots.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert snapshot["chat"]["has_svg_brand"] and snapshot["agent"]["has_svg_brand"]
    assert "安心退" in snapshot["chat"]["text_head"] + snapshot["login"]["text_head"]
