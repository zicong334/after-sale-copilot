/* W15 共享 UI 组件(买家页 / 坐席页 共用同一份实现)
 * 目标(任务书 W15 U1/U2):
 * - 售后进度 stepper:买家回执卡与坐席工单详情「处理进度」两处视图必须完全一致,
 *   所以实现只写这一份,两个页面都调用它,避免两套样式/判定漂移;
 * - 金额药丸:¥ + 大号数字,主色底白字,圆角沿用令牌。
 *
 * 约定:
 * - 纯原生实现,无 CDN、无构建、无第三方依赖;
 * - 只做渲染,不做任何网络调用(网络统一在 /api.js);
 * - 页面若未加载本文件(/ui.js 404),调用方自行降级,不影响主链路。
 */
(function (global) {
  "use strict";

  /* 四个节点:申请 → 审核 → 退款 / 驳回。 */
  var STEPS = [
    { key: "apply", label: "提交申请" },
    { key: "review", label: "平台审核" },
    { key: "refund", label: "退款到账" },
    { key: "reject", label: "申请驳回" }
  ];

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  /* 退款单状态 → 当前节点下标(0 起)。
   * 后端状态为中文枚举:申请 / 审核 / 退货中 / 待验货 / 退款 / 驳回。 */
  function refundStage(status) {
    var s = String(status || "");
    if (s === "退款") return 2;
    if (s === "驳回") return 3;
    if (s === "审核" || s === "退货中" || s === "待验货") return 1;
    return 0;
  }

  /* UTC 时间戳 → 本地 HH:MM(空值/非法值返回空串)。 */
  function fmtClock(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function refundStepTime(index, stage, createdAt, decidedAt) {
    if (index === 0) return fmtClock(createdAt);
    if (index === 1 && stage >= 1) return fmtClock(decidedAt);
    if (index === 2 && stage === 2) return fmtClock(decidedAt);
    if (index === 3 && stage === 3) return fmtClock(decidedAt);
    return "";
  }

  /* 渲染进度条;没有状态(没有退款关联)时返回 null,由调用方决定不渲染。 */
  function renderRefundStepper(status, createdAt, decidedAt) {
    if (!status) return null;
    var stage = refundStage(status);
    var wrap = el("div", "stepper");
    wrap.setAttribute("data-refund-status", String(status));
    STEPS.forEach(function (step, index) {
      var node = el("div", "stepper-node");
      var mark = index < stage ? "done" : (index === stage ? "current" : "future");
      node.classList.add("step-" + mark);
      node.appendChild(el("span", "step-dot"));
      var line = el("span", "step-line");
      line.appendChild(el("span", "step-title", step.label));
      var time = refundStepTime(index, stage, createdAt, decidedAt);
      if (time) line.appendChild(el("span", "step-time", time));
      node.appendChild(line);
      wrap.appendChild(node);
    });
    return wrap;
  }

  /* 金额药丸:入参是「元」。 */
  function pricePill(yuan) {
    var pill = el("span", "price-pill");
    pill.appendChild(el("span", "price-cur", "¥"));
    pill.appendChild(el("span", "price-num", Number(yuan || 0).toFixed(2)));
    return pill;
  }

  /* 金额药丸:入参是「分」(后端金额一律用分)。 */
  function pricePillCents(cents) {
    return pricePill(Number(cents || 0) / 100);
  }

  global.SCUi = {
    STEPS: STEPS,
    refundStage: refundStage,
    fmtClock: fmtClock,
    renderRefundStepper: renderRefundStepper,
    pricePill: pricePill,
    pricePillCents: pricePillCents
  };
})(typeof window !== "undefined" ? window : globalThis);
