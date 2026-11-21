"""W4-B 交付测试:一键起服务 / 一键评测 / 回滚演练 / README 数字可复算。

约定(见 00_通用约定 §6):
- 所有临时文件都落在 conftest 的 ``tmp_dir``(ROOT/.tmp/w1a_*,teardown 清理);
  不用 pytest ``tmp_path``:pytest 以 0o700 创建 basetemp,在 DSH 沙盒 ACL 下创建后
  不可再访问(详见 backend/tests/test_services.py 同名说明);
- 子进程不建管道(Windows 沙箱会拒 CreatePipe),stdout/stderr 一律重定向到文件;
- 起服务用 ``cmd /c serve.bat`` + 新进程组,停止用 CTRL_BREAK_EVENT 发整个进程组;
- 不读/不使用项目 .env 里的真实 API key(测试环境显式隔离)。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IS_WINDOWS = os.name == "nt"
# CI 可移植性修正(2026-11-21 首次真 CI 跑出来):原先硬编码项目 .venv 解释器,
# GitHub runner 上**没有 .venv**(用 setup-python 的系统解释器)→ FileNotFoundError。
# 改为**优先当前解释器**(sys.executable),本地 venv 里跑时它本身就是 venv python;
# 仅当当前解释器不可用时才回退去找 .venv。这与 gen_readme_numbers.py 的口径一致。
VENV_PYTHON = Path(sys.executable) if sys.executable else ROOT / ".venv" / "Scripts" / "python.exe"
if not VENV_PYTHON.is_file():
    for _cand in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if _cand.is_file():
            VENV_PYTHON = _cand
            break


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _tail(path: Path, lines: int = 30) -> str:
    if not path.is_file():
        return "(无日志文件)"
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def _run_to_file(
    cmd: list[str],
    log_path: Path,
    cwd: Path = ROOT,
    timeout: float = 300,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """跑子进程,stdout+stderr 重定向到文件(沙箱禁用管道)。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    full_env = os.environ.copy()
    full_env["TMP"] = str(ROOT / ".tmp")
    full_env["TEMP"] = str(ROOT / ".tmp")
    if env:
        full_env.update(env)
    with log_path.open("wb") as log:
        proc = subprocess.run(
            cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env=full_env, timeout=timeout
        )
    return proc.returncode, log_path.read_text(encoding="utf-8", errors="replace")


def _bat_cmd(bat: Path, *args: str) -> list[str]:
    """构造 ``cmd /c call "路径" 参数...``。

    项目路径含中文与空格(``售后 Copilot``):直接 ``cmd /c "a b\\x.bat" 参数``
    会被 cmd 在空格处拆词(报 "D:\\...\\售后 不是内部或外部命令");
    用 ``call "路径"`` 形式让引号保留在正确位置。
    """
    return ["cmd", "/c", "call", str(bat), *args]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=2
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _wait_health(port: int, timeout: float = 120) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ok(port):
            return True
        time.sleep(0.3)
    return False


def _stop_process_group(proc: subprocess.Popen) -> None:
    """向 cmd + uvicorn 整个进程组发 CTRL_BREAK(沙箱内 taskkill 不可用)。"""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            proc.terminate()
    except (OSError, AttributeError):
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _load_rollback():
    path = ROOT / "backend" / "scripts" / "rollback.py"
    spec = importlib.util.spec_from_file_location("w4b_rollback", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sqlite_table_exists(db_path: Path, table: str) -> bool:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    return row is not None


def _sqlite_column_exists(db_path: Path, table: str, column: str) -> bool:
    # 表名必须加引号:`order` 是 SQL 关键字,不加引号 PRAGMA 会报 near "order" 语法错。
    # (F1 新增 0011 断言 order.delivered_at 时踩到;此前只查过 ticket/refund_request 等非关键字。)
    safe = table.replace('"', '""')
    with sqlite3.connect(str(db_path)) as conn:
        return column in [
            row[1] for row in conn.execute(f'PRAGMA table_info("{safe}")')
        ]


def _markers() -> tuple[str, str]:
    return "<!-- gen_readme_numbers:begin -->", "<!-- gen_readme_numbers:end -->"


def _extract_block(text: str) -> str:
    begin, end = _markers()
    start = text.index(begin)
    finish = text.index(end, start) + len(end)
    return text[start:finish]


# ---------------------------------------------------------------------------
# 1) 一键起服务:BAT -> /health 200 -> 停
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not IS_WINDOWS, reason="serve.bat 仅在 Windows 验证")
def test_serve_bat_health_and_seed(tmp_dir):
    serve_bat = ROOT / "serve.bat"
    assert serve_bat.is_file(), "缺少 serve.bat"

    port = _free_port()
    db_path = tmp_dir / "serve.db"
    log_path = tmp_dir / "serve.log"
    env = {
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "SERVE_HOST": "127.0.0.1",
        "SERVE_PORT": str(port),
    }
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            _bat_cmd(serve_bat),
            cwd=ROOT,
            env={**os.environ, **env, "TMP": str(ROOT / ".tmp"), "TEMP": str(ROOT / ".tmp")},
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    try:
        assert _wait_health(port), (
            "serve.bat 未能在 120s 内提供 /health 200:\n" + _tail(log_path)
        )
        assert _health_ok(port)
        assert "地址: http://127.0.0.1" in log_path.read_text(encoding="utf-8", errors="replace")
        # seed 若为空库应已装载:1 merchant / 30 order
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM merchant").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM \"order\"").fetchone()[0] == 30
    finally:
        _stop_process_group(proc)

    # 停止后端口应不再响应(确认没有残留 uvicorn)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _health_ok(port):
        time.sleep(0.25)
    assert not _health_ok(port), "CTRL_BREAK 后 uvicorn 仍在响应 /health"


