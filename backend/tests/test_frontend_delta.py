"""W10-4 真流式(前端部分)的前端行为测试。

做法:运行时生成一个 Node 探针脚本(写到 ROOT/.tmp/ 下的唯一目录),脚本里造一个
最小假 DOM(document / window / localStorage / crypto / setTimeout 等),加载
`frontend/app.js`,然后按**真实的事件序列**驱动 `handleFrame`,把观察到的气泡文本
以 JSON 打印出来,Python 侧按契约断言。

覆盖的契约(见任务书 W10-4):
- `delta` 帧:逐字追加渲染到 AI 气泡;
- `message` 帧:本轮已收到过 `delta` 时**不重复**追加(只作最终一致性兜底);
  没有 `delta` 的旧服务端保持既有「追加」行为;
- `done` 帧:用 `data.reply` 覆盖气泡(完成后整卡重渲);
- `citation` / `usage` 等既有帧的收集行为不变。

node 不可用时整文件 skip(不是 fail)。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "frontend" / "app.js"
TMP_ROOT = ROOT / ".tmp"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="未找到 node 可执行文件,跳过前端行为测试(node 不可用时 skip 而非 fail)",
)


# ---------------------------------------------------------------------------
# Node 探针脚本:最小假 DOM + 事件序列驱动
# ---------------------------------------------------------------------------

_NODE_SCRIPT = r"""
"use strict";
var fs = require("fs");

/* ------------------------------ 最小假 DOM ------------------------------ */

function FakeClassList() {
  this._set = Object.create(null);
}
FakeClassList.prototype.add = function (name) { this._set[name] = true; };
FakeClassList.prototype.remove = function (name) { delete this._set[name]; };
FakeClassList.prototype.contains = function (name) { return this._set[name] === true; };
FakeClassList.prototype.toggle = function (name) {
  if (this._set[name]) { delete this._set[name]; } else { this._set[name] = true; }
};

function FakeNode(tag) {
  this.tagName = String(tag || "div").toUpperCase();
  this.childNodes = [];
  this.parentNode = null;
  this.textContent = "";
  this.className = "";
  this.style = {};
  this.disabled = false;
  this.value = "";
  this.title = "";
  this.type = "";
  this.scrollTop = 0;
  this.scrollHeight = 0;
  this.attributes = Object.create(null);
  this.listeners = Object.create(null);
  this.classList = new FakeClassList();
}
FakeNode.prototype.appendChild = function (child) {
  child.parentNode = this;
  this.childNodes.push(child);
  return child;
};
FakeNode.prototype.removeChild = function (child) {
  var i = this.childNodes.indexOf(child);
  if (i >= 0) this.childNodes.splice(i, 1);
  child.parentNode = null;
  return child;
};
FakeNode.prototype.addEventListener = function (type, fn) {
  (this.listeners[type] = this.listeners[type] || []).push(fn);
};
FakeNode.prototype.removeEventListener = function () {};
FakeNode.prototype.setAttribute = function (key, value) { this.attributes[key] = String(value); };
FakeNode.prototype.getAttribute = function (key) {
  return Object.prototype.hasOwnProperty.call(this.attributes, key) ? this.attributes[key] : null;
};
FakeNode.prototype.removeAttribute = function (key) { delete this.attributes[key]; };
FakeNode.prototype.focus = function () {};
FakeNode.prototype.blur = function () {};
FakeNode.prototype.dispatchEvent = function () { return true; };
FakeNode.prototype.closest = function () { return null; };
FakeNode.prototype.querySelector = function () { return null; };
FakeNode.prototype.querySelectorAll = function () { return []; };

var byId = Object.create(null);
global.document = {
  getElementById: function (id) {
    if (!byId[id]) byId[id] = new FakeNode("div");
    return byId[id];
  },
  createElement: function (tag) { return new FakeNode(tag); },
  createTextNode: function (text) {
    var node = new FakeNode("#text");
    node.textContent = String(text);
    return node;
  },
  addEventListener: function () {},
  removeEventListener: function () {}
};

