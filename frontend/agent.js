/* 售后 Copilot · 客服工作台(原生 JS,零第三方依赖 零构建)
 * 对接契约 §二 /api/b/*(全部带 Authorization: Bearer <token>):
 *   GET  /api/b/tickets?merchant_id=&status=
 *   GET  /api/b/tickets/{id}
 *   POST /api/b/tickets/{id}/claim      {service_id}
 *   POST /api/b/tickets/{id}/messages   {service_id, content, is_internal?}
 *   POST /api/b/tickets/{id}/close      {service_id?}
 *   POST /api/b/tickets/{id}/suggest     → {data:{summary, suggested_reply, citations, mock}}
 *   GET  /api/b/tickets/{id}/suggest     → 最近一次建议(进程内缓存)
 * 409 时 toast 后端 message(含「已被他人领取」)。
 */
(function () {
  "use strict";

  var TOKEN_KEY = "sc_token";
  var USER_KEY = "sc_user";
  var WS_RETRY_STEPS = [1000, 2000, 5000]; // 断线退避:1s → 2s → 5s(之后保持 5s)
  var WS_PING_MS = 25000;
  var QUEUE_POLL_MS = 5000;                // WS 断线降级时的队列轮询间隔

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

  /* 16 字节随机 hex(crypto.getRandomValues;旧浏览器退回 Math.random 兜底) */
  function randomHex(byteLen) {
    var buf = new Uint8Array(byteLen);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(buf);
    } else {
      for (var i = 0; i < byteLen; i += 1) buf[i] = Math.floor(Math.random() * 256);
    }
    var out = "";
    for (var j = 0; j < buf.length; j += 1) out += ("0" + buf[j].toString(16)).slice(-2);
    return out;
  }

  var WRITE_METHODS = { POST: true, PUT: true, PATCH: true, DELETE: true };

  /**
   * 统一请求入口(原生 fetch 封装,无 CDN/无构建):
   * - 带 `Authorization: Bearer <token>`
   * - 写请求(POST/PUT/PATCH/DELETE)加防重放头 `X-Timestamp`(Unix 秒)+ `X-Nonce`(16 字节 hex)
   * - 401 → 清登录态并跳 login.html
   */
  async function apiFetch(path, opts) {
    opts = opts || {};
    var method = String(opts.method || "GET").toUpperCase();
    var headers = {};
    if (opts.headers) {
      Object.keys(opts.headers).forEach(function (key) { headers[key] = opts.headers[key]; });
    }
    var token = readToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    if (WRITE_METHODS[method]) {
      headers["X-Timestamp"] = String(Math.floor(Date.now() / 1000));
      headers["X-Nonce"] = randomHex(16);
    }
    var init = {};
    Object.keys(opts).forEach(function (key) { init[key] = opts[key]; });
    init.method = method;
    init.headers = headers;
    var resp = await fetch(path, init);
    if (resp.status === 401) gotoLogin();
    return resp;
  }

  /* 身份一律来自 token(不再读 URL ?merchant_id=,也没有静态坐席列表/手填入口) */
  var ME = readUser();
  var TOKEN = readToken();
  if (!TOKEN || !ME || ME.role !== "agent") {
    gotoLogin();
    return;
  }
  var MERCHANT_ID = ME.merchant_id || "";
  var SERVICE_ID = ME.service_id || "";

  /* W8 收尾:启动时回读 /auth/me 校验登录态(token 可能已过期/被吊销)。
   * - 401 → apiFetch 统一 gotoLogin() 清登录态并回 login.html
   * - 服务端身份与本地缓存不一致 → 覆盖 localStorage 后 reload,重新派生 merchant/service
   * - 网络异常 → 沿用本地缓存身份,后续业务请求的 401 仍会兜底跳转 */
  (async function verifySession() {
    if (!TOKEN) return;
    try {
      var resp = await apiFetch("/auth/me");
      if (!resp.ok) { if (resp.status === 401) gotoLogin(); return; }
      var data = await resp.json();
      var user = data && data.user;
      if (!user) return;
      if (user.id !== ME.id || user.role !== ME.role) {
        try { window.localStorage.setItem(USER_KEY, JSON.stringify(user)); } catch (err) { /* 隐私模式忽略 */ }
        location.reload();
      }
    } catch (err) { /* 网络异常:保留缓存身份 */ }
  })();

  var STATUS_LABELS = {
    queued: "待领取",
    claimed: "跟进中",
    waiting_customer: "待客户回复",
    closed: "已关闭"
  };

  var REASON_FALLBACK = {
    amount_dispute: "金额争议",
    complaint: "投诉 / 风险信号",
    unresolved_3rounds: "连续 3 轮未解决",
    explicit_request: "客户主动要求转人工",
    step_limit: "处理步数上限",
    security_risk: "安全 / 越权风险",
    other: "其他原因"
  };

  var els = {
    agentChip: document.getElementById("agentChip"),
    connBadge: document.getElementById("connBadge"),
    logoutBtn: document.getElementById("logoutBtn"),
    queueList: document.getElementById("queueList"),
    queueState: document.getElementById("queueState"),
    queueCount: document.getElementById("queueCount"),
    queueFilters: document.getElementById("queueFilters"),
    refreshBtn: document.getElementById("refreshBtn"),
    detailEmpty: document.getElementById("detailEmpty"),
    detailBody: document.getElementById("detailBody"),
    suggestPanel: document.getElementById("suggestPanel"),
    suggestBtn: document.getElementById("suggest-btn"),
    suggestSummary: document.getElementById("suggest-summary"),
    suggestReply: document.getElementById("suggest-reply"),
    fillReplyBtn: document.getElementById("fill-reply-btn"),
    toastWrap: document.getElementById("toastWrap")
  };

  var state = {
    agentId: SERVICE_ID,
    statusFilter: "",
    tickets: [],
    selectedId: null,
    selectedTicket: null,
    suggestTicketId: null,
    loadingQueue: false,
    detailBusy: false,
    timer: null,             // 队列轮询定时器(WS 断线降级用)
    refreshTimer: null,      // WS 事件触发的队列刷新去抖
    ws: null,
    wsReady: false,
    wsRetry: 0,
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

  function fmtDateTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function fmtQueueTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var now = new Date();
    var sameDay = d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
    if (sameDay) return pad(d.getHours()) + ":" + pad(d.getMinutes());
    return pad(d.getMonth() + 1) + "-" + pad(d.getDate());
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
    }, 3500);
  }

  function statusLabel(ticket) {
    return ticket.status_label || STATUS_LABELS[ticket.status] || ticket.status || "—";
  }

  function badge(ticket) {
    var cls = "badge badge-" + (ticket.status || "closed");
    return el("span", cls, statusLabel(ticket));
  }

  function reasonLabel(ticket) {
    if (ticket && ticket.handoff_reason_label) return ticket.handoff_reason_label;
    if (ticket && ticket.handoff_reason) {
      return REASON_FALLBACK[ticket.handoff_reason] || ticket.handoff_reason;
    }
    return "—";
  }

  function errorMessage(err) {
    return (err && err.message) ? err.message : String(err);
  }

  /* --------------------------------- HTTP --------------------------------- */

  async function api(path, options) {
    var resp;
    try {
      resp = await apiFetch(path, options || {});
    } catch (err) {
      throw new Error("网络错误:" + errorMessage(err));
    }
    var json = null;
    try { json = await resp.json(); } catch (err) { json = null; }
    if (!resp.ok || (json && json.ok === false)) {
      var message = json && json.error && json.error.message
        ? json.error.message
        : "HTTP " + resp.status;
      var e = new Error(message);
      e.status = resp.status;
      e.code = json && json.error ? json.error.code : null;
      throw e;
    }
    return json || {};
  }

  function postJSON(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  /* --------------------------------- 坐席 --------------------------------- */

  /* W8:坐席身份来自登录 token(service_id / merchant_id),不再有静态 _SERVICES
     选择器或手填 service_id;claim/messages/close 的 body.service_id 仍带 token 值,
     仅作 AUTH_MODE=internal 回归兜底(enforce 下以 token 为准)。 */
  function currentAgent() {
    return state.agentId || SERVICE_ID;
  }

  function renderAgentChip() {
    if (!els.agentChip) return;
    var name = ME.display_name || ME.username || "坐席";
    els.agentChip.textContent = name + "(" + SERVICE_ID + ")";
    els.agentChip.title = "merchant_id: " + MERCHANT_ID;
  }

  /* --------------------------------- 队列 --------------------------------- */

  function sortTickets(items) {
    return items.slice().sort(function (a, b) {
      var qa = a.status === "queued" ? 0 : 1;
      var qb = b.status === "queued" ? 0 : 1;
      if (qa !== qb) return qa - qb;
      var ta = a.last_msg_at_utc ? Date.parse(a.last_msg_at_utc) : 0;
      var tb = b.last_msg_at_utc ? Date.parse(b.last_msg_at_utc) : 0;
      ta = isNaN(ta) ? 0 : ta;
      tb = isNaN(tb) ? 0 : tb;
      if (ta !== tb) return tb - ta;
      var ca = a.created_at_utc ? Date.parse(a.created_at_utc) : 0;
      var cb = b.created_at_utc ? Date.parse(b.created_at_utc) : 0;
      ca = isNaN(ca) ? 0 : ca;
      cb = isNaN(cb) ? 0 : cb;
      return cb - ca;
    });
  }

  function setQueueState(kind, message, actionLabel, action) {
    els.queueState.classList.remove("hidden");
    els.queueState.className = "panel-state" + (kind === "error" ? " error" : "");
    els.queueState.textContent = "";
    var icon = el("div", "state-icon", kind === "error" ? "!" : "·");
    els.queueState.appendChild(icon);
    els.queueState.appendChild(document.createTextNode(message));
    if (actionLabel && action) {
      var btn = el("button", "btn btn-ghost btn-sm", actionLabel);
      btn.type = "button";
      btn.addEventListener("click", action);
      els.queueState.appendChild(document.createElement("br"));
      els.queueState.appendChild(btn);
    }
  }

  function renderQueue() {
    var items = state.tickets.filter(function (t) {
      return !state.statusFilter || t.status === state.statusFilter;
    });
    els.queueCount.textContent = String(items.length);
    var existing = els.queueList.querySelectorAll(".ticket-item");
    for (var i = 0; i < existing.length; i += 1) {
      existing[i].parentNode.removeChild(existing[i]);
    }
    if (!items.length) {
      var emptyMsg = state.statusFilter
        ? "当前筛选下没有工单。"
        : "队列为空,暂时没有待处理工单。";
      setQueueState("empty", emptyMsg, null, null);
      return;
    }
    els.queueState.classList.add("hidden");
    items.forEach(function (ticket) {
      var item = el("button", "ticket-item" +
        (ticket.ticket_id === state.selectedId ? " active" : ""));
      item.type = "button";
      item.setAttribute("data-ticket", ticket.ticket_id);

      var row = el("div", "ticket-row");
      row.appendChild(el("span", "ticket-id", "#" + ticket.ticket_id));
      row.appendChild(badge(ticket));
      row.appendChild(el("span", "ticket-time", fmtQueueTime(ticket.last_msg_at_utc || ticket.created_at_utc)));
      item.appendChild(row);

      var summary = ticket.summary || ticket.last_message ||
        (ticket.handoff_reason_label ? "转人工原因:" + ticket.handoff_reason_label : "无摘要");
      item.appendChild(el("div", "ticket-summary", summary));

      var sub = el("div", "ticket-sub");
      sub.appendChild(el("span", "", "买家 " + (ticket.customer_id || "—")));
      if (ticket.order_id) sub.appendChild(el("span", "", "订单 " + ticket.order_id));
      if (ticket.claimed_by) sub.appendChild(el("span", "", "领取 " + ticket.claimed_by));
      item.appendChild(sub);

      item.addEventListener("click", function () {
        loadDetail(ticket.ticket_id);
      });
      els.queueList.appendChild(item);
    });
  }

  async function loadQueue(silent) {
    if (state.loadingQueue) return;
    state.loadingQueue = true;
    if (!silent) {
      els.queueState.classList.remove("hidden");
      els.queueState.className = "panel-state";
      els.queueState.textContent = "正在加载工单队列…";
    }
    try {
      var query = "/api/b/tickets?merchant_id=" + encodeURIComponent(MERCHANT_ID);
      if (state.statusFilter) query += "&status=" + encodeURIComponent(state.statusFilter);
      var data = await api(query);
      state.tickets = sortTickets(Array.isArray(data.items) ? data.items : []);
      renderQueue();
    } catch (err) {
      els.queueState.classList.remove("hidden");
      setQueueState("error", "工单队列加载失败:" + errorMessage(err), "重试", function () {
        loadQueue();
      });
    } finally {
      state.loadingQueue = false;
    }
  }

  /* --------------------------------- 详情 --------------------------------- */

  function actionButton(label, cls, handler) {
    var btn = el("button", "btn " + cls, label);
    btn.type = "button";
    btn.addEventListener("click", function () {
      if (btn.disabled) return;
      handler(btn);
    });
    return btn;
  }

  function metaItem(key, value) {
    var box = el("div", "meta-item");
    box.appendChild(el("span", "k", key));
    box.appendChild(el("span", "v", value || "—"));
    return box;
  }

  function renderTranscript(messages) {
    var wrap = el("div", "transcript");
    if (!messages || !messages.length) {
      wrap.appendChild(el("div", "panel-state", "暂无会话消息。"));
      return wrap;
    }
    messages.forEach(function (msg) {
      var role = msg.author_type === "agent" ? "agent"
        : msg.author_type === "user" ? "user" : "system";
      var row = el("div", "tmsg " + role + (msg.is_internal ? " internal" : ""));
      var head = el("div", "tmsg-head");
      head.appendChild(el("span", "role-name",
        msg.author_label || (role === "agent" ? "人工客服" : role === "user" ? "买家" : "系统")));
      if (msg.is_internal) head.appendChild(el("span", "internal-badge", "内部"));
      head.appendChild(el("span", "", fmtDateTime(msg.created_at_utc)));
      row.appendChild(head);
      row.appendChild(el("div", "tmsg-bubble", msg.content || ""));
      wrap.appendChild(row);
    });
    return wrap;
  }

  function renderDetail(ticket, messages) {
    els.detailEmpty.classList.add("hidden");
    els.detailBody.classList.remove("hidden");
    els.detailBody.textContent = "";

    var head = el("div", "detail-head");
    var titleBox = el("div", "");
    titleBox.appendChild(el("h2", "", "工单 #" + (ticket.ticket_id || state.selectedId)));
    head.appendChild(titleBox);

    var badges = el("div", "head-badges");
    badges.appendChild(badge(ticket));
    badges.appendChild(el("span", "demo-note", "原因:" + reasonLabel(ticket)));
    head.appendChild(badges);

    var actions = el("div", "detail-actions");

    if (ticket.status === "queued") {
      actions.appendChild(actionButton("领取工单", "btn-primary", function (btn) {
        claimTicket(ticket, btn);
      }));
    }

    if (ticket.status !== "closed") {
      actions.appendChild(actionButton("关闭工单", "btn-danger-ghost", function (btn) {
        closeTicket(ticket, btn);
      }));
    }

    actions.appendChild(actionButton("刷新", "btn-ghost", function (btn) {
      btn.disabled = true;
      loadDetail(ticket.ticket_id).finally(function () { btn.disabled = false; });
    }));

    head.appendChild(actions);
    els.detailBody.appendChild(head);

    var summary = el("div", "summary-block");
    summary.appendChild(el("span", "block-label", "诉求摘要"));
    summary.appendChild(el("span", "summary-text" + (ticket.summary ? "" : " empty-text"),
      ticket.summary || "暂无摘要(转人工时未生成)"));
    els.detailBody.appendChild(summary);

    var grid = el("div", "meta-grid");
    grid.appendChild(metaItem("买家", ticket.customer_id));
    grid.appendChild(metaItem("店铺", ticket.merchant_id));
    grid.appendChild(metaItem("订单", ticket.order_id));
    grid.appendChild(metaItem("转人工原因", reasonLabel(ticket)));
    grid.appendChild(metaItem("领取人", ticket.claimed_by || "未领取"));
    grid.appendChild(metaItem("创建时间", fmtDateTime(ticket.created_at_utc)));
    grid.appendChild(metaItem("最后消息", fmtDateTime(ticket.last_msg_at_utc)));
    if (ticket.message_count !== undefined && ticket.message_count !== null) {
      grid.appendChild(metaItem("消息数", String(ticket.message_count)));
    }
    els.detailBody.appendChild(grid);

    els.detailBody.appendChild(el("div", "section-title", "会话转录"));
    els.detailBody.appendChild(renderTranscript(messages));

    var reply = el("div", "reply-box");
    reply.appendChild(el("div", "section-title", "回复买家"));
    var textarea = el("textarea");
    textarea.id = "reply-input";
    textarea.placeholder = "以「人工客服」身份回复买家…(演示环境,内容会落库)";
    textarea.setAttribute("aria-label", "回复买家内容");
    reply.appendChild(textarea);

    var bar = el("div", "reply-bar");
    var internal = el("label", "switch");
    var internalCheck = document.createElement("input");
    internalCheck.type = "checkbox";
    var internalText = el("span", "", "内部备注(仅坐席可见,不发给买家)");
    internal.appendChild(internalCheck);
    internal.appendChild(internalText);
    bar.appendChild(internal);
    bar.appendChild(el("span", "reply-hint", "回车换行;点击发送提交"));
    var sendBtn = actionButton("发送回复", "btn-primary", function (btn) {
      replyTicket(ticket, textarea, internalCheck, btn);
    });
    bar.appendChild(sendBtn);
    reply.appendChild(bar);

    if (ticket.status === "closed") {
      var closedHint = el("div", "reply-hint");
      closedHint.textContent = "工单已关闭,不可再回复。";
      reply.appendChild(closedHint);
      textarea.disabled = true;
      internalCheck.disabled = true;
      sendBtn.disabled = true;
    }
    els.detailBody.appendChild(reply);

    showSuggestPanel(ticket);
  }

  async function loadDetail(ticketId) {
    if (!ticketId) return;
    state.selectedId = ticketId;
    var items = els.queueList.querySelectorAll(".ticket-item");
    for (var i = 0; i < items.length; i += 1) {
      items[i].classList.toggle("active",
        items[i].getAttribute("data-ticket") === ticketId);
    }
    els.detailEmpty.classList.add("hidden");
    els.detailBody.classList.remove("hidden");
    els.detailBody.textContent = "";
    els.suggestPanel.classList.add("hidden");
    els.detailBody.appendChild(el("div", "panel-state", "正在加载工单详情…"));
    try {
      var data = await api("/api/b/tickets/" + encodeURIComponent(ticketId));
      if (state.selectedId !== ticketId) return;
      state.selectedTicket = data.ticket || null;
      renderDetail(data.ticket || {}, Array.isArray(data.messages) ? data.messages : []);
    } catch (err) {
      if (state.selectedId !== ticketId) return;
      els.detailBody.textContent = "";
      els.suggestPanel.classList.add("hidden");
      var box = el("div", "panel-state error");
      box.appendChild(el("div", "state-icon", "!"));
      box.appendChild(document.createTextNode("工单详情加载失败:" + errorMessage(err)));
      els.detailBody.appendChild(box);
      var retry = el("button", "btn btn-ghost btn-sm", "重试");
      retry.type = "button";
      retry.addEventListener("click", function () { loadDetail(ticketId); });
      els.detailBody.appendChild(retry);
    }
  }

  /* ------------------ Copilot 建议回复(W7-4,MOCK 规则模板) ------------------ */

  function resetSuggestion() {
    els.suggestSummary.textContent = "尚未生成建议。";
    els.suggestSummary.classList.add("empty-text");
    els.suggestReply.value = "";
    els.fillReplyBtn.disabled = true;
  }

  function showSuggestPanel(ticket) {
    els.suggestPanel.classList.remove("hidden");
    if (state.suggestTicketId !== ticket.ticket_id) {
      state.suggestTicketId = ticket.ticket_id;
      resetSuggestion();
    }
    els.suggestBtn.disabled = ticket.status === "closed";
    els.suggestBtn.textContent = "生成建议";
  }

  async function suggestReply(ticket, btn) {
    btn.disabled = true;
    btn.textContent = "生成中…";
    try {
      var data = await postJSON(
        "/api/b/tickets/" + encodeURIComponent(ticket.ticket_id) + "/suggest", {});
      var payload = data.data || {};
      els.suggestSummary.textContent = payload.summary || "暂无摘要";
      els.suggestSummary.classList.remove("empty-text");
      els.suggestReply.value = payload.suggested_reply || "";
      els.fillReplyBtn.disabled = !els.suggestReply.value;
      toast("建议已生成(MOCK 规则模板,仅供坐席参考)", "success");
    } catch (err) {
      toast("生成建议失败:" + errorMessage(err), "error");
    } finally {
      btn.textContent = "生成建议";
      btn.disabled = ticket.status === "closed";
    }
  }

  function fillReplyFromSuggestion() {
    var text = els.suggestReply.value.trim();
    if (!text) {
      toast("还没有可填入的建议回复", "error");
      return;
    }
    var target = els.detailBody.querySelector("#reply-input");
    if (!target) {
      toast("回复框尚未就绪,请重新打开工单", "error");
      return;
    }
    target.value = els.suggestReply.value;
    target.focus();
    toast("已填入回复框,请确认或编辑后发送", "success");
  }

  /* --------------------------------- 动作 --------------------------------- */

  async function claimTicket(ticket, btn) {
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = "领取中…";
    try {
      await postJSON("/api/b/tickets/" + encodeURIComponent(ticket.ticket_id) + "/claim", {
        service_id: currentAgent()
      });
      toast("已领取工单 #" + ticket.ticket_id, "success");
      await loadDetail(ticket.ticket_id);
      await loadQueue(true);
    } catch (err) {
      toast("领取失败:" + errorMessage(err), "error");
      btn.disabled = false;
      btn.textContent = original;
      await loadDetail(ticket.ticket_id);
      await loadQueue(true);
    }
  }

  async function replyTicket(ticket, textarea, internalCheck, btn) {
    var content = textarea.value.trim();
    if (!content) {
      textarea.focus();
      toast("回复内容不能为空", "error");
      return;
    }
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = "发送中…";
    try {
      await postJSON("/api/b/tickets/" + encodeURIComponent(ticket.ticket_id) + "/messages", {
        service_id: currentAgent(),
        content: content,
        is_internal: !!internalCheck.checked
      });
      toast(internalCheck.checked ? "内部备注已保存" : "回复已发送买家", "success");
      await loadDetail(ticket.ticket_id);
      await loadQueue(true);
    } catch (err) {
      toast("回复失败:" + errorMessage(err), "error");
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  async function closeTicket(ticket, btn) {
    var ok = window.confirm("确认关闭工单 #" + ticket.ticket_id + "?关闭后买家侧将停止输入。");
    if (!ok) return;
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = "关闭中…";
    try {
      await postJSON("/api/b/tickets/" + encodeURIComponent(ticket.ticket_id) + "/close", {
        service_id: currentAgent()
      });
      toast("工单 #" + ticket.ticket_id + " 已关闭", "success");
      await loadDetail(ticket.ticket_id);
      await loadQueue(true);
    } catch (err) {
      toast("关单失败:" + errorMessage(err), "error");
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  /* ------------------- W8:WS 主通道 + 队列轮询降级 -------------------- */
  /*
   * 主通道:GET /ws/agent/{service_id}?token=,服务端订阅
   * agent:<service_id> + queue:<merchant_id>;事件:ticket_update / queue_new
   * (ticket_status 防御性处理)。心跳 {"type":"ping"} → pong。
   * 降级:WS 正常时暂停队列轮询;onclose 后恢复轮询并 1s/2s/5s 退避重连。
   * 注意:自动刷新详情时若坐席正在回复框输入,不重绘(避免覆盖草稿)。
   */

  function connBadge(stateName, text) {
    if (!els.connBadge) return;
    els.connBadge.setAttribute("data-state", stateName);
    els.connBadge.textContent = text;
  }

  function wsUrl() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + "/ws/agent/" +
      encodeURIComponent(SERVICE_ID) + "?token=" + encodeURIComponent(readToken());
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

  /* WS 断线降级:队列轮询(旧刷新路径),WS 正常时不跑 */
  function startQueuePolling() {
    if (state.timer || state.wsReady) return;
    state.timer = window.setInterval(function () {
      loadQueue(true);
    }, QUEUE_POLL_MS);
  }

  function stopQueuePolling() {
    if (state.timer) {
      window.clearInterval(state.timer);
      state.timer = null;
    }
  }

  /* WS 事件触发的刷新(去抖 300ms):队列必刷;详情仅在坐席未输入时重绘 */
  function scheduleQueueRefresh() {
    if (state.refreshTimer) return;
    state.refreshTimer = window.setTimeout(function () {
      state.refreshTimer = null;
      loadQueue(true);
      if (!state.selectedId) return;
      var replyInput = els.detailBody.querySelector("#reply-input");
      if (replyInput && replyInput.value.trim()) return; // 保护未发送草稿
      loadDetail(state.selectedId);
    }, 300);
  }

  function scheduleWsReconnect() {
    if (state.wsClosedByUs) return;
    var wait = WS_RETRY_STEPS[Math.min(state.wsRetry, WS_RETRY_STEPS.length - 1)];
    state.wsRetry += 1;
    if (state.wsReconnectTimer) window.clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = window.setTimeout(function () {
      state.wsReconnectTimer = null;
      connectWs();
    }, wait);
    connBadge("offline", "实时已断开 · " + Math.round(wait / 1000) + "s 后重连");
  }

  function connectWs() {
    // W8-FIX:显式重连前会先 closeWs()(置 wsClosedByUs=true),这里必须先复位,
    // 否则重连被守卫挡掉,工作台 WS 永不重建。
    if (!SERVICE_ID || !readToken()) return;
    if (state.ws &&
        (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    state.wsClosedByUs = false;
    connBadge("connecting", "实时连接中…");
    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      scheduleWsReconnect();
      return;
    }
    state.ws = ws;
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
      if (state.wsClosedByUs) {
        connBadge("offline", "实时已断开");
        return;
      }
      startQueuePolling(); // 降级:WS 断开时恢复队列轮询
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
        connBadge("online", "实时已连接");
        startWsPing();
        stopQueuePolling();  // WS 正常 → 暂停轮询
        break;
      case "pong":
        break;
      case "ticket_update":
      case "ticket_status":
        scheduleQueueRefresh();
        break;
      case "queue_new":
        toast("有新工单进入队列", "warn");
        scheduleQueueRefresh();
        break;
      default:
        break;
    }
  }

  /* -------------------------------- 事件绑定 ------------------------------- */

  els.refreshBtn.addEventListener("click", function () {
    loadQueue();
    if (state.selectedId) loadDetail(state.selectedId);
  });

  els.queueFilters.addEventListener("click", function (ev) {
    var chip = ev.target.closest ? ev.target.closest(".filter-chip") : null;
    if (!chip) return;
    var chips = els.queueFilters.querySelectorAll(".filter-chip");
    for (var i = 0; i < chips.length; i += 1) chips[i].classList.remove("active");
    chip.classList.add("active");
    state.statusFilter = chip.getAttribute("data-status") || "";
    loadQueue();
  });

  /* W7-4:生成建议 + 一键填入回复框(只填入不发送,坐席可编辑) */
  els.suggestBtn.addEventListener("click", function () {
    if (!state.selectedTicket || !state.selectedId) {
      toast("请先选择工单", "error");
      return;
    }
    suggestReply(state.selectedTicket, els.suggestBtn);
  });

  els.fillReplyBtn.addEventListener("click", fillReplyFromSuggestion);

  /* W8:退出登录(清登录态后回登录页) */
  if (els.logoutBtn) {
    els.logoutBtn.addEventListener("click", function () {
      closeWs();
      stopQueuePolling();
      try {
        window.localStorage.removeItem(TOKEN_KEY);
        window.localStorage.removeItem(USER_KEY);
      } catch (err) { /* 隐私模式忽略 */ }
      location.replace("/login.html");
    });
  }

  /* --------------------------------- 启动 --------------------------------- */

  (async function boot() {
    renderAgentChip();
    startQueuePolling(); // 旧轮询先跑(WS 未就绪时是唯一自动刷新路径)
    connectWs();         // 主通道;ready 后暂停轮询
    loadQueue();
  })();
})();