# ---------------------------------------------------------------------------
# 2) serve.sh / run_eval.sh 语法
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("bash") is None, reason="环境无 bash")
def test_shell_scripts_syntax(tmp_dir):
    log_path = tmp_dir / "bash_n.log"
    for name in ("serve.sh", "run_eval.sh"):
        script = ROOT / name
        assert script.is_file(), f"缺少 {name}"
        code, output = _run_to_file(
            [shutil.which("bash") or "bash", "-n", str(script)], log_path
        )
        assert code == 0, f"{name} 语法检查失败:\n{output}"
    for name in ("serve.sh", "run_eval.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert ".venv" in text and "python" in text


# ---------------------------------------------------------------------------
# 3) 一键评测:run_eval.bat --smoke 跑通并产出报告
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not IS_WINDOWS, reason="run_eval.bat 仅在 Windows 验证")
def test_run_eval_bat_smoke(tmp_dir):
    run_eval_bat = ROOT / "run_eval.bat"
    assert run_eval_bat.is_file(), "缺少 run_eval.bat"
    out_dir = tmp_dir / "reports"
    work_dir = tmp_dir / "eval_work"
    log_path = tmp_dir / "run_eval.log"
    cmd = _bat_cmd(
        run_eval_bat,
        "--smoke",
        "--repeat",
        "1",
        "--run-id",
        "w4b-test-delivery-smoke",
        "--out-dir",
        str(out_dir),
        "--work-dir",
        str(work_dir),
    )
    code, output = _run_to_file(cmd, log_path, timeout=300)
    assert code == 0, "run_eval.bat --smoke 失败:\n" + output[-3000:]

    report_path = out_dir / "w4b-test-delivery-smoke.json"
    assert report_path.is_file(), f"未找到报告 {report_path}\n{output[-2000:]}"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meta"]["mode"] == "mock"
    assert report["meta"]["cases_selected"] == 5
    assert report["meta"]["repeat"] == 1
    assert report["metrics"]["pass_rate"]["denominator"] == 5
    assert report["metrics"]["critical_fail_count"] == 0


# ---------------------------------------------------------------------------
# 4) 回滚演练:升级 -> 回滚(带备份) -> 再升级
# ---------------------------------------------------------------------------

def test_rollback_drill(tmp_dir):
    rollback = _load_rollback()
    db_path = tmp_dir / "rollback.db"
    db_url = f"sqlite:///{db_path.as_posix()}"

    up = rollback.upgrade_database(db_url)
    assert up["current_revision"] == "0011_order_delivered_at"
    assert up["integrity_check"] == "ok"
    assert up["foreign_key_violations"] == 0
    assert _sqlite_table_exists(db_path, "memory")
    assert _sqlite_column_exists(db_path, "ticket", "session_id")
    assert _sqlite_table_exists(db_path, "auth_user")
    assert _sqlite_table_exists(db_path, "kb_vector")  # W9 新增 0007
    assert _sqlite_table_exists(db_path, "job_queue")  # W10 新增 0008
    assert _sqlite_table_exists(db_path, "turn_audit")  # W10 新增 0008
    assert _sqlite_table_exists(db_path, "refund_item")  # W11 新增 0009
    assert _sqlite_column_exists(db_path, "refund_request", "tracking_no")  # W11
    assert _sqlite_column_exists(db_path, "ticket", "sla_breached")  # W11
    assert _sqlite_table_exists(db_path, "badcase")  # W12 新增 0010
    # F1 新增 0011:只加列不建表
    assert _sqlite_column_exists(db_path, "order", "delivered_at")

    down = rollback.downgrade_database(db_url, "0003", backup_dir=tmp_dir / "backup")
    assert down["target"] == "0003_ticket_summary"
    assert down["current_revision"] == "0003_ticket_summary"
    assert down["integrity_check"] == "ok"
    assert down["foreign_key_violations"] == 0
    assert Path(down["backup"]).is_file() and Path(down["backup"]).stat().st_size > 0
    assert not _sqlite_table_exists(db_path, "memory")
    assert not _sqlite_column_exists(db_path, "ticket", "session_id")
    assert not _sqlite_table_exists(db_path, "auth_user")
    assert not _sqlite_table_exists(db_path, "kb_vector")
    assert not _sqlite_table_exists(db_path, "job_queue")
    assert not _sqlite_table_exists(db_path, "turn_audit")
    assert not _sqlite_table_exists(db_path, "refund_item")
    assert not _sqlite_table_exists(db_path, "badcase")  # W12 0010 一并回滚
    # F1 0011 一并回滚(batch_alter_table 兼容 SQLite 的 DROP COLUMN)
    assert not _sqlite_column_exists(db_path, "order", "delivered_at")

    up2 = rollback.upgrade_database(db_url)
    assert up2["current_revision"] == "0011_order_delivered_at"
    assert _sqlite_column_exists(db_path, "order", "delivered_at")  # F1 再升级后列回来
    assert _sqlite_table_exists(db_path, "memory")
    assert _sqlite_column_exists(db_path, "ticket", "session_id")
    assert _sqlite_table_exists(db_path, "auth_user")
    assert _sqlite_table_exists(db_path, "kb_vector")
    assert _sqlite_table_exists(db_path, "job_queue")
    assert _sqlite_table_exists(db_path, "refund_item")


