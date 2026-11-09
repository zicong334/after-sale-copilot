/* 售后 Copilot · 客服工作台(原生 JS,零第三方依赖 零构建)
 * 对接契约 §二 /api/b/*:
 *   GET  /api/b/services
 *   GET  /api/b/tickets?merchant_id=&status=
 *   GET  /api/b/tickets/{id}
 *   POST /api/b/tickets/{id}/claim      {service_id}
 *   POST /api/b/tickets/{id}/messages   {service_id, content, is_internal?}
 *   POST /api/b/tickets/{id}/close      {service_id?}
 * 409 时 toast 后端 message(含「已被他人领取」)。
 */
(function () {
  "use strict";

  var params = new URLSearchParams(location.search);
  var MERCHANT_ID = params.get("merchant_id") || "seed-merchant-1";

  var FALLBACK_SERVICES = [
    { service_id: "seed-agent-1", display_name: "客服 小暖" },
    { service_id: "seed-agent-2", display_name: "客服 小满" }
  ];

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
    agentSelect: document.getElementById("agentSelect"),
    queueList: document.getElementById("queueList"),
    queueState: document.getElementById("queueState"),
    queueCount: document.getElementById("queueCount"),
    queueFilters: document.getElementById("queueFilters"),
    refreshBtn: document.getElementById("refreshBtn"),
    detailEmpty: document.getElementById("detailEmpty"),
    detailBody: document.getElementById("detailBody"),
    toastWrap: document.getElementById("toastWrap")
  };

  var state = {
    agentId: "seed-agent-1",
    statusFilter: "",
    tickets: [],
    selectedId: null,
    selectedTicket: null,
    loadingQueue: false,
    detailBusy: false,
    timer: null
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
      resp = await fetch(path, options || {});
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

  function currentAgent() {
    return els.agentSelect.value || state.agentId;
  }

  function fillServices(services) {
    els.agentSelect.textContent = "";
    services.forEach(function (svc) {
      var opt = document.createElement("option");
      opt.value = svc.service_id;
      opt.textContent = svc.display_name + "(" + svc.service_id + ")";
      els.agentSelect.appendChild(opt);
    });
    try {
      var saved = window.localStorage.getItem("w5.agent");
      if (saved && services.some(function (s) { return s.service_id === saved; })) {
        els.agentSelect.value = saved;
      }
    } catch (err) { /* 隐私模式忽略 */ }
    state.agentId = currentAgent();
  }

  async function loadServices() {
    try {
      var data = await api("/api/b/services");
      var items = Array.isArray(data.items) && data.items.length
        ? data.items : FALLBACK_SERVICES;
      fillServices(items);
    } catch (err) {
      fillServices(FALLBACK_SERVICES);
      toast("坐席列表加载失败:" + errorMessage(err) + "(已用演示默认值)", "error");
    }
  }

  els.agentSelect.addEventListener("change", function () {
    state.agentId = currentAgent();
    try { window.localStorage.setItem("w5.agent", state.agentId); } catch (err) { /* ignore */ }
    if (state.selectedId) loadDetail(state.selectedId);
  });

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
    els.detailBody.appendChild(el("div", "panel-state", "正在加载工单详情…"));
    try {
      var data = await api("/api/b/tickets/" + encodeURIComponent(ticketId));
      if (state.selectedId !== ticketId) return;
      state.selectedTicket = data.ticket || null;
      renderDetail(data.ticket || {}, Array.isArray(data.messages) ? data.messages : []);
    } catch (err) {
      if (state.selectedId !== ticketId) return;
      els.detailBody.textContent = "";
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

  /* --------------------------------- 启动 --------------------------------- */

  (async function boot() {
    fillServices(FALLBACK_SERVICES);
    await loadServices();
    loadQueue();
  })();
})();
