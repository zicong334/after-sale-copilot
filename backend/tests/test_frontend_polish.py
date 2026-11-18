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
    assert len(examples) == 4
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


# ===========================================================================
# W15 前端精修 + 工程规范包
#   U1 售后进度 stepper / U2 金额红橙药丸 / U3 队列彩色角标 / U4 快捷词图标
#   U5 通信收拢终章 / U6 轮询瘦身 / U7 辅助模块降级壳
# ===========================================================================

import os  # noqa: E402
import shutil  # noqa: E402  (W15 追加,放在本节顶部便于自检)
import subprocess  # noqa: E402
import uuid  # noqa: E402

API_JS = _read("api.js")
UI_JS = _read("ui.js")
HANDOFF_PY = (ROOT / "backend" / "app" / "api" / "handoff.py").read_text(encoding="utf-8")
CHAT_PY = (ROOT / "backend" / "app" / "api" / "chat.py").read_text(encoding="utf-8")
AGENT_PY = (ROOT / "backend" / "app" / "api" / "agent.py").read_text(encoding="utf-8")
SLA_PY = (ROOT / "backend" / "app" / "sla.py").read_text(encoding="utf-8")
TRACE_PY = (ROOT / "backend" / "app" / "trace.py").read_text(encoding="utf-8")

# 裸 fetch:排除 window.fetch / fetchImpl / fetch( 之类的属性与方法名
RAW_FETCH_RE = re.compile(r"(?<![A-Za-z0-9_.$])fetch\s*\(")


def test_w15_shared_ui_single_implementation():
    """U1/U2:stepper 与金额药丸只有一份实现(/ui.js),买家页与坐席页共用。"""
    assert "global.SCUi" in UI_JS
    for label in ("提交申请", "平台审核", "退款到账", "申请驳回"):
        assert label in UI_JS, f"ui.js 缺少节点文案 {label}"
        assert label not in APP_JS, f"app.js 重复实现了节点文案 {label}"
        assert label not in AGENT_JS, f"agent.js 重复实现了节点文案 {label}"
    assert "window.SCUi" in APP_JS and "window.SCUi" in AGENT_JS
    assert "pricePillCents" in UI_JS


def test_w15_stepper_four_nodes_and_states():
    """U1:四个节点 + 当前高亮/已完成实心/未来空心三态。"""
    for cls in ("step-done", "step-current", "step-future"):
        assert ".stepper-node." + cls in CSS, f"style.css 缺少 .stepper-node.{cls}"
    assert '"step-" + mark' in UI_JS, "ui.js 未按状态拼接节点类名"
    for mark in ('"done"', '"current"', '"future"'):
        assert mark in UI_JS, f"ui.js 缺少节点态 {mark}"
    assert UI_JS.count('"stepper-node"') >= 1
    assert "data-refund-status" in UI_JS
    assert "step-dot" in UI_JS and "step-line" in UI_JS
    assert "step-title" in UI_JS and "step-time" in UI_JS


