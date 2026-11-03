@echo off
REM PS38 赛事独立版 — Windows 启动
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
  echo [setup] 创建虚拟环境…
  python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat

python -c "import playwright" >nul 2>nul
if errorlevel 1 (
  echo [setup] 安装依赖…
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt || goto :error
)

python -c "from playwright.sync_api import sync_playwright; import pathlib,sys; p=sync_playwright().start(); sys.exit(0 if pathlib.Path(p.chromium.executable_path).exists() else 1)" >nul 2>nul
if errorlevel 1 (
  echo [setup] 安装 Chromium（只需一次）…
  python -m playwright install chromium || goto :error
)

if not exist config.json copy config.example.json config.json >nul

python run.py
set status=%errorlevel%
echo.
echo 程序已退出（状态码 %status%）。
pause
exit /b %status%

:error
echo.
echo 启动失败，请查看上面的错误信息。
pause
exit /b 1
