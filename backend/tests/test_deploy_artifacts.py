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