# ---------------------------------------------------------------------------
# 5) README 数字与 gen_readme_numbers.py 输出逐字一致
# ---------------------------------------------------------------------------

def test_readme_numbers_match_script(tmp_dir):
    gen = ROOT / "backend" / "scripts" / "gen_readme_numbers.py"
    assert gen.is_file(), "缺少 gen_readme_numbers.py"
    log_path = tmp_dir / "gen_numbers.log"
    env = {"PYTHONPATH": str(ROOT / "backend")}
    code, output = _run_to_file(
        [str(VENV_PYTHON), str(gen)], log_path, timeout=180, env=env
    )
    assert code == 0, "gen_readme_numbers.py 运行失败:\n" + output[-3000:]
    generated = _extract_block(output)

    readme = (ROOT / "backend" / "README.md").read_text(encoding="utf-8")
    assert "gen_readme_numbers.py" in readme
    pasted = _extract_block(readme)
    assert pasted == generated, (
        "README 数字与脚本输出不一致(请重跑 gen_readme_numbers.py 并原样粘贴):\n"
        f"--- README ---\n{pasted}\n--- 脚本 ---\n{generated}"
    )


# ---------------------------------------------------------------------------
# 6) README 必备章节 / 声明
# ---------------------------------------------------------------------------

def test_readme_required_sections():
    readme = (ROOT / "backend" / "README.md").read_text(encoding="utf-8")
    required = {
        "五条件对照": "五条件",
        "三场景": "三场景",
        "架构图(文字版)": "架构",
        "不做清单": "不做清单",
        "快速开始": "快速开始",
        "一键起服务命令": "serve.bat",
        "一键评测命令": "run_eval.bat",
        "MOCK 声明清单": "MOCK 声明",
        "扩展点清单": "扩展点",
        "merchant_id 扩展点": "merchant_id",
        "向量检索扩展点": "向量检索",
        "pricing.* 命名空间": "pricing.*",
        "已知声明 1(Ticket 已有 session_id)": "Ticket 已有 session_id",
        "已知声明 2(usage 估算)": "usage",
        "数字生成命令": "gen_readme_numbers.py",
        "回滚脚本": "rollback.py",
    }
    missing = [label for label, needle in required.items() if needle not in readme]
    assert not missing, "README 缺少必备内容:" + "、".join(missing)


# ---------------------------------------------------------------------------
# 7) 一键脚本/回滚/数字脚本不读取真实 key
# ---------------------------------------------------------------------------

def test_entrypoints_do_not_read_secrets():
    files = [
        ROOT / "serve.bat",
        ROOT / "serve.sh",
        ROOT / "run_eval.bat",
        ROOT / "run_eval.sh",
        ROOT / "backend" / "scripts" / "gen_readme_numbers.py",
        ROOT / "backend" / "scripts" / "rollback.py",
    ]
    # 用词边界匹配 .env:普通 ``os.environ.get(...)`` 前缀也含 ".env" 字样,
    # 那是读进程环境变量,不是读项目 .env 文件,不能算命中。
    forbidden = {
        ".env 文件": r"\.env\b",
        "load_dotenv": r"load_dotenv",
        "dotenv": r"dotenv",
        "DEEPSEEK": r"DEEPSEEK",
        "OPENAI": r"OPENAI",
        "ANTHROPIC": r"ANTHROPIC",
        "API_KEY": r"API_KEY",
    }
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [label for label, pattern in forbidden.items() if re.search(pattern, text)]
        assert not hits, f"{path.name} 引用了敏感配置:{hits}"
