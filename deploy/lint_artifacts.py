"""deploy/lint_artifacts.py — W12-3 交付物的静态自检(不依赖 Docker)。

本机没有 Docker,无法做 `docker build` / `docker compose config` 的运行时验证;
本脚本用纯文本 + YAML 解析做**结构级**校验,把"能不能构建"降级为"形状对不对":

- deploy/Dockerfile:多阶段、非 root USER、HEALTHCHECK、EXPOSE、无硬编码密钥;
- deploy/docker-compose.yml:服务齐全、端口/卷/healthcheck 键存在、可选服务有 profiles、
  顶层 volumes 声明完整、镜像不带 :latest;
- deploy/nginx.conf:静态 root + /api/ 反代 + /ws/ 升级头 + /metrics 的 allow/deny;
- deploy/prometheus.yml:抓取目标指向 backend:8000 且路径为 /metrics;
- .github/workflows/ci.yml:lint / pytest / eval 三门禁存在且命令正确,且不注入真实 API key。

用法:
    python deploy/lint_artifacts.py [--root ROOT]

退出码:0 = 全部通过;1 = 至少一条规则不满足(逐条打印)。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - 依赖缺失时给出可执行的提示
    print("[lint] 缺少 PyYAML,请先安装 requirements.lock.txt", file=sys.stderr)
    raise SystemExit(2)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SECRET_ENV_RE = re.compile(
    r"^\s*ENV\s+.*?(API_KEY|SECRET|PASSWORD|TOKEN)\s*=\s*\S+", re.IGNORECASE | re.MULTILINE
)
_LATEST_IMAGE_RE = re.compile(r":latest$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def check_dockerfile(text: str) -> list[str]:
    """Dockerfile 的硬性规则(与任务书 W12-3 对应)。"""
    errors: list[str] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    stages = [ln for ln in lines if ln.upper().startswith("FROM ")]
    if len(stages) < 2:
        errors.append(f"Dockerfile 应为多阶段构建,实际 FROM 数={len(stages)}")

    user_lines = [ln for ln in lines if ln.upper().startswith("USER ")]
    if not user_lines:
        errors.append("Dockerfile 缺少 USER(禁止以 root 运行)")
    else:
        user = user_lines[-1].split(None, 1)[1].strip().lower()
        if user in {"root", "0", "0:0"}:
            errors.append(f"Dockerfile 最后一个 USER 是 root:{user}")

    if not any(ln.upper().startswith("HEALTHCHECK ") for ln in lines):
        errors.append("Dockerfile 缺少 HEALTHCHECK")
    if not any(ln.upper().startswith("EXPOSE ") for ln in lines):
        errors.append("Dockerfile 缺少 EXPOSE")
    if "COPY requirements.lock.txt" not in text:
        errors.append("Dockerfile 未按 requirements.lock.txt 安装依赖(锁文件口径丢失)")
    if _SECRET_ENV_RE.search(text):
        errors.append("Dockerfile 中疑似硬编码密钥(ENV *KEY/SECRET/PASSWORD/TOKEN=...)")
    if "--allow-root" in text:
        errors.append("Dockerfile 出现 --allow-root(不应以 root 运行服务)")
    return errors


def check_compose(doc: dict) -> list[str]:
    """docker-compose.yml 的结构、端口、卷与 profiles 规则。"""
    errors: list[str] = []
    services = doc.get("services")
    if not isinstance(services, dict):
        return ["compose 缺少顶层 services 映射"]

    required = ["backend", "nginx", "postgres", "redis", "prometheus", "grafana"]
    for name in required:
        if name not in services:
            errors.append(f"compose 缺少服务:{name}")

    backend = services.get("backend") or {}
    if not isinstance(backend, dict):
        errors.append("compose backend 服务定义不是映射")
        backend = {}
    if "build" not in backend:
        errors.append("compose backend 缺少 build(镜像需由 Dockerfile 构建)")
    else:
        build = backend["build"]
        if isinstance(build, dict):
            if not build.get("context"):
                errors.append("compose backend.build 缺少 context")
            if build.get("dockerfile") != "deploy/Dockerfile":
                errors.append("compose backend.build.dockerfile 应指向 deploy/Dockerfile")
    if not backend.get("healthcheck"):
        errors.append("compose backend 缺少 healthcheck")
    if not backend.get("volumes"):
        errors.append("compose backend 缺少 volumes(数据卷不持久化)")

    nginx = services.get("nginx") or {}
    if not isinstance(nginx, dict):
        errors.append("compose nginx 服务定义不是映射")
        nginx = {}
    ports = nginx.get("ports") or []
    if not any(str(p).endswith(":80") for p in ports):
        errors.append(f"compose nginx 端口未映射到容器 80:ports={ports!r}")
    nginx_mounts = nginx.get("volumes") or []
    joined = " ".join(str(v) for v in nginx_mounts)
    if "nginx.conf" not in joined:
        errors.append("compose nginx 未挂载 deploy/nginx.conf")
    if "frontend" not in joined:
        errors.append("compose nginx 未挂载 frontend 静态目录")

    for name in ("postgres", "redis", "prometheus", "grafana"):
        svc = services.get(name) or {}
        if not isinstance(svc, dict):
            errors.append(f"compose {name} 服务定义不是映射")
            continue
        profiles = svc.get("profiles") or []
        if not profiles:
            errors.append(f"compose {name} 缺少 profiles(可选服务必须能被 profile 开关)")
        if not svc.get("image"):
            errors.append(f"compose {name} 缺少 image")
        elif _LATEST_IMAGE_RE.search(str(svc["image"])):
            errors.append(f"compose {name} 使用 :latest 镜像:{svc['image']}")

    declared_volumes = doc.get("volumes") or {}
    if not isinstance(declared_volumes, dict) or "copilot-data" not in declared_volumes:
        errors.append("compose 顶层 volumes 未声明 copilot-data")
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for mount in svc.get("volumes") or []:
            if not isinstance(mount, str) or ":" not in mount:
                continue
            source = mount.split(":", 1)[0]
            if source.startswith(".") or source.startswith("/") or source.startswith("$"):
                continue  # 绑定挂载或变量来源
            if source not in declared_volumes:
                errors.append(f"compose 服务 {name} 引用了未声明的命名卷:{source}")

    if not doc.get("networks"):
        # 不强制自定义网络;默认网络即可,但要求至少没写错键。
        pass
    return errors


def check_nginx(text: str) -> list[str]:
    errors: list[str] = []
    needed = [
        ("upstream copilot_backend", "缺少 upstream copilot_backend"),
        ("proxy_pass http://copilot_backend", "缺少反代到 copilot_backend"),
        ("location = /metrics", "/metrics 缺少精确匹配 location"),
        ("deny all;", "/metrics 缺少 deny all(内网限定不成立)"),
        ("location /api/", "缺少 /api/ 反代"),
        ("location /ws/", "缺少 /ws/ WebSocket 反代"),
        ("Upgrade $http_upgrade", "/ws/ 缺少 Upgrade 头"),
        ("try_files $uri $uri/ =404", "静态站点缺少 try_files"),
    ]
    for needle, message in needed:
        if needle not in text:
            errors.append(f"nginx.conf:{message}")
    if "allow 127.0.0.1;" not in text:
        errors.append("nginx.conf:/metrics 未放行回环地址")
    if "proxy_pass http://copilot_backend" in text and "proxy_set_header Host $host;" not in text:
        errors.append("nginx.conf:反代未透传 Host 头")
    return errors


def check_prometheus(doc: dict) -> list[str]:
    errors: list[str] = []
    jobs = doc.get("scrape_configs") or []
    if not isinstance(jobs, list) or not jobs:
        return ["prometheus.yml 缺少 scrape_configs"]
    targets: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("metrics_path") != "/metrics":
            errors.append(f"prometheus.yml job {job.get('job_name')} 的 metrics_path 不是 /metrics")
        for static in job.get("static_configs") or []:
            targets.extend(str(t) for t in (static.get("targets") or []))
    if "backend:8000" not in targets:
        errors.append(f"prometheus.yml 抓取目标未指向 backend:8000:targets={targets!r}")
    return errors


def check_ci(doc: dict, text: str) -> list[str]:
    errors: list[str] = []
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        return ["ci.yml 缺少 jobs 映射"]
    for name in ("lint", "pytest", "eval", "deploy"):
        if name not in jobs:
            errors.append(f"ci.yml 缺少 job:{name}")

    def job_text(name: str) -> str:
        job = jobs.get(name) or {}
        return yaml.safe_dump(job, allow_unicode=True, sort_keys=True) if isinstance(job, dict) else ""

    lint_text = job_text("lint")
    if "compileall" not in lint_text:
        errors.append("ci.yml lint job 未做 py_compile/compileall 全量语法检查")
    if "lint_artifacts.py" not in lint_text:
        errors.append("ci.yml lint job 未跑 deploy/lint_artifacts.py")
    if "pytest" not in job_text("pytest"):
        errors.append("ci.yml pytest job 未跑 pytest")
    eval_text = job_text("eval")
    if "run_eval.py" not in eval_text or "--smoke" not in eval_text:
        errors.append("ci.yml eval job 未跑 run_eval.py --smoke")
    if "--out-dir" not in eval_text:
        errors.append("ci.yml eval job 未把报告写到临时目录(会脏 data/eval/reports)")

    # 密封:FakeLLM/FakeJudge 口径下不得注入真实 key;空值也算"声明了",一并禁止。
    for match in re.finditer(r"^\s*(LLM_API_KEY|LLM_BASE_URL|MINIMAX_API_KEY)\s*:\s*(.*)$", text, re.MULTILINE):
        errors.append(f"ci.yml 不得注入真实模型环境变量:{match.group(1)}")
    if "secrets." in text:
        errors.append("ci.yml 引用了 secrets(本地交付物不允许真实凭据)")
    return errors


def lint(root: Path) -> tuple[list[str], list[str]]:
    """返回 (errors, oks)。errors 为空即通过。"""
    errors: list[str] = []
    oks: list[str] = []

    dockerfile = root / "deploy" / "Dockerfile"
    if not dockerfile.is_file():
        errors.append("缺少 deploy/Dockerfile")
    else:
        errs = check_dockerfile(_read(dockerfile))
        errors.extend(errs)
        if not errs:
            oks.append("deploy/Dockerfile:多阶段 + 非 root USER + HEALTHCHECK + EXPOSE + 锁文件")

    compose_path = root / "deploy" / "docker-compose.yml"
    if not compose_path.is_file():
        errors.append("缺少 deploy/docker-compose.yml")
        compose_doc = {}
    else:
        try:
            compose_doc = yaml.safe_load(_read(compose_path)) or {}
        except yaml.YAMLError as exc:
            compose_doc = {}
            errors.append(f"deploy/docker-compose.yml 不是合法 YAML:{exc}")
        if isinstance(compose_doc, dict) and compose_doc:
            errs = check_compose(compose_doc)
            errors.extend(errs)
            if not errs:
                oks.append(
                    "deploy/docker-compose.yml:6 服务/nginx:80/命名卷/profiles/healthcheck 齐全"
                )

    nginx_path = root / "deploy" / "nginx.conf"
    if not nginx_path.is_file():
        errors.append("缺少 deploy/nginx.conf")
    else:
        errs = check_nginx(_read(nginx_path))
        errors.extend(errs)
        if not errs:
            oks.append("deploy/nginx.conf:静态 + /api//ws 反代 + /metrics allow/deny")

    prom_path = root / "deploy" / "prometheus.yml"
    if not prom_path.is_file():
        errors.append("缺少 deploy/prometheus.yml")
    else:
        try:
            prom_doc = yaml.safe_load(_read(prom_path)) or {}
        except yaml.YAMLError as exc:
            prom_doc = {}
            errors.append(f"deploy/prometheus.yml 不是合法 YAML:{exc}")
        errs = check_prometheus(prom_doc if isinstance(prom_doc, dict) else {})
        errors.extend(errs)
        if not errs:
            oks.append("deploy/prometheus.yml:抓取 backend:8000/metrics")

    ci_path = root / ".github" / "workflows" / "ci.yml"
    if not ci_path.is_file():
        errors.append("缺少 .github/workflows/ci.yml")
    else:
        ci_text = _read(ci_path)
        try:
            ci_doc = yaml.safe_load(ci_text) or {}
        except yaml.YAMLError as exc:
            ci_doc = {}
            errors.append(f"ci.yml 不是合法 YAML:{exc}")
        errs = check_ci(ci_doc if isinstance(ci_doc, dict) else {}, ci_text)
        errors.extend(errs)
        if not errs:
            oks.append(".github/workflows/ci.yml:lint/pytest/eval/deploy 四门禁 + 密封 env")

    dockerignore = root / ".dockerignore"
    if not dockerignore.is_file():
        errors.append("缺少 .dockerignore")
    else:
        ignore_text = _read(dockerignore)
        for needle in (".venv", ".tmp", "backend/dev.db", "**/__pycache__"):
            if needle not in ignore_text:
                errors.append(f".dockerignore 未排除 {needle}")
        if ".venv" in ignore_text and "requirements.lock.txt" not in ignore_text:
            oks.append(".dockerignore:排除 .venv/.tmp/本地库,保留依赖锁")

    readme = root / "deploy" / "README.md"
    if not readme.is_file():
        errors.append("缺少 deploy/README.md")
    else:
        readme_text = _read(readme)
        if "未在本机验证" not in readme_text and "未在本机运行时验证" not in readme_text:
            errors.append("deploy/README.md 未如实标注“未在本机运行时验证”")
        if not errors:
            pass
    return errors, oks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="W12-3 交付物静态自检(无需 Docker)")
    parser.add_argument("--root", default=str(_REPO_ROOT), help="仓库根目录")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    errors, oks = lint(root)
    for line in oks:
        print(f"OK: {line}")
    if errors:
        print(f"\n校验失败:共 {len(errors)} 个错误", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"\nOK: {len(oks)} 组交付物静态校验全部通过(未做 docker 运行时验证)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
