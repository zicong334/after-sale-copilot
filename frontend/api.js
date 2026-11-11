/* W15 U5:共享请求封装(买家页 / 坐席页 / 登录页 三页唯一持有底层网络调用的文件)
 * 目标(任务书 W15 U5「通信收拢终章」):
 * - 统一封装:超时兜底 + 统一错误信封 + 写接口防重放头,三页行为一致;
 * - 统一信封:{ ok, status, data, error, resp }
 *     ok      → HTTP 成功且业务 ok 不为 false
 *     status  → HTTP 状态码(网络异常为 0)
 *     data    → 解析后的 JSON(原始模式为 null)
 *     error   → { code, message }(成功时为 null)
 *     resp    → 原始 Response(流式 SSE 用)
 * - 写方法(POST/PUT/PATCH/DELETE)自动注入 `X-Timestamp`(Unix 秒)+ `X-Nonce`(16 字节 hex);
 * - 401 → 交给页面注入的 onUnauthorized(清登录态 + 回登录页);
 * - 超时:默认 15s,AbortController 中断;`timeoutMs: 0` 表示不超时(SSE 长连接用)。
 *
 * 纯原生实现:无 CDN、无构建、无第三方依赖。
 */
(function (global) {
  "use strict";

  var WRITE_METHODS = { POST: true, PUT: true, PATCH: true, DELETE: true };
  var DEFAULT_TIMEOUT_MS = 15000;

  /* 16 字节随机 hex(crypto.getRandomValues;旧浏览器退回 Math.random 兜底) */
  function randomHex(byteLen) {
    var buf = new Uint8Array(byteLen);
    if (global.crypto && global.crypto.getRandomValues) {
      global.crypto.getRandomValues(buf);
    } else {
      for (var i = 0; i < byteLen; i += 1) buf[i] = Math.floor(Math.random() * 256);
    }
    var out = "";
    for (var j = 0; j < buf.length; j += 1) out += ("0" + buf[j].toString(16)).slice(-2);
    return out;
  }

  function pickError(json, status) {
    var code = "http_" + status;
    var message = "HTTP " + status;
    if (json) {
      if (json.error && (json.error.message || json.error.code)) {
        message = json.error.message || message;
        code = json.error.code || code;
      } else if (json.detail) {
        message = String(json.detail);
      } else if (json.message) {
        message = String(json.message);
      }
    }
    return { code: code, message: message };
  }

  /**
   * 构造一个请求函数。
   * @param {{tokenKey?:string,onUnauthorized?:Function,timeoutMs?:number,fetchImpl?:Function}} options
   * @returns {function(string, object=): Promise<object>} 信封对象
   */
  function createApiFetch(options) {
    options = options || {};
    var tokenKey = options.tokenKey || "";
    var onUnauthorized = typeof options.onUnauthorized === "function" ? options.onUnauthorized : function () {};
    var defaultTimeout = typeof options.timeoutMs === "number" ? options.timeoutMs : DEFAULT_TIMEOUT_MS;
    var fetchImpl = options.fetchImpl ||
      (typeof global.fetch === "function" ? global.fetch.bind(global) : null);

    function readToken() {
      if (!tokenKey) return "";
      try { return global.localStorage.getItem(tokenKey) || ""; } catch (err) { return ""; }
    }

    return async function apiFetch(path, opts) {
      opts = opts || {};
      if (!fetchImpl) {
        return {
          ok: false, status: 0, data: null, resp: null,
          error: { code: "no_transport", message: "网络层不可用" }
        };
      }
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
      Object.keys(opts).forEach(function (key) {
        if (key === "raw" || key === "timeoutMs") return;
        init[key] = opts[key];
      });
      init.method = method;
      init.headers = headers;

      var timeoutMs = typeof opts.timeoutMs === "number" ? opts.timeoutMs : defaultTimeout;
      var controller = null;
      var timer = null;
      if (timeoutMs > 0 && typeof global.AbortController === "function") {
        controller = new global.AbortController();
        init.signal = controller.signal;
        timer = global.setTimeout(function () {
          try { controller.abort(); } catch (err) { /* 忽略 */ }
        }, timeoutMs);
      }

      var resp;
      try {
        resp = await fetchImpl(path, init);
      } catch (err) {
        if (timer) global.clearTimeout(timer);
        var timedOut = err && (err.name === "AbortError" || err.code === 20);
        return {
          ok: false, status: 0, data: null, resp: null,
          error: {
            code: timedOut ? "timeout" : "network_error",
            message: timedOut ? "请求超时,请稍后重试" : "网络异常,请稍后重试"
          }
        };
      }
      if (timer) global.clearTimeout(timer);
      if (resp.status === 401) {
        try { onUnauthorized(); } catch (err) { /* 忽略 */ }
      }

      /* raw 模式:不读 body,交还原生 Response(SSE 流式用)。 */
      if (opts.raw) {
        return {
          ok: resp.ok, status: resp.status, data: null, resp: resp,
          error: resp.ok ? null : { code: "http_" + resp.status, message: "HTTP " + resp.status }
        };
      }

      var text = "";
      try { text = await resp.text(); } catch (err) { text = ""; }
      var json = null;
      if (text) {
        try { json = JSON.parse(text); } catch (err) { json = null; }
      }
      var envelope = { ok: resp.ok, status: resp.status, data: json, resp: resp, error: null };
      if (!resp.ok) {
        envelope.error = pickError(json, resp.status);
      } else if (json && json.ok === false) {
        envelope.ok = false;
        envelope.error = pickError(json, resp.status);
      }
      return envelope;
    };
  }

  global.SCApi = {
    createApiFetch: createApiFetch,
    randomHex: randomHex,
    WRITE_METHODS: WRITE_METHODS
  };
})(typeof window !== "undefined" ? window : globalThis);
