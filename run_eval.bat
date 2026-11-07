@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 一键评测:调 backend/scripts/run_eval.py(FakeLLM + FakeJudge,无 key)
rem 用法:run_eval.bat                      (48 条 x repeat=3)
rem       run_eval.bat --smoke             (golden 前 5 条)
rem       run_eval.bat --layer adversarial --repeat 1
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [run_eval] 未找到 %PY%
  exit /b 1
)

set "PYTHONPATH=%ROOT%backend;%PYTHONPATH%"
"%PY%" "%ROOT%backend\scripts\run_eval.py" %*
exit /b %ERRORLEVEL%