var store = Object.create(null);
global.window = global;
global.window.localStorage = {
  getItem: function (key) {
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  },
  setItem: function (key, value) { store[key] = String(value); },
  removeItem: function (key) { delete store[key]; },
  clear: function () { store = Object.create(null); }
};
global.location = {
  pathname: "/chat.html",
  replace: function () {},
  reload: function () {}
};
global.WebSocket = function FakeWebSocket() {
  this.readyState = 0;
  this.close = function () {};
  this.send = function () {};
};
global.Event = global.Event || function (type, init) { this.type = type; this.init = init; };
global.__DSH_TEST_HOOKS__ = {};

/* ------------------------------ 加载 app.js ------------------------------ */

var appPath = process.argv[2];
if (!appPath) {
  throw new Error("缺少 frontend/app.js 路径参数");
}
(0, eval)(fs.readFileSync(appPath, "utf8"));

var hooks = global.__DSH_TEST_HOOKS__.app;
if (!hooks) {
  throw new Error("frontend/app.js 未注册测试钩子 window.__DSH_TEST_HOOKS__.app");
}

/* ------------------------------ 事件序列驱动 ------------------------------ */

// 与 sendTurn 共用同一份状态/句柄工厂(createTurnMeta / createTurnUi),
// 保证测试与线上跑的是同一套状态结构。
function newTurn() {
  return { ui: hooks.createTurnUi(), meta: hooks.createTurnMeta() };
}

function bubbleText(turn) {
  var ai = turn.ui.getAi();
  return ai ? ai.bubble.textContent : "";
}

function send(turn, event, data) {
  hooks.handleFrame({ event: event, data: data }, turn.ui, turn.meta);
}

var out = {};

/* --- 场景 1:真流式(delta 帧逐字渲染 + message 帧不重复 + done 覆盖) --- */
var stream = newTurn();
send(stream, "delta", { delta: "您好" });
out.delta_first = bubbleText(stream);

send(stream, "delta", { delta: "，我是" });
send(stream, "delta", { delta: "小艾" });
out.delta_accumulated = bubbleText(stream);

/* message 帧(本轮已收到 delta):正文已渲染过,不能重复追加 */
send(stream, "message", { delta: "您好，我是小艾" });
out.message_no_duplicate = bubbleText(stream);
out.meta_reply_after_message = stream.meta.reply;
out.message_reply_recorded = stream.meta.messageReply;
out.streamed_flag = stream.meta.streamed;

/* done 帧:data.reply 是权威最终文案 → 覆盖气泡 */
var finalReply = "您好，我是小艾，请问需要什么帮助？";
send(stream, "done", { reply: finalReply, session_id: "sess-w10", trace_id: "trace-w10" });
out.done_overwrite = bubbleText(stream);
out.meta_reply_after_done = stream.meta.reply;

/* --- 场景 2:旧服务端(只有 message 帧) → 保持既有追加行为 --- */
var legacy = newTurn();
send(legacy, "message", { delta: "您好" });
send(legacy, "message", { delta: "，请问有什么问题？" });
out.legacy_accumulated = bubbleText(legacy);
out.legacy_meta_reply = legacy.meta.reply;
out.legacy_streamed_flag = legacy.meta.streamed;

/* --- 场景 3:只发 delta、不发 message(done 兜底) --- */
var deltaOnly = newTurn();
send(deltaOnly, "delta", { delta: "只有流式帧" });
out.delta_only_before_done = bubbleText(deltaOnly);
send(deltaOnly, "done", { reply: "只有流式帧,最终文案以此为准" });
out.delta_only_after_done = bubbleText(deltaOnly);

/* --- 场景 4:既有帧(citation / usage)收集行为不变 --- */
var legacy2 = newTurn();
send(legacy2, "message", { delta: "正文" });
send(legacy2, "citation", { clause_id: "C-1", snippet: "原文", index: 1 });
send(legacy2, "usage", { tokens_in: 10, tokens_out: 20, cost_usd: 0.001, latency_ms: 88 });
out.citation_count = legacy2.meta.citations.length;
out.citation_clause_id = legacy2.meta.citations.length ? legacy2.meta.citations[0].clause_id : "";
out.usage_tokens_in = legacy2.meta.usage ? legacy2.meta.usage.tokens_in : null;

