"""W12-3 交付物静态验收:没有 Docker 也不假装跑过运行时。

这些用例只证明"交付物的形状对":YAML 可解析、服务/端口/卷/profiles 键齐全、
Dockerfile 多阶段且非 root、nginx 的 /metrics 有内网限定、CI 三门禁齐且密封。
真实 `docker build` / `compose up` **未在本机验证**,deploy/README.md 里如实标注。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _load_lint():
    """按文件路径加载 deploy/lint_artifacts.py(deploy 不是包,也不该为测试改包结构)。"""
    spec = importlib.util.spec_from_file_location(
        "deploy_lint_artifacts", _ROOT / "deploy" / "lint_artifacts.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lint_mod = _load_lint()


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_repo_delivery_artifacts_lint_clean():
    """真实仓库上交付物自检零错误,且每组都产出 OK。"""
    errors, oks = lint_mod.lint(_ROOT)
    assert errors == [], f"交付物静态校验未通过:{errors}"
    assert len(oks) >= 6, oks


def test_dockerfile_multistage_nonroot_healthcheck():
    text = _read("deploy/Dockerfile")
    assert lint_mod.check_dockerfile(text) == []
    assert text.count("\nFROM ") >= 1  # 至少两个 FROM(第一个可能不在行首)
    assert len(re.findall(r"(?m)^FROM\s", text)) >= 2
    assert "USER copilot" in text
    assert "HEALTHCHECK" in text
    assert "requirements.lock.txt" in text
    # 运行阶段不再安装编译器
    runtime = text.split("AS runtime", 1)[1]
    assert "build-essential" not in runtime


def test_dockerfile_lint_catches_root_and_missing_healthcheck():
    text = _read("deploy/Dockerfile")
    root_text = text.replace("USER copilot", "USER root")
    errors = lint_mod.check_dockerfile(root_text)
    assert any("root" in e for e in errors), errors

    no_health = "\n".join(
        line for line in text.splitlines() if not line.strip().upper().startswith("HEALTHCHECK")
    )
    errors = lint_mod.check_dockerfile(no_health)
    assert any("HEALTHCHECK" in e for e in errors), errors


def test_compose_structure_services_ports_volumes_profiles():
    doc = yaml.safe_load(_read("deploy/docker-compose.yml"))
    assert lint_mod.check_compose(doc) == []
    services = doc["services"]
    assert {"backend", "nginx", "postgres", "redis", "prometheus", "grafana"} <= set(services)
    assert services["nginx"]["ports"] == ["8080:80"]
    assert "copilot-data" in doc["volumes"]
    assert "full" in services["postgres"]["profiles"]
    assert "observability" in services["prometheus"]["profiles"]
    # backend 只 expose,不映射宿主端口(对外只经 nginx)
    assert "ports" not in services["backend"]
    assert services["backend"]["expose"] == ["8000"]


def test_compose_lint_catches_missing_profile_and_undeclared_volume():
    doc = yaml.safe_load(_read("deploy/docker-compose.yml"))
    doc["services"]["postgres"].pop("profiles")
    errors = lint_mod.check_compose(doc)
    assert any("profiles" in e for e in errors), errors

    doc = yaml.safe_load(_read("deploy/docker-compose.yml"))
    doc["services"]["backend"]["volumes"] = ["ghost-volume:/app/data"]
    errors = lint_mod.check_compose(doc)
    assert any("未声明" in e for e in errors), errors


def test_nginx_metrics_internal_only():
    text = _read("deploy/nginx.conf")
    assert lint_mod.check_nginx(text) == []
    metrics_block = text.split("location = /metrics", 1)[1].split("}", 1)[0]
    assert "deny all;" in metrics_block
    assert "allow 127.0.0.1;" in metrics_block
    # 反向对照:去掉 deny 必须被抓到
    broken = text.replace("deny all;", "")
    assert any("deny all" in e for e in lint_mod.check_nginx(broken))


def test_prometheus_scrapes_backend_metrics():
    doc = yaml.safe_load(_read("deploy/prometheus.yml"))
    assert lint_mod.check_prometheus(doc) == []
    targets = doc["scrape_configs"][0]["static_configs"][0]["targets"]
    assert targets == ["backend:8000"]


def test_ci_has_three_gates_and_sealed_env():
    text = _read(".github/workflows/ci.yml")
    doc = yaml.safe_load(text)
    assert lint_mod.check_ci(doc, text) == []
    assert {"lint", "pytest", "eval", "deploy"} <= set(doc["jobs"])
    top_env = doc.get("env") or {}
    assert not ({"LLM_API_KEY", "LLM_BASE_URL", "MINIMAX_API_KEY"} & set(top_env)), top_env
    assert "run_eval.py" in text and "--smoke" in text and "--out-dir" in text

    injected = text.replace(
        "  PYTHONDONTWRITEBYTECODE: \"1\"",
        "  PYTHONDONTWRITEBYTECODE: \"1\"\n  LLM_API_KEY: sk-should-not-exist",
    )
    errors = lint_mod.check_ci(yaml.safe_load(injected), injected)
    assert any("LLM_API_KEY" in e for e in errors), errors


def test_deploy_readme_states_not_runtime_verified():
    """如实标注:README 必须写明未在本机运行时验证,且列出待核对项。"""
    text = _read("deploy/README.md")
    assert "未在本机运行时验证" in text
    assert "首次落地核对单" in text
    assert "docker compose" in text
    # 不准出现"已验证可运行"这类未做过的断言
    for forbidden in ("已验证可运行", "已在本地验证运行", "build 通过"):
        assert forbidden not in text


def test_entrypoint_migrates_then_serves():
    text = _read("deploy/entrypoint.sh")
    assert "set -eu" in text
    assert "alembic" in text and "upgrade head" in text
    assert "uvicorn app.main:app" in text
    assert "SEED_ON_EMPTY" in text
    assert "DATABASE_URL" in text


def test_no_unfinished_markers_in_delivery_artifacts():
    """红线:交付物里不留未完成标记(拼字符串是为了让本文件自身不触发 grep)。"""
    markers = ("TO" + "DO", "FIX" + "ME")
    paths = [
        "deploy/Dockerfile",
        "deploy/entrypoint.sh",
        "deploy/docker-compose.yml",
        "deploy/nginx.conf",
        "deploy/prometheus.yml",
        "deploy/lint_artifacts.py",
        "deploy/README.md",
        ".github/workflows/ci.yml",
        ".dockerignore",
    ]
    for rel in paths:
        text = _read(rel)
        for marker in markers:
            assert marker not in text, f"{rel} 含未完成标记"


# ---------------------------------------------------------------------------
# 2026-11-21 CI container job 抓到的真缺陷:nginx 只反代 /api/,
# 而应用还有 /auth /orders /refunds /tickets /v1 四组根级路由 ——
# 它们全部落进静态 `location /`(try_files … =404),**经 nginx 根本登不进去**。
# 静态 lint 查"键齐不齐"查不出这个;只有真起栈冒烟才暴露。
#
# 这里把「枚举全部出口」做成永久守卫:**不硬编码前缀清单**(那样新增 router 又会漏),
# 而是从 app.openapi() 取全部真实路径,按 nginx 匹配语义算出每条路径命中的 location,
# 断言它必须 proxy_pass 到后端。
# ---------------------------------------------------------------------------


def _nginx_locations() -> list[tuple[str, str, str]]:
    """解析 location 块 → [(kind, pattern, body)],kind ∈ {'exact','prefix'}。

    本项目的 nginx.conf 只用精确(=)与前缀 location,没有正则;
    真加了正则本解析会显式报错,而不是静默给出错误的匹配结论。
    """
    text = _read("deploy/nginx.conf")
    out: list[tuple[str, str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"\s*location\s+(.+?)\s*\{", lines[i])
        if not m:
            i += 1
            continue
        head = m.group(1).strip()
        if head.startswith("~"):
            raise AssertionError("nginx.conf 出现正则 location,本守卫的匹配模型需同步升级")
        kind = "exact" if head.startswith("=") else "prefix"
        pattern = head[1:].strip() if kind == "exact" else head
        depth = 1
        body: list[str] = []
        i += 1
        while i < len(lines) and depth > 0:
            depth += lines[i].count("{") - lines[i].count("}")
            if depth > 0:
                body.append(lines[i])
            i += 1
        out.append((kind, pattern, "\n".join(body)))
    assert out, "没解析到任何 location —— 解析器坏了,不是配置对了"
    return out


def _nginx_match(path: str, locs: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    """nginx 匹配语义:精确 `=` 优先,否则**最长前缀**胜出。"""
    for kind, pattern, body in locs:
        if kind == "exact" and pattern == path:
            return kind, pattern, body
    best: tuple[str, str, str] | None = None
    for kind, pattern, body in locs:
        if kind == "prefix" and path.startswith(pattern):
            if best is None or len(pattern) > len(best[1]):
                best = (kind, pattern, body)
    assert best is not None, f"{path} 没有命中任何 location(连 location / 都没有?)"
    return best


def test_nginx_proxies_every_openapi_path_instead_of_serving_static():
    """全部 OpenAPI 路径经 nginx 都必须反代到后端,不能落进静态 404。"""
    from app.main import app

    paths = sorted(app.openapi()["paths"])
    assert len(paths) > 20, f"OpenAPI 路径数异常({len(paths)}),取数口径可能坏了"
    locs = _nginx_locations()
    fell_into_static = []
    for p in paths:
        probe = re.sub(r"\{[^}]+\}", "1", p)  # 路径参数换成占位值,不影响前缀匹配
        _kind, pattern, body = _nginx_match(probe, locs)
        if "proxy_pass" not in body:
            fell_into_static.append(f"{p} -> location {pattern}")
    assert not fell_into_static, (
        "以下路径经 nginx 落到静态 handler(404),部署栈上这些接口不可用:\n  "
        + "\n  ".join(fell_into_static)
    )


def test_nginx_static_frontend_files_are_not_swallowed_by_proxy():
    """反向守卫:补反代前缀时**不能反过来把静态文件吞掉**。

    真实风险:前端有 `api.js`,若把 `location /api/` 写成 `location /api`(去掉尾斜杠),
    `/api.js` 就会被反代到后端 → 前端整个挂掉。本用例枚举 frontend/ 全部文件,
    断言它们仍由静态 handler 提供。

    末尾的合成对照是**门禁有效性自检**(本项目红线:假门禁比没门禁更糟):
    构造一个去掉尾斜杠的 /api 前缀,确认匹配器真会把 api.js 判给它 ——
    否则这条守卫可能只是因为匹配器坏了才"通过"。
    """
    locs = _nginx_locations()
    static_files = sorted(p.name for p in (_ROOT / "frontend").iterdir() if p.is_file())
    assert static_files, "frontend/ 空了?取数口径坏了"
    swallowed = []
    for name in static_files:
        _kind, pattern, body = _nginx_match(f"/{name}", locs)
        if "proxy_pass" in body:
            swallowed.append(f"/{name} -> location {pattern}")
    assert not swallowed, "静态文件被反代吞掉,前端会挂:\n  " + "\n  ".join(swallowed)

    # 有效性自检:去掉尾斜杠的 /api 必须真能吞掉 api.js(证明本守卫有判别力)
    collision = [(k, "/api" if p == "/api/" else p, b) for k, p, b in locs]
    _kind, pattern, body = _nginx_match("/api.js", collision)
    assert pattern == "/api" and "proxy_pass" in body, (
        "匹配器没能识别出尾斜杠缺失导致的静态吞噬 —— 本守卫失去判别力"
    )


def _ci_doc_minimal() -> dict:
    """构造一份能过既有 check_ci 的最小 ci doc,用于负控(故意抽掉某项看它是否真报错)。"""
    return {
        "jobs": {
            "lint": {
                "timeout-minutes": 20,
                "steps": [{"run": "python -m compileall -q backend/app"},
                          {"run": "python deploy/lint_artifacts.py"}],
            },
            "pytest": {"timeout-minutes": 20, "steps": [{"run": "python -m pytest backend/tests -q"}]},
            "eval": {"timeout-minutes": 20, "steps": [
                {"run": "python backend/scripts/run_eval.py --smoke --out-dir /tmp/r"}]},
            "deploy": {"timeout-minutes": 20, "steps": [{"run": "python deploy/lint_artifacts.py"}]},
            "pg-redis": {
                "timeout-minutes": 20,
                "services": {"postgres": {"image": "postgres:16"}, "redis": {"image": "redis:7"}},
                "steps": [{"run": "python -m alembic -c backend/alembic.ini upgrade head"},
                          {"run": "python -m alembic -c backend/alembic.ini downgrade base"}],
            },
            "container": {
                "timeout-minutes": 25,
                "steps": [{"run": "docker compose -f deploy/docker-compose.yml build backend"},
                          {"run": "docker stop $CID"}],
            },
        }
    }


def test_ci_lint_requires_runtime_jobs_and_per_job_timeout():
    """lint 必须校验**运行时两 job**与**每 job 超时**,不能只认老四门禁。

    两条都是真事故换来的:
    - pg-redis / container 是把「未运行时验证」变成可执行验证的唯一手段,
      被误删就退回"写了但没验",而 lint 原先**根本不看它们**;
    - 无 job 级超时那次,PG 全量步骤**卡死 121 分钟**没有任何信号。
    """
    import copy

    full = _ci_doc_minimal()
    assert lint_mod.check_ci(full, yaml.safe_dump(full, allow_unicode=True)) == []

    for missing in ("pg-redis", "container"):
        doc = copy.deepcopy(full)
        del doc["jobs"][missing]
        errs = lint_mod.check_ci(doc, yaml.safe_dump(doc, allow_unicode=True))
        assert any(missing in e for e in errs), f"抽掉 {missing} job 竟不报错:{errs}"

    doc = copy.deepcopy(full)
    del doc["jobs"]["container"]["timeout-minutes"]
    errs = lint_mod.check_ci(doc, yaml.safe_dump(doc, allow_unicode=True))
    assert any("timeout-minutes" in e for e in errs), f"抽掉 job 超时竟不报错:{errs}"

    doc = copy.deepcopy(full)
    doc["jobs"]["container"]["steps"] = [{"run": "echo noop"}]
    errs = lint_mod.check_ci(doc, yaml.safe_dump(doc, allow_unicode=True))
    assert errs, "container job 不 build 镜像也不验停机,竟不报错"
