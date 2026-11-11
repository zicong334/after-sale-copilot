/* 安心退 · 客服工作台(原生 JS,零第三方依赖 零构建)
 * 对接契约 §二 /api/b/*(全部带 Authorization: Bearer <token>):
 *   GET  /api/b/tickets?merchant_id=(固定拉全量,前端按状态筛选)
 *   GET  /api/b/tickets/{id}
 *   POST /api/b/tickets/{id}/claim      {service_id}
 *   POST /api/b/tickets/{id}/messages   {service_id, content, is_internal?}
 *   POST /api/b/tickets/{id}/close      {service_id?}
 *   POST /api/b/tickets/{id}/suggest     → {data:{summary, suggested_reply, citations}}
 *   GET  /api/b/tickets/{id}/suggest     → 最近一次建议(进程内缓存)
 * 409 时 toast 后端 message(含「已被他人领取」)。
 * W14 前端体验包:界面只出现业务语言——短单号、中文状态、可直接发的话术,
 * 不出现内部实现术语(状态枚举、索引细节、trace 等)。
 */
(function () {
  "use strict";

  /* W13 ISS-45/19:客服与买家各用独立 localStorage 槽位,跨角色打开互不删除。 */
  var TOKEN_KEY = "sc_agent_token";
  var USER_KEY = "sc_agent_user";
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
        var saved = false;
        try { window.localStorage.setItem(USER_KEY, JSON.stringify(user)); saved = true; } catch (err) { /* 隐私模式忽略 */ }
        /* W13 ISS-69:仅角色变化(需重新派生权限)且缓存写入成功时才刷新;
         * 同角色仅 id/昵称变化就地沿用,不做无条件 reload。 */
        if (saved && user.role !== ME.role) location.reload();
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
    amount_dispute: "退款金额争议",
    refund_dispute: "退款金额争议",
    logistics_delay: "物流超时未更新",
    shipping_delay: "物流超时未更新",
    quality_defect: "商品瑕疵 / 质量问题",
    complaint: "投诉风险",
    unresolved_3rounds: "连续 3 轮未解决",
    explicit_request: "买家明确要求转人工",
    step_limit: "处理步数已达上限",
    security_risk: "安全风险",
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
    prevLastMsg: Object.create(null), // ticket_id → 上次看到的最后消息时间
    unread: Object.create(null),      // ticket_id → true(买家有新消息)
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

  /* W14 ISS-17/32:工单号展示后 6 位短码,长 UUID 只用于接口与埋点。 */
  function shortCode(id) {
    var raw = String(id || "").replace(/-/g, "");
    if (!raw) return "—";
    return "#" + raw.slice(-6).toUpperCase();
  }

  /* 对客界面里的实现细节统一转成业务话术。 */
  function bizCopy(text) {
    return String(text == null ? "" : text)
      .replace(/policy_search/g, "线上自助查询")
      .replace(/\bverified\b/g, "已核验")
      .replace(/\bqueued\b/g, "排队中")
      .replace(/\bactive\b/g, "生效")
      .replace(/\bretired\b/g, "已停用");
  }

  /* W14 ISS-17/40:副标题优先用真实对话摘要,其次最后一句,最后才是原因描述。 */
  function ticketSummary(ticket) {
    var raw = (ticket.summary || ticket.last_message || "").trim();
    if (raw) {
      var first = raw.split(/[\n。！？!?]/)[0].trim();
      return bizCopy(first.length > 48 ? first.slice(0, 48) + "…" : first);
    }
    var reason = reasonLabel(ticket);
    return reason && reason !== "—" ? "买家因" + reason + "转人工" : "买家发起人工咨询";
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
      return REASON_FALLBACK[ticket.handoff_reason] || "其他原因";
    }
    return "暂未标注原因";
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
    var name = ME.display_name || ME.username || "值班坐席";
    els.agentChip.textContent = name;
    els.agentChip.title = "坐席身份由登录账号决定";
  }

  /* --------------------------------- 队列 --------------------------------- */

  function sortTickets(items) {
    return items.slice().sort(function (a, b) {
      /* W14 ISS-29:有买家新消息的工单永远置顶。 */
      var ua = state.unread[a.ticket_id] ? 0 : 1;
      var ub = state.unread[b.ticket_id] ? 0 : 1;
      if (ua !== ub) return ua - ub;
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

  function renderQueueCounts() {
    var counts = { all: state.tickets.length, queued: 0, claimed: 0, waiting_customer: 0, closed: 0 };
    state.tickets.forEach(function (t) {
      if (counts[t.status] !== undefined) counts[t.status] += 1;
    });
    var nodes = els.queueFilters ? els.queueFilters.querySelectorAll(".chip-count") : [];
    for (var i = 0; i < nodes.length; i += 1) {
      var key = nodes[i].getAttribute("data-count") || "all";
      nodes[i].textContent = String(counts[key] || 0);
    }
  }

  function renderQueue() {
    renderQueueCounts();
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
        (ticket.ticket_id === state.selectedId ? " active" : "") +
        (state.unread[ticket.ticket_id] ? " has-new" : ""));
      item.type = "button";
      item.setAttribute("data-ticket", ticket.ticket_id);

      var row = el("div", "ticket-row");
      row.appendChild(el("span", "ticket-id", shortCode(ticket.ticket_id)));
      row.appendChild(badge(ticket));
      if (state.unread[ticket.ticket_id]) {
        row.appendChild(el("span", "new-dot", "● 新消息"));
      }
      row.appendChild(el("span", "ticket-time", fmtQueueTime(ticket.last_msg_at_utc || ticket.created_at_utc)));
      item.appendChild(row);

      item.appendChild(el("div", "ticket-summary", ticketSummary(ticket)));

      var sub = el("div", "ticket-sub");
      sub.appendChild(el("span", "", "买家 " + shortCode(ticket.customer_id)));
      if (ticket.order_id) sub.appendChild(el("span", "", "订单 " + shortCode(ticket.order_id)));
      if (ticket.claimed_by) sub.appendChild(el("span", "", "领取 " + shortCode(ticket.claimed_by)));
      item.appendChild(sub);

      item.addEventListener("click", function () {
        delete state.unread[ticket.ticket_id];
        item.classList.remove("has-new");
        var dot = item.querySelector(".new-dot");
        if (dot && dot.parentNode) dot.parentNode.removeChild(dot);
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
      /* W14 ISS-40:一次拉全量,前端按状态过滤并显示各状态数量,
       * 否则筛选 Tab 上的计数永远只能是当前筛选的那一档。 */
      var query = "/api/b/tickets?merchant_id=" + encodeURIComponent(MERCHANT_ID);
      var data = await api(query);
      var items = Array.isArray(data.items) ? data.items : [];
      /* W14 ISS-29:与本轮已知的最后消息时间做差量,买家追评即打「新消息」。 */
      items.forEach(function (t) {
        var prev = state.prevLastMsg[t.ticket_id];
        var now = t.last_msg_at_utc || "";
        if (prev && now && Date.parse(now) > Date.parse(prev) &&
            t.ticket_id !== state.selectedId) {
          state.unread[t.ticket_id] = true;
        }
        state.prevLastMsg[t.ticket_id] = now;
      });
      state.tickets = sortTickets(items);
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
      else if (msg.author_type === "assistant") head.appendChild(el("span", "internal-badge", "机器人"));
      head.appendChild(el("span", "", fmtDateTime(msg.created_at_utc)));
      row.appendChild(head);
      row.appendChild(el("div", "tmsg-bubble", msg.content || ""));
      wrap.appendChild(row);
    });
    return wrap;
  }

  function renderDetail(ticket, messages, chatTranscript) {
    els.detailEmpty.classList.add("hidden");
    els.detailBody.classList.remove("hidden");
    els.detailBody.textContent = "";

    var head = el("div", "detail-head");
    var titleBox = el("div", "");
    titleBox.appendChild(el("h2", "", "工单 " + shortCode(ticket.ticket_id || state.selectedId)));
    head.appendChild(titleBox);

    var badges = el("div", "head-badges");
    badges.appendChild(badge(ticket));
    badges.appendChild(el("span", "demo-note", "原因 · " + reasonLabel(ticket)));
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
    summary.appendChild(el("span", "summary-text" + (ticket.summary || ticket.last_message ? "" : " empty-text"),
      ticketSummary(ticket)));
    els.detailBody.appendChild(summary);

    var grid = el("div", "meta-grid");
    grid.appendChild(metaItem("买家", shortCode(ticket.customer_id)));
    grid.appendChild(metaItem("店铺", "本店"));
    grid.appendChild(metaItem("订单", shortCode(ticket.order_id)));
    grid.appendChild(metaItem("转人工原因", reasonLabel(ticket)));
    grid.appendChild(metaItem("领取人", ticket.claimed_by ? shortCode(ticket.claimed_by) : "未领取"));
    grid.appendChild(metaItem("创建时间", fmtDateTime(ticket.created_at_utc)));
    grid.appendChild(metaItem("最后消息", fmtDateTime(ticket.last_msg_at_utc)));
    if (ticket.message_count !== undefined && ticket.message_count !== null) {
      grid.appendChild(metaItem("消息数", String(ticket.message_count)));
    }
    els.detailBody.appendChild(grid);

    if (chatTranscript && chatTranscript.length) {
      els.detailBody.appendChild(el("div", "section-title", "转人工前 AI 问答(只读)"));
      var chatWrap = el("div", "transcript chat-prefix");
      chatTranscript.forEach(function (item) {
        var role = item.author_type === "user" ? "user" : "system";
        var row = el("div", "tmsg " + role);
        var tHead = el("div", "tmsg-head");
        tHead.appendChild(el("span", "role-name",
          item.author_label || (role === "user" ? "买家" : "AI 助手")));
        if (role !== "user") tHead.appendChild(el("span", "internal-badge", "机器人"));
        tHead.appendChild(el("span", "", fmtDateTime(item.created_at_utc)));
        row.appendChild(tHead);
        row.appendChild(el("div", "tmsg-bubble", bizCopy(item.content || "")));
        chatWrap.appendChild(row);
      });
      els.detailBody.appendChild(chatWrap);
    }
    els.detailBody.appendChild(el("div", "section-title", "会话转录"));
    els.detailBody.appendChild(renderTranscript(messages));

    var reply = el("div", "reply-box");
    reply.appendChild(el("div", "section-title", "回复买家"));

    /* W14 ISS-43:未领取的工单先领取再回复,避免抢单前误发。 */
    if (ticket.status === "queued") {
      var locked = el("div", "reply-locked");
      locked.appendChild(el("div", "lock-icon", "🔒"));
      locked.appendChild(el("div", "lock-text", "请先领取工单后回复"));
      if (els.detailBody) {
        locked.appendChild(actionButton("领取工单", "btn-primary", function (btn) {
          claimTicket(ticket, btn);
        }));
      }
      reply.appendChild(locked);
      els.detailBody.appendChild(reply);
      showSuggestPanel(ticket);
      return;
    }

    /* W14 ISS-27:公开发送 / 内部备忘拆成独立选项卡。 */
    var tabs = el("div", "reply-tabs");
    var publicTab = el("button", "reply-tab active", "公开发送给买家");
    publicTab.type = "button";
    publicTab.setAttribute("aria-pressed", "true");
    var internalTab = el("button", "reply-tab", "内部备忘（仅坐席可见）");
    internalTab.type = "button";
    internalTab.setAttribute("aria-pressed", "false");
    tabs.appendChild(publicTab);
    tabs.appendChild(internalTab);
    reply.appendChild(tabs);

    var internalCheck = document.createElement("input");
    internalCheck.type = "checkbox";
    internalCheck.className = "sr-only";
    reply.appendChild(internalCheck);

    function setMode(internal) {
      internalCheck.checked = internal;
      publicTab.classList.toggle("active", !internal);
      internalTab.classList.toggle("active", internal);
      publicTab.setAttribute("aria-pressed", String(!internal));
      internalTab.setAttribute("aria-pressed", String(internal));
    }
    publicTab.addEventListener("click", function () { setMode(false); });
    internalTab.addEventListener("click", function () { setMode(true); });

    var textarea = el("textarea");
    textarea.id = "reply-input";
    textarea.placeholder = "输入回复内容，Enter 发送，Shift+Enter 换行……";
    textarea.setAttribute("aria-label", "回复买家内容");
    reply.appendChild(textarea);

    var bar = el("div", "reply-bar");
    /* W14 ISS-27:工单状态单独展示(只读),不再和「内部备注」勾选耦合。 */
    var statusWrap = el("label", "reply-status-wrap");
    statusWrap.appendChild(el("span", "", "工单状态"));
    var statusSelect = document.createElement("select");
    statusSelect.id = "replyStatus";
    statusSelect.className = "reply-status";
    statusSelect.disabled = true;
    statusSelect.title = "状态随领取/关单自动流转";
    Object.keys(STATUS_LABELS).forEach(function (key) {
      var opt = document.createElement("option");
      opt.value = key;
      opt.textContent = STATUS_LABELS[key];
      if (key === ticket.status) opt.selected = true;
      statusSelect.appendChild(opt);
    });
    statusWrap.appendChild(statusSelect);
    bar.appendChild(statusWrap);
    bar.appendChild(el("span", "reply-hint", "Enter 发送 · Shift+Enter 换行"));
    var sendBtn = actionButton("发送", "btn-primary", function (btn) {
      replyTicket(ticket, textarea, internalCheck, btn);
    });
    bar.appendChild(sendBtn);
    reply.appendChild(bar);

    /* W14 ISS-18:回复框 Enter 直接发送,Shift+Enter 换行。 */
    textarea.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        if (!sendBtn.disabled) sendBtn.click();
      }
    });

    if (ticket.status === "closed") {
      var closedHint = el("div", "reply-hint");
      closedHint.textContent = "工单已关闭,不可再回复。";
      reply.appendChild(closedHint);
      textarea.disabled = true;
      internalCheck.disabled = true;
      sendBtn.disabled = true;
      publicTab.disabled = true;
      internalTab.disabled = true;
    }
    els.detailBody.appendChild(reply);

    showSuggestPanel(ticket);
  }

  /* W14 ISS-53:静默刷新只替换转录/徽章/摘要,保留回复框草稿与滚动位置。 */
  function refreshDetailInPlace(ticket, messages, chatTranscript) {
    var body = els.detailBody;
    var badges = body.querySelector(".head-badges");
    if (badges) {
      badges.textContent = "";
      badges.appendChild(badge(ticket));
      badges.appendChild(el("span", "demo-note", "原因 · " + reasonLabel(ticket)));
    }
    var summaryText = body.querySelector(".summary-text");
    if (summaryText) {
      summaryText.classList.toggle("empty-text", !(ticket.summary || ticket.last_message));
      summaryText.textContent = ticketSummary(ticket);
    }
    var meta = body.querySelector(".meta-grid");
    if (meta) {
      meta.textContent = "";
      meta.appendChild(metaItem("买家", shortCode(ticket.customer_id)));
      meta.appendChild(metaItem("店铺", "本店"));
      meta.appendChild(metaItem("订单", shortCode(ticket.order_id)));
      meta.appendChild(metaItem("转人工原因", reasonLabel(ticket)));
      meta.appendChild(metaItem("领取人", ticket.claimed_by ? shortCode(ticket.claimed_by) : "未领取"));
      meta.appendChild(metaItem("创建时间", fmtDateTime(ticket.created_at_utc)));
      meta.appendChild(metaItem("最后消息", fmtDateTime(ticket.last_msg_at_utc)));
      if (ticket.message_count !== undefined && ticket.message_count !== null) {
        meta.appendChild(metaItem("消息数", String(ticket.message_count)));
      }
    }
    var oldTranscript = body.querySelector(".transcript:not(.chat-prefix)");
    if (oldTranscript && oldTranscript.parentNode) {
      oldTranscript.parentNode.replaceChild(renderTranscript(messages), oldTranscript);
    }
    var prefix = body.querySelector(".chat-prefix");
    if (prefix && chatTranscript && chatTranscript.length) {
      prefix.textContent = "";
      chatTranscript.forEach(function (item) {
        var role = item.author_type === "user" ? "user" : "system";
        var row = el("div", "tmsg " + role);
        var tHead = el("div", "tmsg-head");
        tHead.appendChild(el("span", "role-name",
          item.author_label || (role === "user" ? "买家" : "AI 助手")));
        tHead.appendChild(el("span", "", fmtDateTime(item.created_at_utc)));
        row.appendChild(tHead);
        row.appendChild(el("div", "tmsg-bubble", bizCopy(item.content || "")));
        prefix.appendChild(row);
      });
    }
  }

  async function loadDetail(ticketId, opts) {
    if (!ticketId) return;
    var silent = !!(opts && opts.silent);
    state.selectedId = ticketId;
    var items = els.queueList.querySelectorAll(".ticket-item");
    for (var i = 0; i < items.length; i += 1) {
      items[i].classList.toggle("active",
        items[i].getAttribute("data-ticket") === ticketId);
    }
    els.detailEmpty.classList.add("hidden");
    els.detailBody.classList.remove("hidden");
    var scrollTop = els.detailBody.scrollTop;
    if (!silent) {
      els.detailBody.textContent = "";
      els.suggestPanel.classList.add("hidden");
      els.detailBody.appendChild(el("div", "panel-state", "正在加载工单详情…"));
    }
    try {
      var data = await api("/api/b/tickets/" + encodeURIComponent(ticketId));
      if (state.selectedId !== ticketId) return;
      state.selectedTicket = data.ticket || null;
      /* W13 ISS-13:先渲染转人工前的 AI 问答(chat_message),再渲染工单消息;
       * messages 里已含的 chat 条目(assistant)按 id 去重,避免同一句出现两次。 */
      var shownChatIds = {};
      (Array.isArray(data.messages) ? data.messages : []).forEach(function (m) {
        if (m && m.id) shownChatIds[m.id] = true;
      });
      var chatTranscript = (Array.isArray(data.chat_transcript) ? data.chat_transcript : [])
        .filter(function (m) { return m && m.source === "chat" && !shownChatIds[m.id]; });
      var msgs = Array.isArray(data.messages) ? data.messages : [];
      if (silent && els.detailBody.querySelector(".transcript:not(.chat-prefix)")) {
        refreshDetailInPlace(data.ticket || {}, msgs, chatTranscript);
        els.detailBody.scrollTop = scrollTop;
      } else {
        renderDetail(data.ticket || {}, msgs, chatTranscript);
      }
    } catch (err) {
      if (state.selectedId !== ticketId || silent) return;
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

  /* ------------------- AI 建议回复(W7-4,AI 建议话术) ------------------- */

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

  /* W14 ISS-16/18:建议必须是能直接发给买家的一句话;
   * 去掉寒暄套话,补上订单号与诉求背景,坐席点开就能发。 */
  function enhanceSuggestedReply(ticket, payload) {
    var reply = String(payload.suggested_reply || payload.reply || "").trim();
    if (!reply) return "";
    reply = reply.replace(/^(您好|你好|亲爱的|亲)[，,、:：\s]*/, "");
    var mentionsOrder = ticket.order_id &&
      (reply.indexOf(shortCode(ticket.order_id)) >= 0 ||
       reply.indexOf(String(ticket.order_id)) >= 0);
    if (!mentionsOrder) {
      var reason = reasonLabel(ticket);
      var prefix = reason && reason !== "暂未标注原因"
        ? "关于订单 " + shortCode(ticket.order_id) + " 的" + reason + "，"
        : "关于订单 " + shortCode(ticket.order_id) + " 的售后问题，";
      reply = prefix + "已为您跟进处理，" + reply;
    }
    return reply;
  }

  async function suggestReply(ticket, btn) {
    btn.disabled = true;
    btn.textContent = "生成中…";
    try {
      var data = await postJSON(
        "/api/b/tickets/" + encodeURIComponent(ticket.ticket_id) + "/suggest", {});
      var payload = data.data || {};
      var summary = String(payload.summary || "").trim() || ticketSummary(ticket);
      els.suggestSummary.textContent = bizCopy(summary);
      els.suggestSummary.classList.remove("empty-text");
      els.suggestReply.value = enhanceSuggestedReply(ticket, payload);
      els.fillReplyBtn.disabled = !els.suggestReply.value;
      toast("建议已生成，可直接编辑后发送", "success");
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
      toast("已领取工单 " + shortCode(ticket.ticket_id), "success");
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

  /* W13 ISS-26:关单前弹确认框并收集结单原因,替代静默 window.confirm。 */
  function askCloseReason(ticket) {
    return new Promise(function (resolve) {
      var modal = document.getElementById("closeModal");
      var input = document.getElementById("closeReason");
      var confirmBtn = document.getElementById("closeConfirm");
      var cancelBtn = document.getElementById("closeCancel");
      var idBox = document.getElementById("closeTicketId");
      if (!modal || !input || !confirmBtn || !cancelBtn) {
        var text = window.prompt("请填写结单原因(取消则不关单)", "已为买家解决");
        resolve(text === null ? null : text);
        return;
      }
      if (idBox) idBox.textContent = shortCode(ticket.ticket_id);
      input.value = "";
      modal.classList.remove("hidden");
      input.focus();
      function cleanup() {
        modal.classList.add("hidden");
        confirmBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        input.removeEventListener("keydown", onKey);
      }
      function onOk() {
        var value = input.value.trim();
        if (!value) { input.focus(); return; }
        cleanup();
        resolve(value);
      }
      function onCancel() { cleanup(); resolve(null); }
      function onKey(ev) {
        if (ev.key === "Enter") { ev.preventDefault(); onOk(); }
        else if (ev.key === "Escape") { onCancel(); }
      }
      confirmBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      input.addEventListener("keydown", onKey);
    });
  }

  async function closeTicket(ticket, btn) {
    var reason = await askCloseReason(ticket);
    if (reason === null) return;
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = "关闭中…";
    try {
      /* 结单原因落内部备注(CloseIn 契约不变),再关单。 */
      await postJSON("/api/b/tickets/" + encodeURIComponent(ticket.ticket_id) + "/messages", {
        service_id: currentAgent(),
        content: "结单原因:" + reason,
        is_internal: true
      });
      await postJSON("/api/b/tickets/" + encodeURIComponent(ticket.ticket_id) + "/close", {
        service_id: currentAgent()
      });
      toast("工单 " + shortCode(ticket.ticket_id) + " 已关闭", "success");
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
   * 主通道:GET /ws/agent/{service_id},连上后首帧 auth(不把 token 拼 URL);服务端订阅
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
      encodeURIComponent(SERVICE_ID);
    /* W13 ISS-67:token 不再拼进 URL(避免落入访问日志),改连上后首帧 auth。 */
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
  function scheduleQueueRefresh(ticketId) {
    if (state.refreshTimer) return;
    state.refreshTimer = window.setTimeout(function () {
      state.refreshTimer = null;
      loadQueue(true);
      if (!state.selectedId) return;
      /* W14 ISS-53:事件只静默刷新转录/徽章,不重建详情,草稿与滚动都不丢;
       * 其他工单的事件只刷新队列,不打断当前正在处理的工单。 */
      if (ticketId && ticketId !== state.selectedId) return;
      loadDetail(state.selectedId, { silent: true });
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
    connBadge("offline", "服务暂时离线 · 正在为您重连…");
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
    connBadge("connecting", "正在连接服务…");
    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      scheduleWsReconnect();
      return;
    }
    state.ws = ws;
    ws.onopen = function () {
      try { ws.send(JSON.stringify({ type: "auth", token: readToken() })); } catch (err) { /* onclose 处理 */ }
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
      if (state.wsClosedByUs) {
        connBadge("offline", "服务已离线");
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
        connBadge("online", "服务已连接");
        startWsPing();
        stopQueuePolling();  // WS 正常 → 暂停轮询
        break;
      case "pong":
        break;
      case "ticket_update":
      case "ticket_status":
        scheduleQueueRefresh(msg.ticket_id);
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
    renderQueue(); // 全量已在本地,切换筛选无需再请求
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

  /* --------------------- W9-3 知识库管理(/api/kb,agent) --------------------- */

  var KB_STATUS_LABELS = { draft: "草稿", active: "生效", retired: "已停用" };
  var kb = { items: [], editingId: null, loaded: false, savedSnapshot: "" };
  var kbFilter = { q: "", filter: "all" };
  var kbEls = {
    view: document.getElementById("kbView"),
    toggle: document.getElementById("kbToggle"),
    back: document.getElementById("kbBack"),
    list: document.getElementById("kbList"),
    stats: document.getElementById("kbStats"),
    form: document.getElementById("kbEditor"),
    editorTitle: document.getElementById("kbEditorTitle"),
    clauseId: document.getElementById("kbClauseId"),
    title: document.getElementById("kbTitle"),
    status: document.getElementById("kbStatus"),
    content: document.getElementById("kbContent"),
    hint: document.getElementById("kbEditorHint"),
    newBtn: document.getElementById("kbNew"),
    cancel: document.getElementById("kbCancel"),
    reloadBtn: document.getElementById("kbReload"),
    upload: document.getElementById("kbUpload"),
    search: document.getElementById("kbSearch"),
    filters: document.getElementById("kbFilters")
  };

  function kbShow() {
    var main = document.querySelector(".wb-main");
    if (main) main.classList.add("hidden");
    if (kbEls.view) kbEls.view.classList.remove("hidden");
    if (!kb.loaded) kbLoadAll();
  }

  function kbHide() {
    var main = document.querySelector(".wb-main");
    if (main) main.classList.remove("hidden");
    if (kbEls.view) kbEls.view.classList.add("hidden");
  }

  function kbBadge(status) {
    var span = document.createElement("span");
    span.className = "kb-badge kb-badge-" + status;
    span.textContent = KB_STATUS_LABELS[status] || status;
    return span;
  }

  function kbRenderStats(stats) {
    if (!kbEls.stats) return;
    kbEls.stats.textContent = "";
    var by = stats.by_status || {};
    var parts = [
      "共 " + stats.total + " 条",
      "生效 " + (by.active || 0),
      "草稿 " + (by.draft || 0),
      "已停用 " + (by.retired || 0)
    ];
    parts.forEach(function (text, idx) {
      if (idx) kbEls.stats.appendChild(document.createTextNode("  ·  "));
      var span = document.createElement("span");
      span.textContent = text;
      kbEls.stats.appendChild(span);
    });
  }

  function kbActionButton(label, action, item, cls) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost btn-sm " + (cls || "");
    btn.textContent = label;
    btn.setAttribute("data-kb-action", action);
    btn.setAttribute("data-kb-id", item.id);
    return btn;
  }

  function kbFilteredItems() {
    var q = (kbFilter.q || "").trim().toLowerCase();
    return kb.items.filter(function (item) {
      if (kbFilter.filter === "RP" && !/^RP-/i.test(item.clause_id || "")) return false;
      if (kbFilter.filter === "LP" && !/^LP-/i.test(item.clause_id || "")) return false;
      if (kbFilter.filter === "retired" && item.status !== "retired") return false;
      if (!q) return true;
      var hay = [item.clause_id || "", item.title || "", item.content || ""]
        .join("\n").toLowerCase();
      return hay.indexOf(q) >= 0;
    });
  }

  function kbRenderList() {
    if (!kbEls.list) return;
    kbEls.list.textContent = "";
    var filtered = kbFilteredItems();
    if (!filtered.length) {
      var empty = document.createElement("div");
      empty.className = "kb-empty";
      empty.textContent = kb.items.length
        ? "没有匹配的条款，换个关键词或筛选试试。"
        : "还没有条款。可「新建条款」或「上传 MD」自动切片。";
      kbEls.list.appendChild(empty);
      return;
    }
    filtered.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "kb-item";
      row.setAttribute("role", "listitem");
      row.setAttribute("data-kb-id", item.id);

      var head = document.createElement("div");
      head.className = "kb-item-head";
      var cid = document.createElement("span");
      cid.className = "kb-clause-id";
      cid.textContent = item.clause_id || "(无条款号)";
      var title = document.createElement("span");
      title.className = "kb-item-title";
      title.textContent = item.title || "";
      var ver = document.createElement("span");
      ver.className = "kb-version";
      ver.textContent = "v" + item.version;
      head.appendChild(cid);
      head.appendChild(title);
      head.appendChild(ver);
      head.appendChild(kbBadge(item.status));

      var text = document.createElement("p");
      text.className = "kb-item-text";
      var raw = item.content || "";
      text.textContent = raw.length > 90 ? raw.slice(0, 90) + "…" : raw;

      var actions = document.createElement("div");
      actions.className = "kb-item-actions";
      actions.appendChild(kbActionButton("编辑", "edit", item));
      if (item.status === "draft") {
        actions.appendChild(kbActionButton("启用", "activate", item, "kb-activate"));
      } else if (item.status === "active") {
        actions.appendChild(kbActionButton("停用", "retire", item, "kb-danger"));
      } else if (item.status === "retired") {
        actions.appendChild(kbActionButton("重新启用", "restore", item, "kb-activate"));
      }

      row.appendChild(head);
      row.appendChild(text);
      row.appendChild(actions);
      kbEls.list.appendChild(row);
    });
  }

  function kbFormState() {
    return [
      kbEls.clauseId ? kbEls.clauseId.value : "",
      kbEls.title ? kbEls.title.value : "",
      kbEls.status ? kbEls.status.value : "",
      kbEls.content ? kbEls.content.value : ""
    ].join("\u0001");
  }

  function kbTakeSnapshot() { kb.savedSnapshot = kbFormState(); }

  /* W14 ISS-38:切换条款 / 新建前,有未保存修改先确认,避免白写。 */
  function kbConfirmDiscard() {
    if (!kb.savedSnapshot) return true;
    if (kbFormState() === kb.savedSnapshot) return true;
    return window.confirm("当前条款还有未保存的修改，确定放弃吗？");
  }

  function kbResetForm(force) {
    if (!force && !kbConfirmDiscard()) return;
    kb.editingId = null;
    if (kbEls.editorTitle) kbEls.editorTitle.textContent = "新建条款 · 新建模式";
    if (kbEls.form) kbEls.form.reset();
    if (kbEls.status) kbEls.status.value = "draft";
    if (kbEls.clauseId) kbEls.clauseId.readOnly = false;
    if (kbEls.hint) kbEls.hint.textContent = "";
    kbTakeSnapshot();
    if (kbEls.content) kbEls.content.focus();
  }

  function kbEditItem(item) {
    if (!kbConfirmDiscard()) return;
    kb.editingId = item.id;
    if (kbEls.editorTitle) kbEls.editorTitle.textContent = "编辑条款 · 编辑模式";
    if (kbEls.clauseId) {
      kbEls.clauseId.value = item.clause_id || "";
      kbEls.clauseId.readOnly = true;
    }
    if (kbEls.title) kbEls.title.value = item.title || "";
    if (kbEls.status) kbEls.status.value = item.status || "draft";
    if (kbEls.content) kbEls.content.value = item.content || "";
    if (kbEls.hint) {
      kbEls.hint.textContent = "当前 v" + item.version +
        "；修改标题或正文后保存会自动升版，条款号保持不变。";
    }
    kbTakeSnapshot();
  }

  async function kbRefreshStats() {
    try {
      var stats = await api("/api/kb/stats");
      kbRenderStats(stats);
    } catch (err) { /* 徽标非关键,失败静默 */ }
  }

  async function kbLoadAll() {
    try {
      var data = await api("/api/kb/items");
      kb.items = data.items || [];
      kb.loaded = true;
      kbRenderList();
      await kbRefreshStats();
    } catch (err) {
      toast("知识库加载失败:" + errorMessage(err), "error");
    }
  }

  async function kbSaveForm(ev) {
    ev.preventDefault();
    if (!kbEls.content || !kbEls.content.value.trim()) {
      toast("正文不能为空", "error");
      return;
    }
    var body = {
      clause_id: (kbEls.clauseId.value || "").trim(),
      title: (kbEls.title.value || "").trim(),
      content: kbEls.content.value,
      status: kbEls.status.value
    };
    if (!body.clause_id) { toast("条款号不能为空", "error"); return; }
    if (!/^(RP|LP)-\d+$/i.test(body.clause_id)) {
      if (kbEls.hint) {
        kbEls.hint.textContent = "条款号格式：RP-数字（退货）或 LP-数字（物流），例如 RP-20。";
      }
      toast("条款号格式不正确", "error");
      return;
    }
    try {
      if (kb.editingId) {
        var path = "/api/kb/items/" + encodeURIComponent(kb.editingId);
        await api(path, {
          method: "PATCH",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(body)
        });
        toast("已保存，买家端已同步", "success");
      } else {
        await postJSON("/api/kb/items", body);
        toast("已创建为草稿，启用后参与智能回复", "success");
      }
      kbResetForm(true);
      await kbLoadAll();
    } catch (err) {
      toast("保存失败:" + errorMessage(err), "error");
    }
  }

  async function kbChangeStatus(item, action) {
    var path = "/api/kb/items/" + encodeURIComponent(item.id);
    try {
      if (action === "retire") {
        await api(path, { method: "DELETE" });
        toast((item.clause_id || "") + " 已停用，买家端立即不再命中", "success");
      } else if (action === "restore") {
        await api(path + "/restore", { method: "POST" });
        toast((item.clause_id || "") + " 已重新启用", "success");
      } else if (action === "activate") {
        await api(path, {
          method: "PATCH",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ status: "active" })
        });
        toast((item.clause_id || "") + " 已启用", "success");
      }
      await kbLoadAll();
    } catch (err) {
      toast("状态切换失败:" + errorMessage(err), "error");
    }
  }

  async function kbReloadIndex() {
    if (kbEls.reloadBtn) kbEls.reloadBtn.disabled = true;
    try {
      var data = await postJSON("/api/kb/reload", {});
      toast("已重新发布，生效条款 " + data.active_clauses + " 条，买家端立即生效", "success");
      kb.loaded = false;
      await kbLoadAll();
    } catch (err) {
      toast("发布失败:" + errorMessage(err), "error");
    } finally {
      if (kbEls.reloadBtn) kbEls.reloadBtn.disabled = false;
    }
  }

  async function kbUploadFile(file) {
    if (!file) return;
    try {
      var text = await file.text();
      var data = await postJSON("/api/kb/upload", { filename: file.name, content: text });
      toast(
        "已导入：新增 " + (data.created_count || 0) + " 条草稿、更新 " +
        (data.updated_count || 0) + " 条，启用后参与智能回复",
        "success"
      );
      await kbLoadAll();
    } catch (err) {
      toast("上传失败:" + errorMessage(err), "error");
    }
  }

  if (kbEls.toggle) kbEls.toggle.addEventListener("click", kbShow);
  if (kbEls.back) kbEls.back.addEventListener("click", kbHide);
  if (kbEls.newBtn) kbEls.newBtn.addEventListener("click", kbResetForm);
  if (kbEls.cancel) kbEls.cancel.addEventListener("click", kbResetForm);
  if (kbEls.form) kbEls.form.addEventListener("submit", kbSaveForm);
  if (kbEls.reloadBtn) kbEls.reloadBtn.addEventListener("click", kbReloadIndex);
  if (kbEls.upload) {
    kbEls.upload.addEventListener("change", function () {
      var file = kbEls.upload.files && kbEls.upload.files[0];
      kbUploadFile(file);
      kbEls.upload.value = "";
    });
  }
  if (kbEls.search) {
    kbEls.search.addEventListener("input", function () {
      kbFilter.q = kbEls.search.value || "";
      kbRenderList();
    });
  }
  if (kbEls.filters) {
    kbEls.filters.addEventListener("click", function (ev) {
      var chip = ev.target.closest ? ev.target.closest(".filter-chip") : null;
      if (!chip) return;
      var chips = kbEls.filters.querySelectorAll(".filter-chip");
      for (var i = 0; i < chips.length; i += 1) chips[i].classList.remove("active");
      chip.classList.add("active");
      kbFilter.filter = chip.getAttribute("data-kb-filter") || "all";
      kbRenderList();
    });
  }
  if (kbEls.list) {
    kbEls.list.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-kb-action]");
      if (!btn) return;
      var id = btn.getAttribute("data-kb-id");
      var item = null;
      for (var i = 0; i < kb.items.length; i += 1) {
        if (kb.items[i].id === id) { item = kb.items[i]; break; }
      }
      if (!item) return;
      var action = btn.getAttribute("data-kb-action");
      if (action === "edit") kbEditItem(item);
      else kbChangeStatus(item, action);
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