def test_w15_stepper_timestamp_local_hhmm():
    """U1:节点时间戳取 UTC 转本地 HH:MM(实现只在 ui.js 一处)。

    W15-FIX:后端 RefundOut 的 created_at/decided_at 是 naive UTC,FastAPI 序列化成
    无 Z 后缀 ISO 串;fmtClock 必须补 Z 按 UTC 解析,再转本地显示。
    证法(与时区无关):同一 UTC 瞬间的「无 Z 串」与「带 Z 串」渲染结果必须相同;
    东八区宿主上再锁定精确值(主会话截图 bug:22:28 UTC 被显示成 22:28,应为 06:28)。
    Windows 下 node 可能忽略 TZ 环境变量,故精确断言以探针实测时区偏移 (-480=UTC+8) 门控。
    """
    assert "getHours" in UI_JS and "getMinutes" in UI_JS
    assert '":"' in UI_JS
    assert "function fmtClock" not in APP_JS, "app.js 不应重复实现步骤时间格式化"
    assert "function fmtClock" not in AGENT_JS, "agent.js 不应重复实现步骤时间格式化"
    assert "fmtClock" in UI_JS

    if shutil.which("node") is None:
        import pytest

        pytest.skip("未找到 node 可执行文件,跳过 naive UTC 行为断言(node 缺失 skip 而非 fail)")

    observed = _run_w15_probe()
    assert observed.get("ok"), f"探针执行失败: {observed.get('error')}"
    clocks = observed["clocks"]

    # 1) 无时标串与显式 UTC 串渲染一致 = 按 UTC 解析(不依赖宿主时区)
    assert clocks["naive_utc"] == clocks["zulu"], "无 Z 后缀的 naive UTC 串被按本地时间解析"
    assert clocks["naive_ms"] == clocks["zulu_ms"], "naive UTC 22:28 未按 UTC 解析"
    assert clocks["naive_frac"] == clocks["zulu"], "带微秒的 naive UTC 串未按 UTC 解析"

    # 2) 带时标串维持原行为(不重复补 Z)
    assert clocks["offset_plus8"] == clocks["zulu"]

    # 3) 非法/空值仍返回空串
    assert clocks["invalid"] == "" and clocks["empty"] == ""

    # 4) 东八区宿主:锁定精确本地时刻 + stepper 首节点文案
    if observed.get("tz_offset_min") == -480:
        assert clocks["naive_utc"] == "08:00", "2026-01-01T00:00:00(naive UTC)应为本地 08:00"
        assert clocks["naive_ms"] == "06:28", "2026-11-11T22:28:00(naive UTC)应为本地 06:28"
        first_node = observed["snapshots"]["naive_stepper"]["children"][0]
        times = [
            kid["text"]
            for kid in first_node["children"][1]["children"]
            if kid["class"] == "step-time"
        ]
        assert times == ["06:28"], f"stepper 首节点时间应为本地 06:28,实际 {times}"


def test_w15_stepper_skipped_without_refund():
    """U1:没有退款关联时不渲染进度条(工单 B2 场景)。"""
    assert "if (!status) return null;" in UI_JS
    assert "receipt.refund_request_id" in APP_JS
    assert "if (!rid) return;" in AGENT_JS
    assert "if (!rf || !rf.status) return;" in AGENT_JS


def test_w15_price_pill_structure():
    """U2:金额药丸 = ¥ + 大号数字,主色底白字,圆角沿用令牌。"""
    assert "price-pill" in UI_JS and "price-cur" in UI_JS and "price-num" in UI_JS
    assert ".price-pill" in CSS and ".price-cur" in CSS and ".price-num" in CSS
    assert "background: var(--primary);" in CSS
    assert "border-radius: var(--radius);" in CSS
    assert "price-row" in APP_JS and "price-label" in APP_JS
    assert "pricePill(o.total_cents)" in APP_JS, "订单卡金额未使用药丸"
    assert "pricePill(cached)" in AGENT_JS, "工单详情金额未使用药丸"


def test_w15_token_values_unchanged():
    """U2:既有 12 个令牌值逐字未改;W15 段落只用令牌,不再造裸色值。"""
    origin = {
        "--bg": "#FAF6F0",
        "--card": "#FFFFFF",
        "--primary": "#C05621",
        "--accent": "#9C4221",
        "--success": "#2F855A",
        "--warn": "#B7791F",
        "--danger": "#C53030",
        "--ink": "#2D2A26",
        "--muted": "#6B6560",
    }
    for name, value in origin.items():
        assert re.search(re.escape(name) + r"\s*:\s*" + re.escape(value) + r"\s*;", CSS), \
            f"基准令牌 {name} 被改动"
    tail = CSS.split("W15 前端精修")[1]
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", tail), "W15 段落出现了裸 hex 色值"
    assert "rgba(" not in tail, "W15 段落出现了裸 rgba 色值"
    for name in ("--primary-soft", "--warn-soft", "--muted-soft", "--info", "--info-soft",
                 "--warn-ink", "--muted-line", "--muted-rule", "--primary-line"):
        assert name in CSS, f"缺少令牌变体 {name}"


def test_w15_queue_corner_badge():
    """U3:队列卡右上角四状态彩色角标,颜色全部走令牌。"""
    for cls in ("corner-queued", "corner-claimed", "corner-waiting_customer", "corner-closed"):
        assert cls in AGENT_JS, f"agent.js 缺少角标类 {cls}"
        assert "." + cls in CSS, f"style.css 缺少 .{cls}"
    assert "ticket-corner" in AGENT_JS and ".ticket-corner" in CSS
    assert ".corner-queued { background: var(--warn-soft); color: var(--warn-ink); }" in CSS
    assert ".corner-claimed { background: var(--primary); color: var(--card); }" in CSS
    assert ".corner-waiting_customer { background: var(--info-soft); color: var(--info); }" in CSS
    assert ".corner-closed { background: var(--muted-soft); color: var(--muted); }" in CSS


