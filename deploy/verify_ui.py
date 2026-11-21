#!/usr/bin/env python
"""经 nginx 的**真浏览器点击流验收**(CI container job 专用)。

为什么必须有这一层(2026-11-21 血的教训):
本项目四轮安全审计 + 966 条用例全绿的同时,部署栈**根本登不进去** ——
nginx 只反代 `/api/`,`/auth` `/orders` `/refunds` `/tickets` `/v1` 共 19 个端点全落静态 404。
漏掉的原因很具体:**全部接口测试用 TestClient 直连 app,根本不经过 nginx**;
前端也从没在真浏览器里跑过。静态守卫(按 nginx 匹配语义核对 OpenAPI 全路径)能拦配置缺口,
但**拦不住"配置对了而页面仍然坏"**(JS 报错、静默降级、渲染不出数据)。

尤其阴险的一处:买家页「我的订单」条走 `GET /orders`,**失败时是静默隐藏**
(`frontend/app.js` 里取不到就 `orderStrip.classList.add("hidden")`)——
页面不报错、不弹提示,只是少一块功能。只有真浏览器 + 断言"这块必须在"才抓得到。

覆盖的点击流(每步都点名,失败能立刻定位):
  1. login_buyer        登录页填表点按钮 → 跳 chat.html,`#loginError` 必须为空
  2. buyer_orders       「我的订单」条必须可见且至少一个订单 chip(**打 /orders**)
  3. buyer_chat         发一轮消息 → 回复渲染出来;可见文本**不得出现 RP-xx/LP-xx**(C1 回归)
  4. logout             点退出 → 回 login.html
  5. login_agent        坐席登录 → 跳 agent.html
  6. agent_queue        工单队列计数 > 0 且有队列项(**打 /api/b/tickets**)
  7. agent_detail       点开第一条工单 → 详情面板可见非空(**打 /tickets/{id} 与 /orders/{id}**)
  8. console_clean      全程无 JS 报错(favicon 404 除外)

用法(CI 里栈已起好):
    python -m pip install "playwright==1.56.0"
    python -m playwright install --with-deps chromium
    python deploy/verify_ui.py --out-dir ui_shots --result ui_result.json

输出:逐步 stdout + 截图 + 机器可读 JSON(`ok` / `failed_step` / `reason` / `steps`)。
**JSON 是给负控用的**:CI 会故意抽掉 nginx 的 `/auth` 反代再跑一遍,
断言 `failed_step == "login_buyer"` 且 reason 含 404 —— 证明这道门禁真能拦住,
而不是因为别的原因(浏览器没装、选择器写错)恰好失败。

⚠️ 本脚本**不碰任何数据库**,只经 HTTP 操作已起好的栈;容器内是 FakeLLM,无真实模型花费。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

BUYER = ("buyer_demo", "demo1234")
AGENT = ("agent_demo", "demo1234")
#: C1 红线:内部条款代号不得出现在买家可见文本里。
CLAUSE_CODE = re.compile(r"\b(?:RP|LP)-\d{2}\b")
STEP_TIMEOUT_MS = 25_000


class StepFailed(Exception):
    """带步骤名的失败,便于负控精确断言"是被哪一步拦住的"。"""

    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


def _login(page, base: str, username: str, password: str, expect_path: str, step: str) -> None:
    page.goto(f"{base}/login.html", wait_until="domcontentloaded")
    page.wait_for_selector("#loginBtn", state="visible", timeout=STEP_TIMEOUT_MS)
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("#loginBtn")
    try:
        page.wait_for_url(re.compile(re.escape(expect_path)), timeout=STEP_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001 - 要把页面自己显示的错误原文带出来
        err = ""
        try:
            err = (page.inner_text("#loginError") or "").strip()
        except Exception:  # noqa: BLE001 - 页面可能已经不在登录页
            pass
        raise StepFailed(
            step,
            f"登录后未跳到 {expect_path};登录页错误原文={err!r};当前 URL={page.url};{exc!r}",
        ) from exc
    err = (page.inner_text("#loginError") or "").strip() if page.query_selector("#loginError") else ""
    if err:
        raise StepFailed(step, f"已跳转但登录页仍有错误文本:{err!r}")


def run(base: str, out_dir: Path, strict_console: bool, channel: str | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []
    console_errors: list[str] = []
    failed_step: str | None = None
    reason = ""

    def shot(page, name: str) -> None:
        try:
            page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
        except Exception as exc:  # noqa: BLE001 - 截图失败不该掩盖真正的断言失败
            print(f"  [warn] 截图 {name} 失败:{exc!r}", flush=True)

    def record(name: str, t0: float, detail: str = "") -> None:
        steps.append({"step": name, "ok": True, "ms": round((time.time() - t0) * 1000), "detail": detail})
        print(f"  [OK]   {name} {detail}", flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel=channel or None,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(STEP_TIMEOUT_MS)

        def on_console(msg) -> None:
            if msg.type == "error":
                text = msg.text or ""
                if "favicon" in text.lower():
                    return  # 静态站没放 favicon,404 属噪声
                console_errors.append(f"console: {text}")

        page.on("console", on_console)
        page.on("pageerror", lambda exc: console_errors.append(f"pageerror: {exc}"))

        try:
            t0 = time.time()
            _login(page, base, *BUYER, expect_path="/chat.html", step="login_buyer")
            shot(page, "01_buyer_chat")
            record("login_buyer", t0, f"→ {page.url}")

            # 「我的订单」条:失败时前端**静默隐藏**,所以必须断言它可见且有 chip。
            t0 = time.time()
            try:
                page.wait_for_selector("#orderStrip:not(.hidden)", state="visible", timeout=STEP_TIMEOUT_MS)
                page.wait_for_function(
                    "() => document.querySelectorAll('#orderChips button').length > 0",
                    timeout=STEP_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001
                raise StepFailed(
                    "buyer_orders",
                    "「我的订单」条不可见或没有订单 chip —— GET /orders 很可能没被反代"
                    f"(前端取不到就静默隐藏);{exc!r}",
                ) from exc
            chips = page.eval_on_selector_all("#orderChips button", "els => els.length")
            shot(page, "02_buyer_orders")
            record("buyer_orders", t0, f"订单 chip {chips} 个")

            t0 = time.time()
            page.fill("#text", "退货运费谁承担")
            page.click("#send")
            # ⚠️ 第一版断言太松,自己踩了假验证:只判"stream 含用户文本且长度 > 用户文本+10",
            # 结果被**思考占位符**「AI 正在思考」满足 —— 截图里助手气泡还在转圈,回复根本没到。
            # 改成三条硬条件:①思考占位 div.loading-row 消失;②真回复气泡
            # div.msg.ai:not(.error) .bubble 有实质文本;③无错误气泡(请求失败…)。
            try:
                page.wait_for_function(
                    "() => {"
                    " const s = document.getElementById('stream'); if (!s) return false;"
                    " if (s.querySelector('.loading-row')) return false;"
                    " if (s.querySelector('.msg.ai.error')) return false;"
                    " const b = s.querySelectorAll('.msg.ai:not(.error) .bubble');"
                    " for (const x of b) {"
                    "   const t = (x.innerText || '').trim();"
                    "   if (t.length >= 12 && !t.includes('请求失败') && !t.includes('正在思考')) return true;"
                    " }"
                    " return false; }",
                    timeout=STEP_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001
                state = page.evaluate(
                    "() => { const s = document.getElementById('stream');"
                    " return s ? {loading: !!s.querySelector('.loading-row'),"
                    " error: !!s.querySelector('.msg.ai.error'), text: s.innerText.slice(0, 300)} : null; }"
                )
                raise StepFailed("buyer_chat", f"没等到真回复气泡渲染;现场={state!r};{exc!r}") from exc
            reply = page.eval_on_selector(
                "#stream .msg.ai:not(.error) .bubble", "e => (e.innerText || '').trim()"
            )
            # R19(2026-11-21):镜像原先**不 COPY data/policies**、entrypoint 也不跑
            # load_policies.py ⇒ 部署出来知识库是空的,**任何政策问题都只会回"检索无命中"兜底**。
            # 本机对照实测:不装语料 58 字兜底,装了同一问题 336 字真答案 + [1] 引用。
            # 所以这里必须断言"引用真渲染出来",否则这条 UI 验收对 R19 完全无感。
            if "检索无命中" in reply or "暂未找到" in reply:
                raise StepFailed(
                    "buyer_chat",
                    f"回复是检索无命中兜底 —— 部署栈知识库很可能是空的(R19):{reply[:120]!r}",
                )
            n_cite = page.eval_on_selector_all("#stream .citations .citation-ref", "els => els.length")
            # ⚠️ 引用有**两种渲染形态**,别只认一种(第一版只认引用条,误判为失败):
            #   - SSE 流式路径:`renderCitations()` 追加 div.citations > button.citation-ref
            #   - JSON done 路径:只重渲气泡文本,`[n]` 角标**内联在回复正文里**
            # 页面上真实可见的是内联角标,所以断言"有 [n] 角标 **或** 有引用条"。
            # 兜底回复("检索无命中")两者都没有 ⇒ 仍能抓住 R19。
            inline_refs = re.findall(r"\[\d+\]", reply)
            if n_cite < 1 and not inline_refs:
                raise StepFailed(
                    "buyer_chat",
                    f"回复渲染了但**既无内联 [n] 角标也无引用条**(检索/引用链路断了):{reply[:120]!r}",
                )
            visible = page.inner_text("#stream")
            leak = CLAUSE_CODE.findall(visible)
            if leak:
                raise StepFailed("buyer_chat", f"买家可见文本泄露内部条款代号(C1 回归):{leak}")
            shot(page, "03_buyer_reply")
            record("buyer_chat", t0,
                   f"真回复 {len(reply)} 字,内联角标 {len(inline_refs)} 个/引用条 {n_cite} 条,"
                   f"无条款代号:{reply[:50]!r}")

            t0 = time.time()
            page.click("#logoutBtn")
            try:
                page.wait_for_url(re.compile(r"/login\.html"), timeout=STEP_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                raise StepFailed("logout", f"点退出后没回登录页:{exc!r}") from exc
            record("logout", t0)

            t0 = time.time()
            _login(page, base, *AGENT, expect_path="/agent.html", step="login_agent")
            shot(page, "04_agent_home")
            record("login_agent", t0, f"→ {page.url}")

            t0 = time.time()
            try:
                page.wait_for_function(
                    "() => document.querySelectorAll('#queueList [data-ticket]').length > 0",
                    timeout=STEP_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001
                state = ""
                try:
                    state = (page.inner_text("#queueState") or "").strip()
                except Exception:  # noqa: BLE001
                    pass
                raise StepFailed("agent_queue", f"工单队列没加载出条目;队列区文本={state!r};{exc!r}") from exc
            n = page.eval_on_selector_all("#queueList [data-ticket]", "els => els.length")
            shot(page, "05_agent_queue")
            record("agent_queue", t0, f"队列 {n} 条")

            t0 = time.time()
            page.click("#queueList [data-ticket]")
            try:
                page.wait_for_selector("#detailBody:not(.hidden)", state="visible", timeout=STEP_TIMEOUT_MS)
                page.wait_for_function(
                    "() => { const d = document.getElementById('detailBody');"
                    " return d && d.innerText && d.innerText.trim().length > 30; }",
                    timeout=STEP_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001
                raise StepFailed(
                    "agent_detail",
                    "点开工作单后详情面板空/不可见 —— GET /tickets/{id} 或 /orders/{id} 很可能没被反代"
                    f";{exc!r}",
                ) from exc
            detail = page.inner_text("#detailBody")
            shot(page, "06_agent_detail")
            record("agent_detail", t0, f"详情 {len(detail)} 字")

            t0 = time.time()
            if strict_console and console_errors:
                raise StepFailed("console_clean", f"全程出现 JS 报错:{console_errors[:6]}")
            record("console_clean", t0, f"JS 报错 {len(console_errors)} 条")
        except StepFailed as exc:
            failed_step, reason = exc.step, exc.reason
            steps.append({"step": exc.step, "ok": False, "detail": exc.reason})
            print(f"  [FAIL] {exc.step}: {exc.reason}", flush=True)
            shot(page, f"FAIL_{exc.step}")
        except Exception as exc:  # noqa: BLE001 - 基础设施类失败也要如实报出,不冒充断言失败
            failed_step, reason = "infrastructure", repr(exc)
            steps.append({"step": "infrastructure", "ok": False, "detail": repr(exc)})
            print(f"  [FAIL] infrastructure: {exc!r}", flush=True)
        finally:
            context.close()
            browser.close()

    return {
        "ok": failed_step is None,
        "failed_step": failed_step,
        "reason": reason,
        "steps": steps,
        "console_errors": console_errors,
        "base_url": base,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="经 nginx 的真浏览器点击流验收")
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--out-dir", default="ui_shots")
    ap.add_argument("--result", default="ui_result.json")
    ap.add_argument("--allow-console-errors", action="store_true")
    ap.add_argument(
        "--channel",
        default="",
        help="浏览器通道。CI 留空用 bundled chromium;本机无 bundled 时可用 msedge。",
    )
    args = ap.parse_args()

    print(f"[ui] 目标 = {args.base_url}", flush=True)
    print("[ui] 是否经 nginx 取决于调用方:CI 传 nginx 的 8080;本机预跑传 uvicorn 直连端口"
          "(**那一跑不算经 nginx 已验**)", flush=True)
    out = run(
        args.base_url,
        Path(args.out_dir),
        strict_console=not args.allow_console_errors,
        channel=args.channel,
    )
    Path(args.result).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ui] 结论:{'全部通过' if out['ok'] else '失败于 ' + str(out['failed_step'])}", flush=True)
    print(f"[ui] 结果 JSON = {args.result};截图目录 = {args.out_dir}", flush=True)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())