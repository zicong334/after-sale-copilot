/* 售后 Copilot 聊天壳:原生 fetch + ReadableStream 解析 SSE(EventSource 不支持 POST)。 */
(function () {
  "use strict";

  var params = new URLSearchParams(location.search);
  var MERCHANT_ID = params.get("merchant_id") || "seed-merchant-1";
  var CUSTOMER_ID = params.get("customer_id") || "seed-cust-009";

  var els = {
    stream: document.getElementById("stream"),
    empty: document.getElementById("emptyHint"),
    form: document.getElementById("composer"),
    input: document.getElementById("text"),
    send: document.getElementById("send"),
    conn: document.getElementById("conn"),
    banner: document.getElementById("handoffBanner"),
    trace: document.getElementById("trace")
  };
  var state = { sessionId: null, busy: false, handedOff: false };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function addBubble(role, text) {
    var wrap = el("div", "msg " + role);
    var label = el("div", "role", role === "user" ? "我" : role === "system" ? "系统" : "AI 客服");
    var bubble = el("div", "bubble", text || "");
    wrap.appendChild(label);
    wrap.appendChild(bubble);
    els.stream.appendChild(wrap);
    els.stream.scrollTop = els.stream.scrollHeight;
    return { wrap: wrap, bubble: bubble };
  }

  function setBusy(busy) {
    state.busy = busy;
    els.send.disabled = busy;
    els.conn.textContent = busy ? "生成中…" : "已连接";
    els.conn.className = "conn" + (busy ? "" : " live");
  }

  /* SSE 解析(fetch + ReadableStream) */
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
    var data = frame.data;
    switch (frame.event) {
      case "message":
        meta.reply += data.delta || "";
        ui.bubble.textContent = meta.reply;
        els.stream.scrollTop = els.stream.scrollHeight;
        break;
      case "citation":
        meta.citations.push(data);
        break;
      case "confirm_request":
        renderConfirmCard(data, meta);
        break;
      case "handoff_notice":
        state.handedOff = true;
        els.banner.classList.remove("hidden");
        addBubble("system", data.message || "已转人工,AI 已停止自动回复。");
        break;
      case "usage":
        renderUsage(ui.wrap, data);
        break;
      case "done":
        meta.done = data;
        if (data.session_id) state.sessionId = data.session_id;
        if (data.trace_id) els.trace.textContent = "trace: " + data.trace_id;
        if (data.session_status && data.session_status !== "active") {
          state.handedOff = true;
          els.banner.classList.remove("hidden");
        }
        break;
      default:
        break;
    }
  }

  async function sendTurn(text) {
    if (state.busy) return;
    els.empty.classList.add("hidden");
    addBubble("user", text);
    var ui = addBubble("assistant", "");
    var meta = { reply: "", citations: [], done: null, confirmToken: null };
    setBusy(true);

    var body = {
      merchant_id: MERCHANT_ID,
      customer_id: CUSTOMER_ID,
      text: text
    };
    if (state.sessionId) body.session_id = state.sessionId;

    try {
      var resp = await fetch("/api/c/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body)
      });
      if (!resp.ok) {
        var detail = await resp.text();
        throw new Error("HTTP " + resp.status + " " + detail.slice(0, 300));
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
      renderCitations(ui.wrap, meta.citations);
      if (!meta.reply) ui.bubble.textContent = "(本轮无自动回复)";
    } catch (err) {
      ui.bubble.textContent = "请求失败:" + err.message;
      addBubble("system", "如持续失败,请检查服务是否启动(后端 /health)与网络。");
    } finally {
      setBusy(false);
    }
  }

  /* ---------------- 引用 [n] 点击高亮条款 ---------------- */

  function renderCitations(wrap, citations) {
    if (!citations.length) return;
    var bar = el("div", "citations");
    var panel = el("div", "cite-panel");
    var marks = [];
    citations.forEach(function (cite, idx) {
      var ref = el("button", "citation-ref", "[" + (cite.index || idx + 1) + "]");
      ref.type = "button";
      ref.title = "查看并高亮条款 " + (cite.clause_id || "");
      var mark = el("mark", "", cite.snippet || "");
      panel.appendChild(mark);
      marks.push(mark);
      ref.addEventListener("click", function () {
        var same = ref.classList.contains("active");
        marks.forEach(function (m) { m.style.outline = "none"; });
        bar.querySelectorAll(".citation-ref").forEach(function (b) { b.classList.remove("active"); });
        if (same) {
          panel.classList.remove("open");
          return;
        }
        ref.classList.add("active");
        panel.classList.add("open");
        mark.style.outline = "2px solid #f0b429";
      });
      bar.appendChild(ref);
    });
    wrap.appendChild(bar);
    wrap.appendChild(panel);
    els.stream.scrollTop = els.stream.scrollHeight;
  }

  /* ---------------- 确认卡四要素 + 倒计时 + 确认/拒绝 ---------------- */

  function renderConfirmCard(card, meta) {
    var wrap = el("div", "confirm-card");
    wrap.appendChild(el("h3", "", "需要你确认后才能执行"));

    var rows = [
      ["做什么", card.what],
      ["影响面", card.impact],
      ["不可逆", card.irreversibility],
      ["替代方案", card.alternatives]
    ];
    rows.forEach(function (pair) {
      var row = el("div", "card-row");
      row.appendChild(el("div", "k", pair[0]));
      row.appendChild(el("div", "v", pair[1] || "—"));
      wrap.appendChild(row);
    });

    var actions = el("div", "card-actions");
    var okBtn = el("button", "primary", "确认执行");
    var noBtn = el("button", "danger", "拒绝");
    okBtn.type = "button";
    noBtn.type = "button";
    var countdown = el("div", "countdown", "");
    actions.appendChild(okBtn);
    actions.appendChild(noBtn);
    actions.appendChild(countdown);
    wrap.appendChild(actions);
    els.stream.appendChild(wrap);
    els.stream.scrollTop = els.stream.scrollHeight;

    var left = typeof card.expires_in_seconds === "number" ? card.expires_in_seconds : 600;
    function tick() {
      if (left <= 0) {
        countdown.textContent = "已超时 = 自动拒绝(草稿保留)";
        okBtn.disabled = true;
        return;
      }
      var m = Math.floor(left / 60);
      var s = left % 60;
      countdown.textContent = "剩余 " + m + ":" + (s < 10 ? "0" + s : s) + "(超时视为拒绝)";
      left -= 1;
      setTimeout(tick, 1000);
    }
    tick();

    function decide(kind) {
      okBtn.disabled = true;
      noBtn.disabled = true;
      countdown.textContent = "已提交…";
      fetch("/api/c/confirm/" + encodeURIComponent(card.confirm_token) + "/" + kind, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ thread_id: card.thread_id || (meta.done && meta.done.session_id) || state.sessionId })
      })
        .then(function (resp) { return resp.json().then(function (json) { return { status: resp.status, json: json }; }); })
        .then(function (out) {
          countdown.textContent = "";
          if (out.status >= 400 || out.json.ok === false) {
            countdown.textContent = "失败:" + ((out.json.error && out.json.error.message) || out.status);
            okBtn.disabled = false;
            noBtn.disabled = false;
            return;
          }
          renderDecision(out.json);
        })
        .catch(function (err) { countdown.textContent = "失败:" + err.message; });
    }
    okBtn.addEventListener("click", function () { decide("confirm"); });
    noBtn.addEventListener("click", function () { decide("reject"); });
  }

  function renderDecision(result) {
    var box = el("div", "receipt");
    var receipt = result.receipt;
    if (receipt && result.decision === "confirmed") {
      var badge = el("span", "badge", receipt.status === "unknown" ? "未执行" : "回执 verified");
      box.appendChild(badge);
      var id = receipt.refund_request_id || receipt.ticket_id || "—";
      box.appendChild(el("span", "row", "业务单号:" + id));
      if (receipt.amount_cents !== undefined && receipt.amount_cents !== null) {
        box.appendChild(el("span", "row", "金额:" + (receipt.amount_cents / 100).toFixed(2) + " 元(" + receipt.amount_cents + " 分)"));
      }
      box.appendChild(el("span", "row", "状态:" + receipt.status + " · 时间:" + (receipt.occurred_at_utc || "")));
      box.appendChild(el("span", "row", "verified:" + receipt.verified + " · trace:" + (receipt.trace_id || "")));
    } else if (result.decision === "rejected") {
      box.appendChild(el("span", "badge", result.timeout ? "超时 = 拒绝" : "已拒绝"));
      box.appendChild(el("span", "row", "未执行任何写操作;草稿已保留在确认卡。"));
    } else {
      box.appendChild(el("span", "badge", "未执行"));
      box.appendChild(el("span", "row", "执行失败,详见回执 error 字段。"));
    }
    if (result.reply) box.appendChild(el("span", "row", result.reply));
    els.stream.appendChild(box);
    els.stream.scrollTop = els.stream.scrollHeight;
  }

  function renderUsage(wrap, usage) {
    if (!usage || !usage.tokens_in) return;
    var line = el("div", "usage",
      "本轮 " + usage.tokens_in + " in / " + usage.tokens_out + " out · $" +
      Number(usage.cost_usd || 0).toFixed(6) + " · " + (usage.latency_ms || 0) + " ms · ");
    line.appendChild(el("span", "est", "estimated(估算,非真实账单)"));
    wrap.appendChild(line);
  }

  els.form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = els.input.value.trim();
    if (!text) return;
    els.input.value = "";
    sendTurn(text);
  });

  setBusy(false);
})();