def test_w15_queue_card_remains_button():
    """U3:角标只是叠加在卡片上,整卡仍是 button(键盘可达性不退化)。"""
    assert 'el("button", "ticket-item"' in AGENT_JS
    assert 'item.type = "button";' in AGENT_JS
    queue_block = AGENT_JS.split("function renderQueue()")[1][:2600]
    assert "cornerBadge(ticket)" in queue_block
    assert "row.appendChild(badge(ticket))" not in queue_block, "队列行仍放纯文字徽章"
    assert "badge(ticket)" in AGENT_JS, "详情头部徽章不应被移除"


def test_w15_quick_chip_icons():
    """U4:快捷词各带一枚 16px 线性 SVG 图标,W14 的胶囊契约不变(W16 起第四枚为「查天气」)。"""
    chips = re.findall(r'class="example-chip"[^>]*data-text="([^"]+)"', CHAT_HTML)
    assert len(chips) == 4
    assert CHAT_HTML.count('<svg class="chip-icon"') == 4
    for chip in re.findall(r'<button[^>]*class="example-chip"[\s\S]*?</button>', CHAT_HTML):
        assert '<svg class="chip-icon"' in chip
        assert 'stroke="currentColor"' in chip
    assert ".chip-icon" in CSS
    assert "width: 16px;" in CSS and "height: 16px;" in CSS


def test_w15_no_raw_fetch_in_pages():
    """U5:底层网络调用收拢到 /api.js,其余页面零裸 fetch。"""
    for name, src in (
        ("app.js", APP_JS),
        ("agent.js", AGENT_JS),
        ("login.html", LOGIN_HTML),
        ("chat.html", CHAT_HTML),
        ("agent.html", AGENT_HTML),
        ("ui.js", UI_JS),
    ):
        assert not RAW_FETCH_RE.search(src), f"{name} 仍有裸 fetch 调用"
        assert "fetchImpl" not in src, f"{name} 绕过了统一封装"
    assert "global.fetch" in API_JS, "api.js 必须持有唯一的底层网络调用"


def test_w15_api_fetch_timeout_and_envelope():
    """U5:统一封装带超时兜底、统一错误信封、写接口防重放头。"""
    assert "createApiFetch" in API_JS
    assert "AbortController" in API_JS and "timeoutMs" in API_JS
    assert "X-Timestamp" in API_JS and "X-Nonce" in API_JS
    assert "resp.status === 401" in API_JS and "onUnauthorized" in API_JS
    for key in ("ok:", "status:", "data:", "error:", "resp:"):
        assert key in API_JS, f"信封缺少字段 {key}"
    assert "createApiFetch" in APP_JS
    assert "createApiFetch" in AGENT_JS
    assert "createApiFetch" in LOGIN_HTML
    assert '<script src="/api.js"></script>' in LOGIN_HTML
    assert '<script src="/api.js"></script>' in CHAT_HTML
    assert '<script src="/api.js"></script>' in AGENT_HTML
    assert '<script src="/ui.js"></script>' in CHAT_HTML
    assert '<script src="/ui.js"></script>' in AGENT_HTML


def test_w15_summary_brief_view_has_no_transcript():
    """U6:轮询用的 brief 视图只回状态与计数,不含消息正文。"""
    assert '_SUMMARY_VIEWS = ("full", "brief", "messages")' in HANDOFF_PY
    assert 'if view == "brief":' in HANDOFF_PY
    brief_block = HANDOFF_PY.split('if view == "brief":')[1].split('if view == "messages":')[0]
    assert "ticket_messages" not in brief_block, "brief 视图仍在回工单消息正文"
    assert '"messages"' not in brief_block, "brief 视图仍在回会话消息正文"
    assert "message_count" in brief_block and "last_msg_at_utc" in brief_block
    assert "view=brief" in APP_JS and "view=messages" in APP_JS
    assert "&after=" in APP_JS


