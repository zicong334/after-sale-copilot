/* 售后 Copilot · 聊天页(原生 JS,零第三方依赖 零构建)
 * SSE 协议沿用 /api/c/chat/stream;W5 新增:
 * - append_ack:转人工后的轻量系统行(不是 AI 气泡)
 * - handoff_notice:全流程只弹一次 3.5s toast
 * - handed_off 后每 3s 轮询会话 summary,把 author_type=agent 渲染成「人工」绿气泡
 * - ticket_closed:禁用输入 + 提示「工单已关闭」
 * - usage.estimated===true:小字「演示估算,非真实账单」
 */
(function () {
  "use strict";

  var params = new URLSearchParams(location.search);
  var MERCHANT_ID = params.get("merchant_id") || "seed-merchant-1";
  var CUSTOMER_ID = params.get("customer_id") || "seed-cust-009";
  var POLL_MS = 3000;
  var TOAST_MS = 3500;

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
    closedHint: document.getElementById("closedHint")
  };

  var state = {
    sessionId: null,
    busy: false,
    handedOff: false,
    closed: false,
    handoffToastShown: false,
    handoffLineShown: false,
    closedNotified: false,
    pollTimer: null,
    pollFails: 0,
    seenHuman: Object.create(null),
    lastText: ""
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

    function tick() {
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
      okBtn.disabled = true;
      noBtn.disabled = true;
      countdown.textContent = "已提交…";
      fetch("/api/c/confirm/" + encodeURIComponent(card.confirm_token) + "/" + kind, {
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

  function startPolling() {
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
      var resp = await fetch(
        "/api/c/handoff/sessions/" + encodeURIComponent(state.sessionId) + "/summary",
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
      if (data.ticket_closed === true) onTicketClosed();
    } catch (err) {
      state.pollFails += 1;
    }
  }

  function onTicketClosed() {
    if (state.closed) return;
    state.closed = true;
    stopPolling();
    renderStatus();
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
        if (data.session_id) state.sessionId = data.session_id;
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
      var resp = await fetch("/api/c/chat/stream", {
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
      els.send.disabled = state.closed;
      els.input.focus();
    }
  }

  /* --------------------------------- 启动 -------------------------------- */

  renderStatus();
  autoResize();
  els.input.focus();
})();
