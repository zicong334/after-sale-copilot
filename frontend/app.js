/* 安心退 · 买家聊天页(原生 JS,零第三方依赖 零构建)
 * SSE 协议沿用 /api/c/chat/stream;W5 新增:
 * - append_ack:转人工后的留言同步微标(不是系统气泡)
 * - handoff_notice:全流程只弹一次 3.5s toast
 * - handed_off 后每 3s 轮询会话 summary,把 author_type=agent 渲染成「人工」绿气泡
 * - W5-FIX R1:会话状态**只**由本 session 的工单驱动(`/api/c/handoff/summary?session_id=`),
 *   不再按客户取最近工单;handed_off 且工单未关闭时输入框保持可用(留言追加到工单),
 *   只有本 session 的工单 closed 才禁用输入 + 提示「工单已关闭」
 * W14 前端体验包:对客文案全部业务化——短单号、中文状态、¥金额、统一时间;
 * trace/token 用量/英文枚举只留控制台,不再出现在页面上。
 */
(function () {
  "use strict";

  /* W13 ISS-45/19:买家独立凭证槽,与客服页(sc_agent_*)互不影响。 */
  var TOKEN_KEY = "sc_buyer_token";
  var USER_KEY = "sc_buyer_user";
  var POLL_MS = 3000;
  var TOAST_MS = 3500;
  var WS_RETRY_STEPS = [1000, 2000, 5000]; // 断线退避:1s → 2s → 5s(之后保持 5s)
  var WS_PING_MS = 25000;

  /* W10:测试钩子模式(Node + 假 DOM 断言用)。
   * 注入 window.__DSH_TEST_HOOKS__ 即视为测试模式:跳过登录守卫与 /auth/me 自检,
   * 保证 DOM 残缺(假 DOM)时也能注册钩子。正常浏览器没有该变量,行为完全不变。 */
  var TEST_MODE = !!(typeof window !== "undefined" && window.__DSH_TEST_HOOKS__);

  /* ------------------------- W8 登录态 / 统一请求封装 ------------------------ */

  function readToken() {
    try { return window.localStorage.getItem(TOKEN_KEY) || ""; } catch (err) { return ""; }
  }

  function readUser() {
    try {
      var raw = window.localStorage.getItem(USER_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (err) {
      return null;
    }
  }

  function gotoLogin() {
    try {
      window.localStorage.removeItem(TOKEN_KEY);
      window.localStorage.removeItem(USER_KEY);
    } catch (err) { /* 隐私模式忽略 */ }
    if (location.pathname.indexOf("login.html") < 0) location.replace("/login.html");
  }

  /* W15 U5:底层网络调用收拢到 /api.js(全站唯一持有请求实现的文件)。
   * 本页只做两件事:懒加载共享封装 + 注入本页凭证槽与 401 兜底。
   * 共享封装统一提供:超时兜底 / 统一错误信封 { ok, status, data, error, resp }
   * / 写接口防重放头(X-Timestamp + X-Nonce)。 */
  var _apiFetch = null;

  function apiFetch(path, opts) {
    if (!_apiFetch) {
      var SC = (typeof window !== "undefined") ? window.SCApi : null;
      if (!SC || typeof SC.createApiFetch !== "function") {
        return Promise.reject(new Error("请求封装未加载(/api.js)"));
      }
      _apiFetch = SC.createApiFetch({ tokenKey: TOKEN_KEY, onUnauthorized: gotoLogin });
    }
    return _apiFetch(path, opts);
  }

  /* 会话 id 持久化:WS 与续聊都依赖它(同一买家账号下复用) */
  function saveSession() {
    if (!state.sessionId) return;
    try { window.localStorage.setItem(SESSION_KEY, state.sessionId); } catch (err) { /* 隐私模式忽略 */ }
  }

  function loadSession() {
    try { return window.localStorage.getItem(SESSION_KEY) || ""; } catch (err) { return ""; }
  }

  /* 身份一律来自 token(不再读 URL ?merchant_id=&customer_id=) */
  var ME = readUser();
  var TOKEN = readToken();
  if ((!TOKEN || !ME || ME.role !== "buyer") && !TEST_MODE) {
    gotoLogin();
    return;
  }
  // 测试模式下可能没有登录态,补一个最小身份,避免后续取字段报错
  if (!ME) ME = { id: "", username: "", role: "buyer", merchant_id: "", customer_id: "" };
  var MERCHANT_ID = ME.merchant_id || "";
  var CUSTOMER_ID = ME.customer_id || "";
  // 会话 id 按买家分开存:同一浏览器切换买家账号不会串会话
  var SESSION_KEY = "sc_session_" + (CUSTOMER_ID || ME.id || ME.username || "buyer");

  /* W8 收尾:启动时回读 /auth/me 校验登录态(token 可能已过期/被吊销)。
   * - 401 → apiFetch 统一 gotoLogin() 清登录态并回 login.html
   * - 服务端身份与本地缓存不一致 → 覆盖 localStorage 后 reload,重新派生 merchant/customer/session
   * - 网络异常 → 沿用本地缓存身份,后续业务请求的 401 仍会兜底跳转 */
  (async function verifySession() {
    if (!TOKEN || TEST_MODE) return;
    try {
      var out = await apiFetch("/auth/me");
      if (!out.ok) { if (out.status === 401) gotoLogin(); return; }
      var data = out.data;
      var user = data && data.user;
      if (!user) return;
      if (user.id !== ME.id || user.role !== ME.role) {
        var saved = false;
        try { window.localStorage.setItem(USER_KEY, JSON.stringify(user)); saved = true; } catch (err) { /* 隐私模式忽略 */ }
        /* W13 ISS-69:仅角色变化且写入成功才刷新,避免每次进页都无条件 reload。 */
        if (saved && user.role !== ME.role) location.reload();
      }
    } catch (err) { /* 网络异常:保留缓存身份 */ }
  })();

  var els = {
    stream: document.getElementById("stream"),
    empty: document.getElementById("emptyState"),
    examples: document.getElementById("examples"),
    form: document.getElementById("composer"),
    input: document.getElementById("text"),
    send: document.getElementById("send"),
    status: document.getElementById("statusBadge"),
    toastWrap: document.getElementById("toastWrap"),
    orderStrip: document.getElementById("orderStrip"),
    orderChips: document.getElementById("orderChips"),
    closedHint: document.getElementById("closedHint"),
    handoffBar: document.getElementById("handoffBar"),
    connBadge: document.getElementById("connBadge"),
    logoutBtn: document.getElementById("logoutBtn")
  };

  var state = {
    sessionId: null,
    busy: false,
    handedOff: false,
    pendingHandoff: false, // W11 B8:已挂转人工确认卡、尚未确认
    closed: false,
    ticketId: null,
    ticketStatus: null,
    handoffToastShown: false,
    handoffLineShown: false,
    closedNotified: false,
    pollTimer: null,
    pollFails: 0,
    seenHuman: Object.create(null),
    ticketMsgCount: null, // W15 U6:已见工单消息条数(变化才拉消息正文)
    ticketLastMsgAt: "", // W15 U6:增量拉取游标(最后一条消息时间)
    lastText: "",
    lastUserBubble: null, // 最近一条买家气泡,用于挂「已同步至坐席」微标
    confirmCards: Object.create(null), // confirm_token → { okBtn, noBtn, countdown },供 WS 同步
    ws: null,
    wsReady: false,
    wsRetry: 0,
    wsSession: null, // 当前 WS 订阅的 session_id,会话切换时需重连
    wsReconnectTimer: null,
    wsPingTimer: null,
    wsClosedByUs: false
  };

  /* -------------------------------- 基础工具 -------------------------------- */

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function nowTime() {
    var d = new Date();
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  /* W14 ISS-56:全站统一时间格式(月-日 时:分),不再出现 UTC 原文/多种混排。 */
  function fmtTime(iso) {
    if (!iso) return nowTime();
    var d = new Date(iso);
    if (isNaN(d.getTime())) return nowTime();
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function scrollBottom() {
    els.stream.scrollTop = els.stream.scrollHeight;
  }

  /* W14 ISS-36:仅当用户本来就贴着底部时才自动滚底,
   * 生成过程中上翻阅读历史不会被强行拽回。 */
  function nearBottom() {
    var el = els.stream;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - (el.clientHeight || 0) < 120;
  }

  /* W14 ISS-07/56:业务单号只展示后 6 位短码,不把 36 位 UUID 怼到买家脸上。 */
  function shortCode(id) {
    var raw = String(id || "").replace(/-/g, "");
    if (!raw) return "—";
    return "#" + raw.slice(-6).toUpperCase();
  }

  /* 对客文案净化:后端字段里的工具名/英文枚举不直接透传给买家。 */
  function sanitizeCopy(text) {
    return String(text == null ? "" : text)
      .replace(/policy_search/g, "线上自助查询")
      .replace(/\bverified\b/g, "已核验")
      .replace(/\bqueued\b/g, "排队中")
      .replace(/\bactive\b/g, "生效")
      .replace(/\bretired\b/g, "已停用");
  }

  var RECEIPT_STATUS_TEXT = {
    queued: "排队中",
    pending: "处理中",
    claimed: "跟进中",
    confirmed: "已确认",
    approved: "已通过",
    active: "处理中",
    rejected: "已拒绝",
    refunded: "已退款",
    failed: "处理失败",
    expired: "已超时",
    unknown: "待确认"
  };

  function receiptStatusText(status) {
    return RECEIPT_STATUS_TEXT[status] || "已受理";
  }

  function toast(message, kind) {
    var node = el("div", "toast" + (kind ? " " + kind : ""), message);
    node.setAttribute("role", "status");
    els.toastWrap.appendChild(node);
    window.setTimeout(function () {
      node.classList.add("leaving");
      window.setTimeout(function () {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 280);
    }, TOAST_MS);
  }

  function renderStatus() {
    if (state.closed) {
      els.status.setAttribute("data-state", "closed");
      els.status.textContent = "工单已关闭";
    } else if (state.handedOff) {
      els.status.setAttribute("data-state", "handoff");
      els.status.textContent = "已转人工";
    } else if (state.pendingHandoff) {
      /* W11 B8:确认卡还没点,不能提前显示"已转人工"。 */
      els.status.setAttribute("data-state", "pending-handoff");
      els.status.textContent = "待确认转人工";
    } else if (state.busy) {
      els.status.setAttribute("data-state", "active");
      els.status.textContent = "AI 回复中…";
    } else {
      els.status.setAttribute("data-state", "active");
      els.status.textContent = "AI 接待中";
    }
  }

  /* W14 ISS-02/23:trace 只写浏览器控制台供排查,页面 DOM 不再出现 trace 标记。 */
  function setTrace(traceId) {
    if (traceId && window.console && console.debug) console.debug("[trace]", traceId);
  }

  function hideEmpty() {
    if (els.empty) els.empty.classList.add("hidden");
  }

  /* -------------------------------- 消息渲染 -------------------------------- */

  /* W14 ISS-34:回复正文的轻量格式化。
   * 只处理三类高频内容:换行分段、**加粗**、①②③ 编号条目;
   * 全部用 DOM 文本节点拼装,绝不用 innerHTML(避免 XSS)。
   * 纯文本(无标记/无换行)直接 textContent,保持原有渲染路径与测试稳定。 */
  function renderRichText(bubble, raw) {
    var text = String(raw || "");
    if (!/[*\n①-⑳]/.test(text)) {
      bubble.textContent = text;
      return;
    }
    bubble.textContent = "";
    var lines = text.split(/\n+/);
    lines.forEach(function (line) {
      var clean = line.trim();
      if (!clean) return;
      var isItem = /^(①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩|[-•·]\s|\d+[.、]\s)/.test(clean);
      var row = el("div", "bubble-line" + (isItem ? " bubble-item" : ""));
      var parts = clean.split(/(\*\*[^*]+\*\*)/g);
      parts.forEach(function (part) {
        if (!part) return;
        if (part.indexOf("**") === 0 && part.lastIndexOf("**") === part.length - 2) {
          row.appendChild(el("strong", "", part.slice(2, -2)));
        } else {
          row.appendChild(document.createTextNode(part));
        }
      });
      bubble.appendChild(row);
    });
  }

  function addBubble(role, text, opts) {
    opts = opts || {};
    var stick = opts.forceScroll || role === "user" || nearBottom();
    var wrap = el("div", "msg " + role);
    var meta = el("div", "msg-meta");
    var name = opts.roleName ||
      (role === "user" ? "我" : role === "human" ? "人工客服" : "AI 客服");
    if (role === "human") meta.appendChild(el("span", "human-badge", "人工"));
    meta.appendChild(el("span", "role-name", name));
    meta.appendChild(el("span", "msg-time", opts.time || nowTime()));
    var bubble = el("div", "bubble");
    renderRichText(bubble, sanitizeCopy(text));
    wrap.appendChild(meta);
    wrap.appendChild(bubble);
    els.stream.appendChild(wrap);
    if (stick) scrollBottom();
    var handle = { wrap: wrap, bubble: bubble, meta: meta };
    if (role === "user") state.lastUserBubble = handle;
    return handle;
  }

  /* W14 ISS-08/22:转人工后追加留言只留一条气泡底部浅灰微标,不再刷大块黄色系统气泡。 */
  function addSyncNote(anchor, text) {
    if (!anchor) return null;
    var note = el("div", "sync-note", text || "✓ 留言已同步至工单");
    note.setAttribute("role", "status");
    anchor.wrap.appendChild(note);
    if (nearBottom()) scrollBottom();
    return note;
  }

  function addSysLine(text, closed) {
    var line = el("div", "sysline" + (closed ? " closed" : ""));
    line.textContent = text;
    els.stream.appendChild(line);
    scrollBottom();
    return line;
  }

  function showLoading() {
    var row = el("div", "loading-row");
    row.appendChild(el("span", "", "AI 正在思考"));
    var dots = el("span", "typing");
    dots.appendChild(el("i"));
    dots.appendChild(el("i"));
    dots.appendChild(el("i"));
    row.appendChild(dots);
    els.stream.appendChild(row);
    scrollBottom();
    return row;
  }

  function removeNode(node) {
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  function addError(message, retryText) {
    var wrap = el("div", "msg ai error");
    var meta = el("div", "msg-meta");
    meta.appendChild(el("span", "role-name", "系统提示"));
    meta.appendChild(el("span", "msg-time", nowTime()));
    var bubble = el("div", "bubble", "请求失败:" + message);
    wrap.appendChild(meta);
    wrap.appendChild(bubble);
    if (retryText) {
      var retry = el("button", "btn btn-ghost btn-sm", "重试");
      retry.type = "button";
      retry.addEventListener("click", function () {
        removeNode(wrap);
        sendTurn(retryText);
      });
      wrap.appendChild(retry);
    }
    els.stream.appendChild(wrap);
    scrollBottom();
  }

  /* ------------------------------ 引用 [n] 条款卡 ---------------------------- */

  /* W14 ISS-37:每条引用拥有独立的条款卡;点击 [n] 只展开对应一张,
   * 其他卡自动收起,不再把所有条款混排在同一块面板里。 */
  function renderCitations(wrap, citations) {
    if (!citations.length) return;
    var bar = el("div", "citations");
    var cards = [];
    citations.forEach(function (cite, idx) {
      var n = cite.index || idx + 1;
      var ref = el("button", "citation-ref", "[" + n + "]");
      ref.type = "button";
      ref.title = "查看条款 " + (cite.clause_id || "");
      ref.setAttribute("aria-label", "展开第 " + n + " 条政策条款");
      ref.setAttribute("aria-expanded", "false");
      var card = el("div", "clause-card");
      card.setAttribute("data-clause", cite.clause_id || "");
      var body = el("div", "clause-text");
      body.appendChild(el("mark", "", cite.snippet || "(条款原文缺失)"));
      card.appendChild(body);
      var version = cite.version ? " · 版本 " + cite.version : "";
      card.appendChild(el("div", "clause-meta",
        "条款 " + (cite.clause_id || "—") + version));
      ref.addEventListener("click", function () {
        var isSame = ref.classList.contains("active");
        cards.forEach(function (item) {
          item.ref.classList.remove("active");
          item.ref.setAttribute("aria-expanded", "false");
          item.card.classList.remove("open");
        });
        if (isSame) return;
        ref.classList.add("active");
        ref.setAttribute("aria-expanded", "true");
        card.classList.add("open");
        if (nearBottom()) scrollBottom();
      });
      cards.push({ ref: ref, card: card });
      bar.appendChild(ref);
    });
    wrap.appendChild(bar);
    cards.forEach(function (item) { wrap.appendChild(item.card); });
    if (nearBottom()) scrollBottom();
  }

  /* ------------------------------ 确认卡(四要素) --------------------------- */

  /* W14 ISS-07/35/54:确认卡按对客语言渲染,倒计时可被正确销毁/恢复。 */
  function renderConfirmCard(card) {
    var wrap = el("div", "confirm-card");
    var head = el("div", "confirm-head");
    head.appendChild(el("span", "confirm-mark", "!"));
    head.appendChild(el("span", "", "需要您确认后为您办理"));
    wrap.appendChild(head);
    wrap.appendChild(el("div", "confirm-sub",
      "确认后立即为您办理；超时未确认将自动取消，不会产生扣款或不可逆操作。"));

    var table = el("div", "card-table");
    [
      ["处理事项", card.what],
      ["影响范围", card.impact],
      ["是否可撤销", sanitizeCopy(card.irreversibility)],
      ["其他方案", sanitizeCopy(card.alternatives)]
    ].forEach(function (pair) {
      table.appendChild(el("div", "k", pair[0]));
      table.appendChild(el("div", "v", sanitizeCopy(pair[1]) || "—"));
    });
    wrap.appendChild(table);

    var total = typeof card.expires_in_seconds === "number" && card.expires_in_seconds > 0
      ? card.expires_in_seconds : 600;
    var left = total;
    var progress = el("div", "progress");
    var bar = el("i");
    progress.appendChild(bar);
    wrap.appendChild(progress);

    var actions = el("div", "card-actions");
    var okBtn = el("button", "btn btn-primary", "确认办理");
    var noBtn = el("button", "btn btn-ghost", "暂不办理");
    okBtn.type = "button";
    noBtn.type = "button";
    var countdown = el("div", "countdown", "");
    actions.appendChild(okBtn);
    actions.appendChild(noBtn);
    actions.appendChild(countdown);
    wrap.appendChild(actions);

    els.stream.appendChild(wrap);
    if (nearBottom()) scrollBottom();

    // W8:登记确认卡,收到 WS confirm_status(含其他标签页确认)时同步最终态
    var entry = {
      okBtn: okBtn,
      noBtn: noBtn,
      countdown: countdown,
      timer: null,
      stopped: false // 终态(已提交 / WS 同步)后停掉倒计时,避免覆盖最终文案
    };
    state.confirmCards[card.confirm_token] = entry;

    function stopTick() {
      if (entry.timer) {
        window.clearTimeout(entry.timer);
        entry.timer = null;
      }
    }

    function tick() {
      if (entry.stopped) return;
      if (left <= 0) {
        countdown.textContent = "已超时 · 已自动取消（未执行任何操作）";
        bar.style.width = "0%";
        okBtn.disabled = true;
        noBtn.disabled = true;
        stopTick();
        return;
      }
      var m = Math.floor(left / 60);
      var s = left % 60;
      countdown.textContent = "剩余 " + m + ":" + pad(s) + " · 超时自动取消";
      bar.style.width = Math.max(0, (left / total) * 100).toFixed(2) + "%";
      left -= 1;
      entry.timer = window.setTimeout(tick, 1000);
    }
    tick();

    function decide(kind) {
      /* ISS-35:点确认/拒绝即销毁倒计时,避免它继续覆盖结果文案。 */
      entry.stopped = true;
      stopTick();
      okBtn.disabled = true;
      noBtn.disabled = true;
      countdown.textContent = "正在为您办理…";
      apiFetch("/api/c/confirm/" + encodeURIComponent(card.confirm_token) + "/" + kind, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          thread_id: card.thread_id || state.sessionId
        })
      })
        .then(function (out) {
          countdown.textContent = "";
          if (!out.ok || !out.data) {
            countdown.textContent = "暂时没能办成，请稍后再试或联系人工坐席";
            /* ISS-54:失败不能把卡留在「已停止」的死锁里,恢复按钮与倒计时。 */
            entry.stopped = false;
            okBtn.disabled = false;
            noBtn.disabled = false;
            if (left > 0) tick();
            return;
          }
          stopTick();
          renderDecision(out.data);
        })
        .catch(function (err) {
          countdown.textContent = "网络不稳定，请稍后再试";
          entry.stopped = false;
          okBtn.disabled = false;
          noBtn.disabled = false;
          if (left > 0) tick();
          console.warn("[confirm]", err);
        });
    }

    okBtn.addEventListener("click", function () { decide("confirm"); });
    noBtn.addEventListener("click", function () { decide("reject"); });
  }

  /* W14 ISS-07/56:回执只讲业务结果——短码、金额、中文状态、统一时间;
   * verified/trace 等调试字段留在后端与日志,不再出现在买家页面。 */
  function renderDecision(result) {
    var receipt = result.receipt;
    /* W11 B8:只有确认执行成功并真正建出工单,才算转人工完成。 */
    if (result.decision === "confirmed" && receipt && receipt.ticket_id) {
      state.pendingHandoff = false;
      if (!state.handedOff) markHandedOff(true);
      else renderStatus();
    } else if (result.decision !== "confirmed") {
      state.pendingHandoff = false;
      renderStatus();
    }
    var box = el("div", "receipt");
    if (result.decision === "confirmed") {
      var ok = !receipt || receipt.status !== "unknown";
      box.appendChild(el("span", "badge", ok ? "已办理" : "暂未办理"));
      var id = (receipt && (receipt.refund_request_id || receipt.ticket_id)) || "—";
      box.appendChild(el("span", "row", "业务单号 " + shortCode(id)));
      if (receipt && receipt.amount_cents !== undefined && receipt.amount_cents !== null) {
        /* W15 U2:退款金额改红橙药丸(主色令牌),标签与次要信息降灰。 */
        var amountRow = el("span", "row price-row");
        amountRow.appendChild(el("span", "price-label", "退款金额 "));
        amountRow.appendChild(pricePill(receipt.amount_cents));
        box.appendChild(amountRow);
      }
      if (receipt) {
        box.appendChild(el("span", "row",
          "当前状态 " + receiptStatusText(receipt.status) +
          " · 办理时间 " + fmtTime(receipt.occurred_at_utc)));
      }
    } else if (result.decision === "rejected") {
      box.classList.add("rejected");
      box.appendChild(el("span", "badge", result.timeout ? "已超时自动取消" : "已取消"));
      box.appendChild(el("span", "row", "未执行任何操作，您的订单不受影响。"));
    } else {
      box.classList.add("rejected");
      box.appendChild(el("span", "badge", "暂未办理"));
      box.appendChild(el("span", "row", "这次没能办成，请稍后再试或联系人工坐席。"));
    }
    if (result.reply) box.appendChild(el("span", "row", sanitizeCopy(result.reply)));
    els.stream.appendChild(box);
    if (nearBottom()) scrollBottom();
    /* W15 U1:售后进度 stepper(没有退款单关联时不渲染,避免空进度条)。 */
    if (receipt && receipt.refund_request_id) {
      loadRefundStepper(receipt.refund_request_id);
    }
  }

  /* W15 U1/U2:进度条与金额药丸的实现统一放在 /ui.js(买家页与坐席页共用),
   * 两个视图必须完全一致,不允许各写一份。
   * /ui.js 没加载时降级成本地最小实现,不影响主链路。 */
  function scui() {
    return (typeof window !== "undefined" && window.SCUi) ? window.SCUi : null;
  }

  /* 金额单位换算:后端金额一律用分,展示用元。 */
  function yuanOf(cents) {
    return Number(cents || 0) / 100;
  }

  /* W15 U2:金额药丸(¥ + 大号数字,主色底白字,圆角同令牌)。 */
  function pricePill(cents) {
    var ui = scui();
    if (ui) return ui.pricePill(yuanOf(cents));
    var pill = el("span", "price-pill");
    pill.appendChild(el("span", "price-cur", "¥"));
    pill.appendChild(el("span", "price-num", yuanOf(cents).toFixed(2)));
    return pill;
  }

  /* W15 U1:售后进度四节点(申请 → 审核 → 退款 / 驳回)。
   * 当前节点高亮、已完成实心、未来空心;节点时间戳用本地 HH:MM。
   * 具体实现见 /ui.js(与坐席页共用),这里只做转发。 */
  function renderRefundStepper(status, createdAt, decidedAt) {
    var ui = scui();
    if (!ui) return null;
    return ui.renderRefundStepper(status, createdAt, decidedAt);
  }

  /* W15 U1:拉退款单进度(数据源:现有 GET /refunds/{id},不改后端)。 */
  async function loadRefundStepper(refundId) {
    try {
      var out = await apiFetch("/refunds/" + encodeURIComponent(refundId));
      if (!out.ok || !out.data) return;
      var stepper = renderRefundStepper(
        out.data.status, out.data.created_at, out.data.decided_at);
      if (!stepper) return;
      els.stream.appendChild(stepper);
      if (nearBottom()) scrollBottom();
    } catch (err) {
      /* 进度拉不到不打扰买家,回执本身照常展示 */
    }
  }

  /* W14 ISS-02/03:token 用量/费用/耗时属于调试信息,不再展示给买家;
   * usage 帧仍被收集进 meta(供后端指标与测试断言),只是不渲染 DOM。 */

  /* ---------------------------- 转人工 / 轮询闭环 --------------------------- */

  function markHandedOff(withToast) {
    state.handedOff = true;
    state.pendingHandoff = false;
    renderStatus();
    if (state.wsReady) connBadge("waiting", "等待坐席接入中…");
    applyHandoffUi();
    if (withToast && !state.handoffToastShown) {
      state.handoffToastShown = true;
      toast("已为您转接人工坐席，专属坐席会尽快回复。", "warn");
    }
    /* W14 ISS-08:转人工提示由顶部常驻 handoffBar 承担,不再往对话流里塞系统气泡。 */
    startPolling();
  }

  /**
   * W5-FIX R1:已转人工但**本会话工单仍未关闭**时,输入区保持可用。
   * 买家继续输入 = 追加留言到人工工单(后端 blocked 轮会 append),不是新会话。
   */
  function applyHandoffUi() {
    if (!state.handedOff || state.closed) return;
    if (els.handoffBar) els.handoffBar.classList.remove("hidden");
    els.form.classList.remove("is-closed");
    els.input.disabled = false;
    els.send.disabled = state.busy;
    els.input.placeholder = "留言将追加到人工工单…（Enter 发送，Shift+Enter 换行）";
  }

  function startPolling() {
    // W8:WS 主通道正常时暂停轮询;未就绪/断开时恢复(旧轮询逻辑原样保留为降级)
    if (state.wsReady) return;
    if (state.pollTimer || state.closed) return;
    state.pollFails = 0;
    pollSummary();
    state.pollTimer = window.setInterval(pollSummary, POLL_MS);
  }

  function stopPolling() {
    if (state.pollTimer) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function pollSummary() {
    if (!state.sessionId || state.closed) return;
    try {
      // W5-FIX R1:会话作用域端点——只返回本 session 的工单,不再按客户取最近一张。
      // W15 U6:view=brief → 极简字段(状态 + 消息计数),不再每 3s 回传整段转录。
      var out = await apiFetch(
        "/api/c/handoff/summary?session_id=" + encodeURIComponent(state.sessionId) +
        "&view=brief",
        { headers: { Accept: "application/json" } }
      );
      if (!out.ok) {
        state.pollFails += 1;
        if (state.pollFails === 6) {
          addSysLine("工单状态同步暂时不可用,正在重试…");
        }
        return;
      }
      state.pollFails = 0;
      var data = out.data || {};
      state.ticketId = (data.ticket && data.ticket.ticket_id) || null;
      state.ticketStatus = data.ticket_status || null;
      if (data.session_status === "handed_off" || data.session_status === "closed") {
        markHandedOff(false);
      }
      /* 只有工单消息条数变化时才拉一次消息正文(view=messages + after 游标)。 */
      var count = Number(data.message_count || 0);
      if (count !== state.ticketMsgCount) {
        await pollNewMessages(state.ticketLastMsgAt || "");
        state.ticketMsgCount = count;
        state.ticketLastMsgAt = data.last_msg_at_utc || state.ticketLastMsgAt || "";
      }
      // 只有「本会话的工单」关闭才禁用输入;同客户历史 closed 工单不再参与判断。
      if (data.ticket_closed === true) {
        onTicketClosed();
      } else if (state.handedOff) {
        applyHandoffUi();
      }
    } catch (err) {
      state.pollFails += 1;
    }
  }

  /* W15 U6:增量拉取坐席新消息(只回 after 之后的可见消息,不含内部备注)。 */
  async function pollNewMessages(after) {
    var out = await apiFetch(
      "/api/c/handoff/summary?session_id=" + encodeURIComponent(state.sessionId) +
      "&view=messages&after=" + encodeURIComponent(after || ""),
      { headers: { Accept: "application/json" } }
    );
    if (!out.ok || !out.data) return;
    renderTicketMessages(
      Array.isArray(out.data.ticket_messages) ? out.data.ticket_messages : []);
  }

  /* 渲染坐席消息:按消息 id 去重,内部备注不下发给买家。 */
  function renderTicketMessages(list) {
    list.forEach(function (msg) {
      if (!msg || msg.author_type !== "agent") return;
      // 内部备注只给坐席看,买家页不渲染(后端 summary 也带 is_internal 标记)
      if (msg.is_internal === true) return;
      var key = msg.id ||
        (String(msg.created_at_utc || "") + "|" + String(msg.content || ""));
      if (state.seenHuman[key]) return;
      state.seenHuman[key] = true;
      hideEmpty(); // W14 ISS-51:渲染人工消息前先收起欢迎屏
      addBubble("human", msg.content || "", { time: fmtTime(msg.created_at_utc) });
    });
  }

  function onTicketClosed() {
    if (state.closed) return;
    state.closed = true;
    stopPolling();
    closeWs();
    renderStatus();
    if (els.handoffBar) els.handoffBar.classList.add("hidden");
    els.input.disabled = true;
    els.send.disabled = true;
    els.form.classList.add("is-closed");
    els.closedHint.classList.remove("hidden");
    if (!state.closedNotified) {
      state.closedNotified = true;
      addSysLine("工单已关闭,本次会话已结束。如需新问题,请刷新页面重新发起。", true);
      toast("工单已关闭,输入已停用。", "error");
    }
  }

  /* ------------------------- W8:WS 主通道(实时推送) ------------------------ */
  /*
   * 主通道:GET /ws/buyer/{session_id},连上后首帧 auth(session_id 来自聊天返回/本地保存)。
   * 事件:ready / agent_message / ticket_status / confirm_status / assistant_message;
   * 心跳:客户端发 {"type":"ping"} → 服务端回 pong。
   * 降级:onclose 按 1s/2s/5s 退避重连,重连期间恢复 pollSummary 轮询。
   */

  function connBadge(stateName, text) {
    if (!els.connBadge) return;
    els.connBadge.setAttribute("data-state", stateName);
    els.connBadge.textContent = text;
  }

  function wsUrl() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + "/ws/buyer/" +
      encodeURIComponent(state.sessionId);
    /* W13 ISS-67:token 不拼 URL,连接建立后首帧 auth。 */
  }

  function startWsPing() {
    stopWsPing();
    state.wsPingTimer = window.setInterval(function () {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        try {
          state.ws.send(JSON.stringify({ type: "ping" }));
        } catch (err) { /* 断线由 onclose 统一处理 */ }
      }
    }, WS_PING_MS);
  }

  function stopWsPing() {
    if (state.wsPingTimer) {
      window.clearInterval(state.wsPingTimer);
      state.wsPingTimer = null;
    }
  }

  function closeWs() {
    state.wsClosedByUs = true;
    if (state.wsReconnectTimer) {
      window.clearTimeout(state.wsReconnectTimer);
      state.wsReconnectTimer = null;
    }
    stopWsPing();
    if (state.ws) {
      var ws = state.ws;
      state.ws = null;
      try { ws.close(); } catch (err) { /* 已关闭忽略 */ }
    }
    state.wsReady = false;
  }

  function scheduleWsReconnect() {
    if (state.wsClosedByUs || state.closed || !state.sessionId) return;
    var wait = WS_RETRY_STEPS[Math.min(state.wsRetry, WS_RETRY_STEPS.length - 1)];
    state.wsRetry += 1;
    if (state.wsReconnectTimer) window.clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = window.setTimeout(function () {
      state.wsReconnectTimer = null;
      connectWs();
    }, wait);
    connBadge("offline", "服务暂时离线 · 正在为您重连…");
  }

  function connectWs() {
    // W8-FIX:会话切换时会先 closeWs()(置 wsClosedByUs=true)再 connectWs(),
    // 因此这里必须先复位标志,否则显式重连会被上一行守卫直接挡掉,WS 永不建立。
    if (state.closed) return;
    if (!state.sessionId || !readToken()) return;
    if (state.ws &&
        (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    state.wsClosedByUs = false;
    connBadge("connecting", "正在连接服务…");
    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      scheduleWsReconnect();
      return;
    }
    state.ws = ws;
    state.wsSession = state.sessionId;
    ws.onopen = function () {
      // W13 ISS-67:先发首帧 auth,再等服务端 ready 帧(不把 token 拼进 URL)。
      try { ws.send(JSON.stringify({ type: "auth", token: readToken() })); } catch (err) { /* onclose 统一处理 */ }
    };
    ws.onmessage = function (ev) {
      var msg = null;
      try { msg = JSON.parse(ev.data); } catch (err) { return; }
      if (msg && msg.type) handleWsEvent(msg);
    };
    ws.onerror = function () { /* onclose 统一处理重连 */ };
    ws.onclose = function () {
      if (state.ws === ws) state.ws = null;
      state.wsReady = false;
      stopWsPing();
      if (state.closed || state.wsClosedByUs) {
        connBadge("offline", "服务已离线");
        return;
      }
      if (state.handedOff) startPolling(); // 降级:WS 断开时恢复旧轮询
      scheduleWsReconnect();
    };
  }

  function handleWsEvent(msg) {
    switch (msg.type) {
      case "ready":
        state.wsReady = true;
        state.wsRetry = 0;
        if (state.wsReconnectTimer) {
          window.clearTimeout(state.wsReconnectTimer);
          state.wsReconnectTimer = null;
        }
        /* W14 ISS-25/31:对客只讲业务状态,不暴露「WebSocket 已连接」这类实现细节。 */
        if (state.handedOff) connBadge("waiting", "等待坐席接入中…");
        else connBadge("online", "在线服务中");
        startWsPing();
        pollSummary();  // 先补一次状态,避免 ready 与首帧事件之间漏状态
        stopPolling();  // WS 正常 → 暂停轮询
        break;
      case "pong":
        break;
      case "agent_message":
        onWsAgentMessage(msg);
        break;
      case "ticket_status":
        onWsTicketStatus(msg);
        break;
      case "ticket_update":
        // 队列频道事件:补一次本会话 summary(防御未来重复投递,不打断输入)
        if (state.sessionId) pollSummary();
        break;
      case "confirm_status":
        onWsConfirmStatus(msg);
        break;
      case "assistant_message":
        // 多标签同步:本标签的 AI 回复已由 SSE 渲染,这里不重复追加
        break;
      default:
        break;
    }
  }

  function onWsAgentMessage(msg) {
    if (!msg || !msg.session_id || msg.session_id !== state.sessionId) return;
    var key = msg.message_id ||
      (String(msg.ts || "") + "|" + String(msg.content || ""));
    if (state.seenHuman[key]) return;
    state.seenHuman[key] = true;
    hideEmpty();
    addBubble("human", msg.content || "", { time: fmtTime(msg.ts) });
    markHandedOff(false);
  }

  function onWsTicketStatus(msg) {
    if (!msg || (msg.session_id && msg.session_id !== state.sessionId)) return;
    state.ticketStatus = msg.status || state.ticketStatus;
    if (msg.status === "closed") {
      onTicketClosed();
      return;
    }
    if (msg.status) markHandedOff(false);
  }

  function onWsConfirmStatus(msg) {
    if (!msg || !msg.token) return;
    var card = state.confirmCards[msg.token];
    if (!card) return;
    /* W14 ISS-35:外部已裁决,先销毁本地倒计时,避免 tick 还在跑。 */
    card.stopped = true;
    if (card.timer) {
      window.clearTimeout(card.timer);
      card.timer = null;
    }
    var confirmed = msg.status === "confirmed";
    card.okBtn.disabled = true;
    card.noBtn.disabled = true;
    card.countdown.textContent = confirmed ? "已为您办理" : "已取消办理";
    toast(confirmed ? "已按您的选择办理" : "已取消本次办理", confirmed ? "success" : "warn");
  }

  /* --------------------------------- 输入区 ------------------------------- */

  function autoResize() {
    els.input.style.height = "auto";
    var next = Math.min(els.input.scrollHeight, 168);
    els.input.style.height = Math.max(next, 40) + "px";
  }

  if (els.input) {
    els.input.addEventListener("input", autoResize);

    els.input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        els.form.requestSubmit ? els.form.requestSubmit() : els.form.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    });
  }

  if (els.examples) {
    els.examples.addEventListener("click", function (ev) {
      var chip = ev.target.closest ? ev.target.closest(".example-chip") : null;
      if (!chip) return;
      els.input.value = chip.getAttribute("data-text") || chip.textContent || "";
      autoResize();
      els.input.focus();
    });
  }

  if (els.form) {
    els.form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var text = els.input.value.trim();
      if (!text || state.busy || state.closed) return;
      els.input.value = "";
      autoResize();
      sendTurn(text);
    });
  }

  /* ---------------------------------- SSE -------------------------------- */

  function parseFrame(raw) {
    var event = "message";
    var dataLines = [];
    raw.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) event = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) dataLines.push(line.slice(5).trim());
    });
    if (!dataLines.length) return null;
    try {
      return { event: event, data: JSON.parse(dataLines.join("\n")) };
    } catch (err) {
      return null;
    }
  }

  function handleFrame(frame, ui, meta) {
    var data = frame.data || {};
    switch (frame.event) {
      case "delta":
        // W10 真流式:模型吐字时实时下发,逐字追加渲染到 AI 气泡。
        // 不支持流式的服务端不会发这一帧(0 帧),此时走 message 帧的旧追加路径。
        meta.streamed = true;
        if (ui.ensureAi) {
          var streamAi = ui.ensureAi();
          meta.reply += data.delta || "";
          streamAi.bubble.textContent = meta.reply;
          scrollBottom();
        }
        break;
      case "message":
        if (ui.ensureAi) {
          if (meta.streamed) {
            // 本轮已经收到过 delta 帧:正文已逐字渲染过一遍,
            // message 帧只作最终一致性兜底(记进 meta.messageReply 供 done 校验),
            // **不能**再追加到气泡,否则整段回复会重复显示。
            meta.messageReply += data.delta || "";
          } else {
            var ai = ui.ensureAi();
            meta.reply += data.delta || "";
            ai.bubble.textContent = meta.reply;
            scrollBottom();
          }
        }
        break;
      case "citation":
        meta.citations.push(data);
        break;
      case "confirm_request":
        removeNode(ui.loading);
        ui.loading = null;
        /* W11 B8:转人工确认卡挂起时,顶部徽标显示"待确认转人工"。 */
        state.pendingHandoff = data.tool === "handoff__create_ticket";
        renderStatus();
        /* W14 ISS-68:确认卡永远排在本轮 AI 气泡下方;先确保气泡存在,
         * 避免「确认卡在上、AI 回复在下」的错序。 */
        if (ui.ensureAi) ui.ensureAi();
        renderConfirmCard(data);
        break;
      case "handoff_notice":
        markHandedOff(true);
        break;
      case "append_ack":
        removeNode(ui.loading);
        ui.loading = null;
        meta.acked = true;
        /* W14 ISS-08/22:只留气泡底部浅灰微标,不刷大块黄色系统气泡。 */
        addSyncNote(state.lastUserBubble, "✓ 已同步至坐席");
        if (data.session_status && data.session_status !== "active") markHandedOff(false);
        break;
      case "usage":
        meta.usage = data;
        break;
      case "done":
        meta.done = data;
        // W10:done.reply 是权威最终文案 → 覆盖 AI 气泡(完成后整卡重渲)。
        // meta.reply 同步为最终文案,保证收尾判断用的是权威文本。
        if (typeof data.reply === "string" && data.reply.length) {
          meta.reply = data.reply;
          if (ui.ensureAi) {
            var doneAi = ui.ensureAi();
            renderRichText(doneAi.bubble, sanitizeCopy(data.reply));
            if (nearBottom()) scrollBottom();
          }
        }
        if (data.session_id) {
          state.sessionId = data.session_id;
          saveSession();
          if (!state.ws || state.wsSession !== state.sessionId) {
            closeWs();          // 会话 id 变化:退订旧频道后重连
            connectWs();
          }
        }
        if (data.trace_id) setTrace(data.trace_id);
        if (data.usage) meta.usage = data.usage;
        if (data.session_status && data.session_status !== "active") {
          markHandedOff(false);
        }
        if (data.ack && !meta.acked) {
          meta.acked = true;
          addSyncNote(state.lastUserBubble, "✓ 已同步至坐席");
        }
        /* W14 ISS-68:未回复时不再渲染空壳气泡。 */
        break;
      default:
        break;
    }
  }

  /**
   * 一轮对话的渲染状态(气泡承载 + 引用 + 用量)。
   * 抽成工厂是为了让 sendTurn 与前端测试共用同一份状态结构,避免两边漂移。
   * - reply:当前已渲染进气泡的正文
   * - messageReply:message 帧的累计文本(流式时只作一致性校验,不进气泡)
   * - streamed:本轮是否收到过 delta 帧
   */
  function createTurnMeta() {
    return {
      reply: "",
      messageReply: "",
      citations: [],
      done: null,
      usage: null,
      acked: false,
      streamed: false
    };
  }

  /** 一轮对话的 DOM 句柄:loading 行 + 懒建的 AI 气泡。 */
  function createTurnUi() {
    var ui = { loading: null, ensureAi: null, getAi: null };
    var ai = null;
    ui.ensureAi = function () {
      if (!ai) {
        removeNode(ui.loading);
        ui.loading = null;
        ai = addBubble("ai", "");
      }
      return ai;
    };
    ui.getAi = function () { return ai; };
    return ui;
  }

  async function sendTurn(text) {
    text = String(text || "").trim();
    if (!text || state.busy || state.closed) return;

    state.lastText = text;
    hideEmpty();
    addBubble("user", text);
    state.busy = true;
    renderStatus();
    els.send.disabled = true;
    els.input.focus();

    var ui = createTurnUi();
    ui.loading = showLoading();
    var meta = createTurnMeta();

    var body = {
      merchant_id: MERCHANT_ID,
      customer_id: CUSTOMER_ID,
      text: text
    };
    if (state.sessionId) body.session_id = state.sessionId;

    try {
      var out = await apiFetch("/api/c/chat/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream"
        },
        body: JSON.stringify(body),
        raw: true,        // W15 U5:SSE 走原始响应模式(不解析 JSON)
        timeoutMs: 0     // W15 U5:流式长连接不设超时
      });
      if (!out.ok) {
        var resp = out.resp;
        var detail = out.error ? out.error.message : "请求失败";
        if (resp) {
          try { detail += " " + (await resp.text()).slice(0, 240); } catch (err) { /* 忽略 */ }
        }
        throw new Error("HTTP " + out.status + " " + detail);
      }
      var reader = out.resp.body.getReader();
      var decoder = new TextDecoder("utf-8");
      var buffer = "";
      for (;;) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        var cut;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          var frame = parseFrame(buffer.slice(0, cut));
          buffer = buffer.slice(cut + 2);
          if (frame) handleFrame(frame, ui, meta);
        }
      }
      if (buffer.trim()) {
        var tail = parseFrame(buffer);
        if (tail) handleFrame(tail, ui, meta);
      }
      removeNode(ui.loading);
      ui.loading = null;

      var ai = ui.getAi();
      if (meta.citations.length && ai) renderCitations(ai.wrap, meta.citations);
      if (ai && meta.reply.trim()) {
        renderRichText(ai.bubble, sanitizeCopy(meta.reply));
      } else if (ai) {
        /* 没有正文就不再留一个空气泡(ISS-68)。 */
        removeNode(ai.wrap);
      }
    } catch (err) {
      removeNode(ui.loading);
      ui.loading = null;
      addError(err.message || String(err), text);
    } finally {
      state.busy = false;
      renderStatus();
      // 未关单时恢复可输入(转人工后仍要能继续留言);关单才禁用。
      els.send.disabled = state.closed;
      els.input.disabled = state.closed;
      if (state.handedOff && !state.closed) applyHandoffUi();
      els.input.focus();
    }
  }

  /* --------------------------- 我的订单 / 刷新恢复 --------------------------- */

  var ORDER_STATUS_TEXT = {
    created: "待付款",
    paid: "待发货",
    shipped: "待收货",
    delivered: "已完成",
    refunded: "已退款",
    cancelled: "已取消"
  };

  function orderStatusText(status) {
    return ORDER_STATUS_TEXT[status] || "处理中";
  }

  /* W14 ISS-41:登录后顶部给出「我的订单」快捷入口,一键带单号发起售后。 */
  async function loadMyOrders() {
    if (TEST_MODE || !CUSTOMER_ID || !els.orderStrip || !els.orderChips) return;
    try {
      var out = await apiFetch("/orders");
      if (!out.ok) return;
      var data = out.data;
      var list = Array.isArray(data) ? data : (data && data.items) || [];
      var mine = list.filter(function (o) {
        return o && o.customer_id === CUSTOMER_ID;
      }).slice(0, 3);
      if (!mine.length) {
        els.orderStrip.classList.add("hidden");
        return;
      }
      els.orderChips.textContent = "";
      mine.forEach(function (o) {
        var btn = el("button", "order-chip");
        btn.type = "button";
        /* W15 U2:订单卡金额改红橙药丸,单号与状态降灰。 */
        btn.appendChild(el("span", "order-code", "订单 " + shortCode(o.id)));
        btn.appendChild(pricePill(o.total_cents));
        btn.appendChild(el("span", "order-status", orderStatusText(o.status)));
        btn.addEventListener("click", function () {
          els.input.value = "我想咨询订单 " + shortCode(o.id) + " 的售后问题";
          autoResize();
          els.input.focus();
        });
        els.orderChips.appendChild(btn);
      });
      els.orderStrip.classList.remove("hidden");
    } catch (err) {
      /* 订单列表失败不打扰买家,输入框仍可用 */
    }
  }

  /* W14 ISS-51:刷新页面后若还有未处理的确认卡,恢复出来继续办理。 */
  async function restorePendingConfirm() {
    if (TEST_MODE || !state.sessionId) return;
    try {
      var out = await apiFetch(
        "/api/c/confirm/pending?thread_id=" + encodeURIComponent(state.sessionId));
      if (!out.ok || !out.data) return;
      var data = out.data;
      if (!data || !data.ok || !data.card || !data.confirm_token) return;
      var card = Object.assign({}, data.card, {
        confirm_token: data.confirm_token,
        thread_id: data.thread_id || state.sessionId,
        expires_in_seconds: data.expires_in_seconds,
        tool: data.tool
      });
      /* W11 B8:刷新后仍挂着转人工确认卡 → 保持"待确认转人工"。 */
      state.pendingHandoff = data.tool === "handoff__create_ticket";
      renderStatus();
      hideEmpty();
      renderConfirmCard(card);
    } catch (err) {
      /* 没有待办确认时接口会报错,静默即可 */
    }
  }

  /* --------------------------------- 启动 -------------------------------- */

  function boot() {
    renderStatus();
    autoResize();
    if (els.input) els.input.focus();

    /* W8:退出登录(清 token/用户/本次会话 id 的登录态,回登录页) */
    if (els.logoutBtn) {
      els.logoutBtn.addEventListener("click", function () {
        closeWs();
        try {
          window.localStorage.removeItem(TOKEN_KEY);
          window.localStorage.removeItem(USER_KEY);
        } catch (err) { /* 隐私模式忽略 */ }
        location.replace("/login.html");
      });
    }

    /* W8:恢复本买家上次的会话 id → 先跑旧轮询,再连 WS 主通道(WS 就绪后轮询自动暂停) */
    (function bootSession() {
      var saved = loadSession();
      /* ISS-41:订单快捷卡与会话无关,首次进入也先拉取。 */
      loadMyOrders();
      if (!saved) return;
      state.sessionId = saved;
      startPolling();
      connectWs();
      restorePendingConfirm();
    })();
  }

  try {
    boot();
  } catch (err) {
    // 测试模式(假 DOM 场景)下,启动期异常不许阻断测试钩子注册;
    // 正常浏览器保持原有行为:异常照常抛出。
    if (!TEST_MODE) throw err;
  }

  /* W10:测试钩子(Node + 假 DOM 断言用)。
   * 正常浏览器没有 window.__DSH_TEST_HOOKS__,这段不会执行。 */
  if (typeof window !== "undefined" && window.__DSH_TEST_HOOKS__) {
    window.__DSH_TEST_HOOKS__.app = {
      parseFrame: parseFrame,
      handleFrame: handleFrame,
      addBubble: addBubble,
      renderCitations: renderCitations,
      state: state,
      // W15:进度条与金额药丸的渲染入口(DOM 快照探针直接调用)
      renderRefundStepper: renderRefundStepper,
      pricePill: pricePill,
      // 以下两项是给测试用的工厂:与 sendTurn 共用同一份状态/句柄结构,避免测试与线上漂移
      createTurnMeta: createTurnMeta,
      createTurnUi: createTurnUi
    };
  }
})();