process.stdout.write("__DSH_JSON__" + JSON.stringify(out) + "\n");
"""


# ---------------------------------------------------------------------------
# 执行探针
# ---------------------------------------------------------------------------


def _run_probe() -> dict:
    """生成 Node 探针脚本并执行,返回它打印的 JSON 观察结果。

    子进程的标准输出/错误**落文件**而不是管道:DSH 沙盒下 Windows 匿名管道
    (CreatePipe)会报 WinError 5,文件重定向没有这个问题。
    """
    work_dir = TMP_ROOT / f"w10_frontend_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "frontend_delta_probe.js"
    script.write_text(_NODE_SCRIPT, encoding="utf-8")
    stdout_path = work_dir / "probe.stdout.txt"
    stderr_path = work_dir / "probe.stderr.txt"
    try:
        with stdout_path.open("wb") as out_file, stderr_path.open("wb") as err_file:
            proc = subprocess.run(
                [NODE, str(script), str(APP_JS)],
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=err_file,
                timeout=120,
                cwd=str(ROOT),
            )
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    assert proc.returncode == 0, (
        f"node 探针失败(exit={proc.returncode})\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "__DSH_JSON__" in stdout, (
        f"node 探针未输出 JSON 结果\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    return json.loads(stdout.split("__DSH_JSON__", 1)[1])


@pytest.fixture(scope="module")
def probe() -> dict:
    return _run_probe()


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


def test_delta_frame_renders_incrementally(probe: dict) -> None:
    """delta 帧:逐字渲染到 AI 气泡(第一帧就是这段文字)。"""
    assert probe["delta_first"] == "您好"


def test_delta_frames_accumulate(probe: dict) -> None:
    """多个 delta 帧:气泡文本是累加结果(逐字追加)。"""
    assert probe["delta_accumulated"] == "您好，我是小艾"


def test_message_frame_does_not_duplicate_after_delta(probe: dict) -> None:
    """已收到过 delta 时,message 帧不再追加到气泡(否则整段重复显示)。"""
    assert probe["message_no_duplicate"] == "您好，我是小艾"
    # message 帧的文本仍被记下来,供 done 做一致性校验
    assert probe["message_reply_recorded"] == "您好，我是小艾"
    # 已渲染正文(meta.reply)不被 message 帧污染
    assert probe["meta_reply_after_message"] == "您好，我是小艾"
    assert probe["streamed_flag"] is True


def test_done_frame_overwrites_with_authoritative_reply(probe: dict) -> None:
    """done 帧:用 data.reply 覆盖气泡(完成后整卡重渲)。"""
    expected = "您好，我是小艾，请问需要什么帮助？"
    assert probe["done_overwrite"] == expected
    assert probe["meta_reply_after_done"] == expected


def test_delta_only_flow_falls_back_to_done_reply(probe: dict) -> None:
    """只发 delta、不发 message 时,最终文案同样以 done.reply 为准。"""
    assert probe["delta_only_before_done"] == "只有流式帧"
    assert probe["delta_only_after_done"] == "只有流式帧,最终文案以此为准"


def test_legacy_message_only_flow_still_appends(probe: dict) -> None:
    """回归:没有 delta 的旧流程,message 帧仍然是「追加」渲染。"""
    assert probe["legacy_accumulated"] == "您好，请问有什么问题？"
    assert probe["legacy_meta_reply"] == "您好，请问有什么问题？"
    assert probe["legacy_streamed_flag"] is False


def test_citation_and_usage_collection_unchanged(probe: dict) -> None:
    """回归:citation / usage 帧的收集行为不变。"""
    assert probe["citation_count"] == 1
    assert probe["citation_clause_id"] == "C-1"
    assert probe["usage_tokens_in"] == 10
