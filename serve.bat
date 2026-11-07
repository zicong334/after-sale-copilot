@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 一键起服务:迁移 -> 空库 seed -> uvicorn(无 key 可起)
rem 用法:serve.bat            (默认 127.0.0.1:8000)
rem       set SERVE_PORT=8080 && serve.bat
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [serve] 未找到 %PY%
  echo [serve] 请先在项目根创建 .venv 并安装 requirements.lock.txt
  exit /b 1
)

set "PYTHONPATH=%ROOT%backend;%PYTHONPATH%"
if "%SERVE_HOST%"=="" set "SERVE_HOST=127.0.0.1"
if "%SERVE_PORT%"=="" set "SERVE_PORT=8000"

echo [serve] Alembic upgrade head ...
"%PY%" -m alembic -c "%ROOT%backend\alembic.ini" upgrade head
if errorlevel 1 (
  echo [serve] 数据库迁移失败
  exit /b 1
)

rem 仅当 merchant 表为空时才 seed,避免每次启动覆盖已有业务数据。
"%PY%" -c "from app.db import SessionLocal;from app.models import Merchant;from sqlalchemy import select,func;s=SessionLocal();n=s.scalar(select(func.count()).select_from(Merchant));s.close();raise SystemExit(3 if n==0 else 0)"
if errorlevel 3 (
  echo [serve] 空库,装载 seed 数据 ...
  "%PY%" "%ROOT%backend\scripts\seed.py"
  if errorlevel 1 (
    echo [serve] seed 失败
    exit /b 1
  )
) else (
  if errorlevel 1 (
    echo [serve] 数据库检查失败
    exit /b 1
  )
)

echo [serve] 地址: http://%SERVE_HOST%:%SERVE_PORT%
echo [serve] 健康检查: http://%SERVE_HOST%:%SERVE_PORT%/health
echo [serve] Ctrl+C 停止
"%PY%" -m uvicorn app.main:app --host "%SERVE_HOST%" --port "%SERVE_PORT%"
exit /b %ERRORLEVEL%
