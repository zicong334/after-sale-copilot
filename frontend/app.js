/* 售后 Copilot · 聊天页(原生 JS,零第三方依赖 零构建)
 * SSE 协议沿用 /api/c/chat/stream;W5 新增:
 * - append_ack:转人工后的轻量系统行(不是 AI 气泡)
 * - handoff_notice:全流程只弹一次 3.5s toast
 * - handed_off 后每 3s 轮询会话 summary,把 author_type=agent 渲染成「人工」绿气泡
 * - W5-FIX R1:会话状态**只**由本 session 的工单驱动(`/api/c/handoff/summary?session_id=`),
 *   不再按客户取最近工单;handed_off 且工单未关闭时输入框保持可用(留言追加到工单),
 *   只有本 session 的工单 closed 才禁用输入 + 提示「工单已关闭」
 * - usage.estimated===true:小字「演示估算,非真实账单」
 */
(function () {
  "use strict";

  var TOKEN_KEY = "sc_token";
  var USER_KEY = "sc_user";
  var POLL_MS = 3000;
  var TOAST_MS = 3500;
  var WS_RETRY_STEPS = [1000, 2000, 5000]; // 断线退避:1s → 2s → 5s(之后保持 5s)
  var WS_PING_MS = 25000;

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
  if (!TOKEN || !ME || ME.role !== "buyer") {
    gotoLogin();
    return;
  }
  var MERCHANT_ID = ME.merchant_id || "";
  var CUSTOMER_ID = ME.customer_id || "";
  // 会话 id 按买家分开存:同一浏览器切换买家账号不会串会话
  var SESSION_KEY = "sc_session_" + (CUSTOMER_ID || ME.id || ME.username || "buyer");

  /* W8 收尾:启动时回读 /auth/me 校验登录态(token 可能已过期/被吊销)。
   * - 401 → apiFetch 统一 gotoLogin() 清登录态并回 login.html
   * - 服务端身份与本地缓存不一致 → 覆盖 localStorage 后 reload,重新派生 merchant/customer/session
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

  var els = {
    stream: document.getElementById("stream"),
    empty: document.getElementById("emptyState"),
    examples: document.getElementById("examples"),
    form: document.getElementById("composer"),
    input: document.getElementById("text"),
    send: document.getElementById("send"),
    status: document.getElementById("statusBadge"),
    trace: document.getElementById("trace"),
    toastWrap: document.getElementById("toastWrap"),
    closedHint: document.getElementById("closedHint"),
    handoffBar: document.getElementById("handoffBar"),
    connBadge: document.getElementById("connBadge"),
    logoutBtn: document.getElementById("logoutBtn")
  };

  var state = {
    sessionId: null,
    busy: false,
    handedOff: false,
    closed: false,
    ticketId: null,
    ticketStatus: null,
    handoffToastShown: false,
    handoffLineShown: false,
    closedNotified: false,
    pollTimer: null,
    pollFails: 0,
    seenHuman: Object.create(null),
    lastText: "",
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

  function fmtTime(iso) {
    if (!iso) return nowTime();
    var d = new Date(iso);
    if (isNaN(d.getTime())) return nowTime();
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function scrollBottom() {
    els.stream.scrollTop = els.stream.scrollHeight;
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
    } else if (state.busy) {
      els.status.setAttribute("data-state", "active");
      els.status.textContent = "AI 回复中…";
    } else {
      els.status.setAttribute("data-state", "active");
      els.status.textContent = "AI 接待中";
    }
  }

  function setTrace(traceId) {
    if (traceId) els.trace.textContent = "trace: " + traceId;
  }

  function hideEmpty() {
    if (els.empty) els.empty.classList.add("hidden");
  }

  /* -------------------------------- 消息渲染 -------------------------------- */

  function addBubble(role, text, opts) {
    opts = opts || {};
    var wrap = el("div", "msg " + role);
    var meta = el("div", "msg-meta");
    var name = opts.roleName ||
      (role === "user" ? "我" : role === "human" ? "人工客服" : "AI 客服");
    if (role === "human") meta.appendChild(el("span", "human-badge", "人工"));
    meta.appendChild(el("span", "role-name", name));
    meta.appendChild(el("span", "msg-time", opts.time || nowTime()));
    var bubble = el("div", "bubble", text || "");
    wrap.appendChild(meta);
    wrap.appendChild(bubble);
    els.stream.appendChild(wrap);
    scrollBottom();
    return { wrap: wrap, bubble: bubble, meta: meta };
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

  function renderCitations(wrap, citations) {
    if (!citations.length) return;
    var bar = el("div", "citations");
    var panel = el("div", "clause-card");
    var refs = [];
    citations.forEach(function (cite, idx) {
      var n = cite.index || idx + 1;
      var ref = el("button", "citation-ref", "[" + n + "]");
      ref.type = "button";
      ref.title = "查看条款 " + (cite.clause_id || "");
      ref.setAttribute("aria-label", "展开第 " + n + " 条政策条款");
      ref.addEventListener("click", function () {
        var isSame = ref.classList.contains("active");
        refs.forEach(function (r) { r.classList.remove("active"); });
        panel.classList.remove("open");
        if (isSame) return;
        ref.classList.add("active");
        panel.textContent = "";
        var body = el("div", "clause-text");
        body.appendChild(el("mark", "", cite.snippet || "(条款原文缺失)"));
        panel.appendChild(body);
        var version = cite.version ? " · 版本 " + cite.version : "";
        panel.appendChild(el("div", "clause-meta",
          "条款 " + (cite.clause_id || "—") + version + " · 点击 [n] 可收起"));
        panel.classList.add("open");
        scrollBottom();
      });
      refs.push(ref);
      bar.appendChild(ref);
    });
    wrap.appendChild(bar);
    wrap.appendChild(panel);
    scrollBottom();
  }

  /* ------------------------------ 确认卡(四要素) --------------------------- */

  function renderConfirmCard(card) {
    var wrap = el("div", "confirm-card");
    var head = el("div", "confirm-head");
    head.appendChild(el("span", "confirm-mark", "!"));
    head.appendChild(el("span", "", "需要你确认后才能执行"));
    wrap.appendChild(head);
    wrap.appendChild(el("div", "confirm-sub",
      "确认后才会有真实写操作;超时未确认视为拒绝,草稿会保留。"));

    var table = el("div", "card-table");
    [
      ["做什么", card.what],
      ["影响面", card.impact],
      ["不可逆", card.irreversibility],
      ["替代方案", card.alternatives]
    ].forEach(function (pair) {
      table.appendChild(el("div", "k", pair[0]));
      table.appendChild(el("div", "v", pair[1] || "—"));
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
    var okBtn = el("button", "btn btn-primary", "确认执行");
    var noBtn = el("button", "btn btn-danger-ghost", "取消");
    okBtn.type = "button";
    noBtn.type = "button";
    var countdown = el("div", "countdown", "");
    actions.appendChild(okBtn);
    actions.appendChild(noBtn);
    actions.appendChild(countdown);
    wrap.appendChild(actions);

    els.stream.appendChild(wrap);
    scrollBottom();

    // W8:登记确认卡,收到 WS confirm_status(含其他标签页确认)时同步最终态
    state.confirmCards[card.confirm_token] = {
      okBtn: okBtn,
      noBtn: noBtn,
      countdown: countdown,
      stopped: false // 终态(已提交 / WS 同步)后停掉倒计时,避免覆盖最终文案
    };

    function tick() {
      var entry = state.confirmCards[card.confirm_token];
      if (entry && entry.stopped) return;
      if (left <= 0) {
        countdown.textContent = "已超时 = 自动拒绝(草稿保留)";
        bar.style.width = "0%";
        okBtn.disabled = true;
        noBtn.disabled = true;
        return;
      }
      var m = Math.floor(left / 60);
      var s = left % 60;
      countdown.textContent = "剩余 " + m + ":" + pad(s) + " · 超时视为拒绝";
      bar.style.width = Math.max(0, (left / total) * 100).toFixed(2) + "%";
      left -= 1;
      window.setTimeout(tick, 1000);
    }
    tick();

    function decide(kind) {
      var entry = state.confirmCards[card.confirm_token];
      if (entry) entry.stopped = true;
      okBtn.disabled = true;
      noBtn.disabled = true;
      countdown.textContent = "已提交…";
      apiFetch("/api/c/confirm/" + encodeURIComponent(card.confirm_token) + "/" + kind, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          thread_id: card.thread_id || state.sessionId
        })
      })
        .then(function (resp) {
          return resp.json().then(function (json) {
            return { status: resp.status, json: json };
          });
        })
        .then(function (out) {
          countdown.textContent = "";
          if (out.status >= 400 || out.json.ok === false) {
            countdown.textContent = "失败:" +
              ((out.json.error && out.json.error.message) || out.status);
            okBtn.disabled = false;
            noBtn.disabled = false;
            return;
          }
          renderDecision(out.json);
        })
        .catch(function (err) {
          countdown.textContent = "失败:" + err.message;
          okBtn.disabled = false;
          noBtn.disabled = false;
        });
    }

    okBtn.addEventListener("click", function () { decide("confirm"); });
    noBtn.addEventListener("click", function () { decide("reject"); });
  }

  function renderDecision(result) {
    var receipt = result.receipt;
    var box = el("div", "receipt");
    if (result.decision === "confirmed") {
      var badge = el("span", "badge",
        receipt && receipt.status === "unknown" ? "未执行" : "回执 verified");
      box.appendChild(badge);
      var id = (receipt && (receipt.refund_request_id || receipt.ticket_id)) || "—";
      box.appendChild(el("span", "row", "业务单号:" + id));
      if (receipt && receipt.amount_cents !== undefined && receipt.amount_cents !== null) {
        box.appendChild(el("span", "row",
          "金额:" + (receipt.amount_cents / 100).toFixed(2) + " 元(" +
          receipt.amount_cents + " 分)"));
      }
      if (receipt) {
        box.appendChild(el("span", "row",
          "状态:" + receipt.status + " · 时间:" + (receipt.occurred_at_utc || "")));
        box.appendChild(el("span", "row",
          "verified:" + receipt.verified + " · trace:" + (receipt.trace_id || "")));
      }
    } else if (result.decision === "rejected") {
      box.classList.add("rejected");
      box.appendChild(el("span", "badge", result.timeout ? "超时 = 拒绝" : "已拒绝"));
      box.appendChild(el("span", "row", "未执行任何写操作;草稿已保留在确认卡。"));
    } else {
      box.classList.add("rejected");
      box.appendChild(el("span", "badge", "未执行"));
      box.appendChild(el("span", "row", "执行失败,详见回执 error 字段。"));
    }
    if (result.reply) box.appendChild(el("span", "row", result.reply));
    els.stream.appendChild(box);
    scrollBottom();
  }

  /* --------------------------------- 用量 -------------------------------- */

  function renderUsage(wrap, usage) {
    if (!usage) return;
    var parts = [];
    if (usage.tokens_in !== undefined && usage.tokens_in !== null) {
      parts.push("token " + usage.tokens_in + " in / " + usage.tokens_out + " out");
    }
    if (usage.cost_usd !== undefined && usage.cost_usd !== null) {
      parts.push("$" + Number(usage.cost_usd).toFixed(6));
    }
    if (usage.latency_ms !== undefined && usage.latency_ms !== null) {
      parts.push(usage.latency_ms + " ms");
    }
    if (!parts.length) return;
    var line = el("div", "usage", parts.join(" · "));
    if (usage.estimated === true) {
      line.appendChild(document.createTextNode(" · "));
      line.appendChild(el("span", "estimate-note", "演示估算,非真实账单"));
    }
    wrap.appendChild(line);
    scrollBottom();
  }

  /* ---------------------------- 转人工 / 轮询闭环 --------------------------- */

  function markHandedOff(withToast) {
    var first = !state.handedOff;
    state.handedOff = true;
    renderStatus();
    applyHandoffUi();
    if (withToast && !state.handoffToastShown) {
      state.handoffToastShown = true;
      toast("已转人工:AI 已停止自动回复,坐席会尽快跟进。", "warn");
    }
    if (first && !state.handoffLineShown) {
      state.handoffLineShown = true;
      addSysLine("已转人工,AI 已停止自动回复,坐席会尽快跟进。");
    }
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
    els.input.placeholder = "留言将追加到人工工单…(Enter 发送,Shift+Enter 换行)";
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
      var resp = await apiFetch(
        "/api/c/handoff/summary?session_id=" + encodeURIComponent(state.sessionId),
        { headers: { Accept: "application/json" } }
      );
      if (!resp.ok) {
        state.pollFails += 1;
        if (state.pollFails === 6) {
          addSysLine("工单状态同步暂时不可用,正在重试…");
        }
        return;
      }
      state.pollFails = 0;
      var data = await resp.json();
      state.ticketId = (data.ticket && data.ticket.ticket_id) || null;
      state.ticketStatus = data.ticket_status || null;
      if (data.session_status === "handed_off" || data.session_status === "closed") {
        markHandedOff(false);
      }
      var list = Array.isArray(data.ticket_messages) ? data.ticket_messages : [];
      list.forEach(function (msg) {
        if (!msg || msg.author_type !== "agent") return;
        // 内部备注只给坐席看,买家页不渲染(后端 summary 也带 is_internal 标记)
        if (msg.is_internal === true) return;
        var key = msg.id ||
          (String(msg.created_at_utc || "") + "|" + String(msg.content || ""));
        if (state.seenHuman[key]) return;
        state.seenHuman[key] = true;
        addBubble("human", msg.content || "", { time: fmtTime(msg.created_at_utc) });
      });
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
   * 主通道:GET /ws/buyer/{session_id}?token=(session_id 来自聊天返回/本地保存)。
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
      encodeURIComponent(state.sessionId) + "?token=" + encodeURIComponent(readToken());
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
    connBadge("offline", "实时已断开 · " + Math.round(wait / 1000) + "s 后重连");
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
    connBadge("connecting", "实时连接中…");
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
      // 连接建立后仍等服务端 ready 帧(先鉴权再订阅频道)
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
        connBadge("offline", "实时已断开");
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
        connBadge("online", "实时已连接");
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
    card.stopped = true;
    var confirmed = msg.status === "confirmed";
    card.okBtn.disabled = true;
    card.noBtn.disabled = true;
    card.countdown.textContent = confirmed ? "已确认(实时同步)" : "已拒绝(实时同步)";
    toast(confirmed ? "确认卡已确认" : "确认卡已拒绝", confirmed ? "success" : "warn");
  }

  /* --------------------------------- 输入区 ------------------------------- */

  function autoResize() {
    els.input.style.height = "auto";
    var next = Math.min(els.input.scrollHeight, 168);
    els.input.style.height = Math.max(next, 40) + "px";
  }

  els.input.addEventListener("input", autoResize);

  els.input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      els.form.requestSubmit ? els.form.requestSubmit() : els.form.dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  if (els.examples) {
    els.examples.addEventListener("click", function (ev) {
      var chip = ev.target.closest ? ev.target.closest(".example-chip") : null;
      if (!chip) return;
      els.input.value = chip.getAttribute("data-text") || chip.textContent || "";
      autoResize();
      els.input.focus();
    });
  }

  els.form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = els.input.value.trim();
    if (!text || state.busy || state.closed) return;
    els.input.value = "";
    autoResize();
    sendTurn(text);
  });

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
      case "message":
        if (ui.ensureAi) {
          var ai = ui.ensureAi();
          meta.reply += data.delta || "";
          ai.bubble.textContent = meta.reply;
          scrollBottom();
        }
        break;
      case "citation":
        meta.citations.push(data);
        break;
      case "confirm_request":
        removeNode(ui.loading);
        ui.loading = null;
        renderConfirmCard(data);
        break;
      case "handoff_notice":
        markHandedOff(true);
        break;
      case "append_ack":
        removeNode(ui.loading);
        ui.loading = null;
        meta.acked = true;
        if (data.ack) addSysLine(data.ack);
        if (data.session_status && data.session_status !== "active") markHandedOff(false);
        break;
      case "usage":
        meta.usage = data;
        break;
      case "done":
        meta.done = data;
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
          addSysLine(data.ack);
        }
        if (data.ai_replied === false && !meta.reply) {
          removeNode(ui.loading);
          ui.loading = null;
        }
        break;
      default:
        break;
    }
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

    var ui = { loading: showLoading(), ensureAi: null };
    var meta = { reply: "", citations: [], done: null, usage: null, acked: false };
    var ai = null;

    ui.ensureAi = function () {
      if (!ai) {
        removeNode(ui.loading);
        ui.loading = null;
        ai = addBubble("ai", "");
      }
      return ai;
    };

    var body = {
      merchant_id: MERCHANT_ID,
      customer_id: CUSTOMER_ID,
      text: text
    };
    if (state.sessionId) body.session_id = state.sessionId;

    try {
      var resp = await apiFetch("/api/c/chat/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream"
        },
        body: JSON.stringify(body)
      });
      if (!resp.ok) {
        var detail = await resp.text();
        throw new Error("HTTP " + resp.status + " " + detail.slice(0, 240));
      }
      var reader = resp.body.getReader();
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

      if (meta.citations.length && ai) renderCitations(ai.wrap, meta.citations);
      if (meta.usage && ai) renderUsage(ai.wrap, meta.usage);
      if (ai && !meta.reply.trim()) ai.bubble.textContent = "(本轮无自动回复)";
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

  /* --------------------------------- 启动 -------------------------------- */

  renderStatus();
  autoResize();
  els.input.focus();

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
    if (!saved) return;
    state.sessionId = saved;
    startPolling();
    connectWs();
  })();
})();
