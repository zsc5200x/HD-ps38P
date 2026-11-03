#!/bin/bash
# PS38 赛事独立版 — macOS 双击启动
set -u
cd "$(dirname "$0")" || exit 1

# 项目内已下载的浏览器优先使用（避免重复下载到 ~/Library/Caches）
if [ -d .playwright-browsers ]; then
  export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
fi

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "找不到 python3，请先安装 Python 3.9 或更高版本。"
  read -r -n 1 -p "按任意键退出…"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "[setup] 创建虚拟环境…"
  "$PYTHON" -m venv .venv || exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import playwright" >/dev/null 2>&1; then
  echo "[setup] 安装依赖…"
  python -m pip install --upgrade pip >/dev/null
  python -m pip install -r requirements.txt || exit 1
fi

if ! python - <<'PY' >/dev/null 2>&1
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    raise SystemExit(0 if Path(pw.chromium.executable_path).exists() else 1)
PY
then
  echo "[setup] 安装 Chromium（只需一次，可能需要几分钟）…"
  python -m playwright install chromium || exit 1
fi

if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "[setup] 已生成 config.json；如需自动填账号密码，请编辑它。"
fi

python run.py
status=$?
echo
echo "程序已退出（状态码 $status）。"
read -r -n 1 -p "按任意键关闭窗口…"
exit "$status"