def test_w15_aux_degraded_shells():
    """U7:三处辅助调用包 try-degrade,异常记 aux_degraded 事件后走原主链路。"""
    assert 'EVENT_AUX_DEGRADED = "aux_degraded"' in TRACE_PY
    assert "def record_aux_degraded(" in TRACE_PY
    for src, source in (
        (CHAT_PY, "semantic_cache.lookup"),
        (AGENT_PY, "agent.suggest_citations"),
        (SLA_PY, "sla.scan_breaches"),
    ):
        assert "record_aux_degraded" in src, "缺少降级事件记录"
        assert source in src, f"缺少降级来源标记 {source}"
    assert "semantic_cache.store" in CHAT_PY


# ---------------------------------------------------------------------------
# W15 DOM 快照(Node + 假 DOM 探针,产出 .tmp/w15/*.json)
# ---------------------------------------------------------------------------

_W15_PROBE = r"""
"use strict";
var fs = require("fs");
var path = require("path");
var FRONTEND = process.argv[2];

function makeNode(tag) {
  var node = {
    tagName: String(tag).toUpperCase(),
    childNodes: [],
    attributes: {},
    _class: "",
    _text: "",
    parentNode: null,
    style: {},
    files: []
  };
  Object.defineProperty(node, "className", {
    get: function () { return node._class; },
    set: function (v) { node._class = (v === null || v === undefined) ? "" : String(v); }
  });
  Object.defineProperty(node, "textContent", {
    get: function () {
      if (!node.childNodes.length) return node._text;
      return node.childNodes.map(function (c) { return c.textContent; }).join("");
    },
    set: function (v) {
      node._text = (v === null || v === undefined) ? "" : String(v);
      node.childNodes = [];
    }
  });
  node.classList = {
    add: function () {
      var set = node._class.split(/\s+/).filter(Boolean);
      Array.prototype.slice.call(arguments).forEach(function (c) {
        if (set.indexOf(c) < 0) set.push(c);
      });
      node._class = set.join(" ");
    },
    remove: function () {
      var set = node._class.split(/\s+/).filter(Boolean);
      Array.prototype.slice.call(arguments).forEach(function (c) {
        var i = set.indexOf(c);
        if (i >= 0) set.splice(i, 1);
      });
      node._class = set.join(" ");
    },
    contains: function (c) { return node._class.split(/\s+/).indexOf(c) >= 0; },
    toggle: function () {}
  };
  node.appendChild = function (c) { c.parentNode = node; node.childNodes.push(c); return c; };
  node.removeChild = function (c) {
    node.childNodes = node.childNodes.filter(function (x) { return x !== c; });
    c.parentNode = null;
    return c;
  };
  node.setAttribute = function (k, v) { node.attributes[k] = String(v); };
  node.getAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : null;
  };
  node.removeAttribute = function (k) { delete node.attributes[k]; };
  node.addEventListener = function () {};
  node.removeEventListener = function () {};
  node.dispatchEvent = function () { return true; };
  node.querySelector = function () { return null; };
  node.querySelectorAll = function () { return []; };
  node.closest = function () { return null; };
  node.matches = function () { return false; };
  node.focus = function () {};
  node.blur = function () {};
  node.click = function () {};
  node.scrollIntoView = function () {};
  node.getBoundingClientRect = function () {
    return { top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0 };
  };
  return node;
}

var byId = {};
global.document = {
  getElementById: function (id) { if (!byId[id]) byId[id] = makeNode("div"); return byId[id]; },
  createElement: function (tag) { return makeNode(tag); },
  createTextNode: function (text) { var n = makeNode("#text"); n._text = String(text); return n; },
  createDocumentFragment: function () { return makeNode("#fragment"); },
  addEventListener: function () {},
  removeEventListener: function () {},
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  body: makeNode("body"),
  documentElement: makeNode("html"),
  head: makeNode("head")
};

var store = {
  _d: {},
  getItem: function (k) {
    return Object.prototype.hasOwnProperty.call(store._d, k) ? store._d[k] : null;
  },
  setItem: function (k, v) { store._d[k] = String(v); },
  removeItem: function (k) { delete store._d[k]; },
  clear: function () { store._d = {}; }
};
store.setItem("sc_agent_token", "probe-token");
store.setItem("sc_agent_user", JSON.stringify({
  role: "agent", id: "agent_probe", merchant_id: "m_probe",
  service_id: "s_probe", display_name: "探针坐席"
}));
store.setItem("sc_buyer_token", "probe-token");
store.setItem("sc_buyer_user", JSON.stringify({
  role: "buyer", id: "buyer_probe", customer_id: "c_probe"
}));

global.window = global;
global.localStorage = store;
global.location = {
  pathname: "/agent.html", protocol: "http:", host: "127.0.0.1", search: "",
  href: "http://127.0.0.1/agent.html",
  replace: function () {}, reload: function () {}, assign: function () {}
};
try {
  Object.defineProperty(global, "navigator", {
    value: { userAgent: "node-probe" }, configurable: true, writable: true
  });
} catch (err) { /* node 已内置只读 navigator,忽略 */ }
global.addEventListener = function () {};
global.removeEventListener = function () {};
global.innerHeight = 900;
global.innerWidth = 1440;
global.scrollTo = function () {};
global.matchMedia = function () {
  return { matches: false, addEventListener: function () {}, removeEventListener: function () {} };
};
global.setInterval = function () { return 0; };
global.clearInterval = function () {};
global.setTimeout = function () { return 0; };
global.clearTimeout = function () {};
global.requestAnimationFrame = function () { return 0; };
global.fetch = function () {
  var payload = JSON.stringify({ ok: true, items: [], user: null });
  return Promise.resolve({
    ok: true, status: 200,
    text: function () { return Promise.resolve(payload); },
    json: function () { return Promise.resolve(JSON.parse(payload)); },
    headers: { get: function () { return null; } }
  });
};
global.WebSocket = function () {
  this.readyState = 0;
  this.close = function () {};
  this.send = function () {};
  this.addEventListener = function () {};
};
global.__DSH_TEST_HOOKS__ = {};

function load(file) {
  var src = fs.readFileSync(path.join(FRONTEND, file), "utf8");
  (0, eval)(src);
}

function ser(node, depth) {
  if (!node || depth > 8) return null;
  var out = {
    tag: node.tagName,
    class: node.className || "",
    text: (node.textContent || "").trim(),
    attrs: node.attributes || {}
  };
  var kids = [];
  (node.childNodes || []).forEach(function (c) {
    var s = ser(c, depth + 1);
    if (s) kids.push(s);
  });
  out.children = kids;
  return out;
}

load("api.js");
load("ui.js");
load("app.js");
load("agent.js");

var hooks = global.__DSH_TEST_HOOKS__;
var out = { ok: false, snapshots: {} };

try {
  out.snapshots.buyer_stepper = ser(hooks.app.renderRefundStepper(
    "审核", "2026-11-10T02:03:00Z", null), 0);
  out.snapshots.workbench_stepper = ser(hooks.agent.renderRefundStepper(
    "退款", "2026-11-10T02:03:00Z", "2026-11-11T09:41:00Z"), 0);
  out.snapshots.corners = {};
  ["queued", "claimed", "waiting_customer", "closed"].forEach(function (st) {
    out.snapshots.corners[st] = ser(hooks.agent.cornerBadge({ status: st }), 0);
  });
  out.snapshots.order_pill = ser(hooks.app.pricePill(13390), 0);
  out.snapshots.agent_price_pill = ser(hooks.agent.pricePill(8212), 0);
  out.clocks = {
    naive_utc: global.SCUi.fmtClock("2026-01-01T00:00:00"),
    zulu: global.SCUi.fmtClock("2026-01-01T00:00:00Z"),
    naive_ms: global.SCUi.fmtClock("2026-11-11T22:28:00"),
    zulu_ms: global.SCUi.fmtClock("2026-11-11T22:28:00Z"),
    naive_frac: global.SCUi.fmtClock("2026-01-01T00:00:00.123456"),
    offset_plus8: global.SCUi.fmtClock("2026-01-01T08:00:00+08:00"),
    invalid: global.SCUi.fmtClock("not-a-time"),
    empty: global.SCUi.fmtClock("")
  };
  out.tz_offset_min = new Date("2026-01-01T00:00:00Z").getTimezoneOffset();
  out.snapshots.naive_stepper = ser(global.SCUi.renderRefundStepper(
    "审核", "2026-11-11T22:28:00", null), 0);
  out.ok = true;
} catch (err) {
  out.error = String(err && err.stack ? err.stack : err);
}

process.stdout.write("__DSH_JSON__" + JSON.stringify(out));
process.exit(0);
"""


def _run_w15_probe() -> dict:
    """跑 Node 假 DOM 探针,返回观察结果(标准输出落文件,避开 Windows 匿名管道限制)。"""
    node = shutil.which("node")
    if node is None:
        return {}
    work_dir = ROOT / ".tmp" / f"w15_probe_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "probe.js"
    stdout_path = work_dir / "probe.stdout.txt"
    stderr_path = work_dir / "probe.stderr.txt"
    script.write_text(_W15_PROBE, encoding="utf-8")
    # 东八区固定,便于锁定 08:00/06:28;Windows 下 node 可能忽略 TZ,断言按实测偏移门控
    env = dict(os.environ)
    env["TZ"] = "Asia/Shanghai"
    try:
        with stdout_path.open("wb") as out_file, stderr_path.open("wb") as err_file:
            proc = subprocess.run(
                [node, str(script), str(FRONTEND)],
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=err_file,
                timeout=120,
                cwd=str(ROOT),
                env=env,
            )
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        assert proc.returncode == 0, f"探针退出码 {proc.returncode}: {stderr[-500:]}"
        assert "__DSH_JSON__" in stdout, f"探针无输出: {stderr[-500:]}"
        return json.loads(stdout.split("__DSH_JSON__", 1)[1])
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_w15_dom_snapshots_export():
    """W15 DOM 快照:买家/坐席两套 stepper、队列角标、金额药丸(≥4 份,落 .tmp/w15/)。"""
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("未找到 node 可执行文件,跳过 DOM 快照(node 不可用时 skip 而非 fail)")

    observed = _run_w15_probe()
    assert observed.get("ok"), f"探针执行失败: {observed.get('error')}"
    snaps = observed["snapshots"]

    buyer = snaps["buyer_stepper"]
    bench = snaps["workbench_stepper"]
    buyer_nodes = buyer["children"]
    bench_nodes = bench["children"]
    assert len(buyer_nodes) == 4 and len(bench_nodes) == 4
    assert buyer_nodes[0]["class"].endswith("step-done")
    assert buyer_nodes[1]["class"].endswith("step-current")
    assert buyer_nodes[2]["class"].endswith("step-future")
    assert bench_nodes[2]["class"].endswith("step-current")
    assert bench_nodes[3]["class"].endswith("step-future")
    clock = re.compile(r"^\d{2}:\d{2}$")
    times = [
        node_text
        for item in bench_nodes
        for node_text in [
            kid["text"] for kid in item["children"][1]["children"] if kid["class"] == "step-time"
        ]
    ]
    assert times and all(clock.match(t) for t in times), f"时间戳不是本地 HH:MM: {times}"

    corners = snaps["corners"]
    assert corners["queued"]["class"] == "ticket-corner corner-queued"
    assert corners["queued"]["text"] == "待领取"
    assert corners["claimed"]["class"] == "ticket-corner corner-claimed"
    assert corners["waiting_customer"]["class"] == "ticket-corner corner-waiting_customer"
    assert corners["closed"]["class"] == "ticket-corner corner-closed"

    assert snaps["order_pill"]["text"] == "¥133.90"
    assert snaps["agent_price_pill"]["text"] == "¥82.12"

    out_dir = ROOT / ".tmp" / "w15"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "buyer_refund_stepper.json": buyer,
        "workbench_refund_stepper.json": bench,
        "queue_corner_badges.json": corners,
        "order_card_price_pill.json": snaps["order_pill"],
    }
    for name, payload in files.items():
        (out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    assert len(files) >= 4
    for name in files:
        assert (out_dir / name).exists()


def test_citation_card_shows_title_not_internal_code():
    """C1(2026-11-17 审计):引用卡可见文本不得出现内部条款代号。

    W13 ISS-21 要求 RP-xx/LP-xx 不出现在买家可见文本里。后端 sanitize 覆盖了
    draft 与 SSE delta,但 citations 是另一条路径:app.js 原先把 cite.clause_id
    渲染成悬浮提示「查看条款 RP-02」与卡元信息「条款 RP-02 v1」。
    改为显示 cite.title;data-clause 属性保留供内部定位(非可见文本)。
    """
    assert '"查看条款 " + (cite.title' in APP_JS, "悬浮提示应改用条款标题"
    assert '"条款 " + (cite.title' in APP_JS, "引用卡元信息应改用条款标题"
    for line in APP_JS.splitlines():
        assert not ("条款 " in line and "clause_id" in line), (
            f"可见文本仍在拼内部代号: {line.strip()}"
        )
