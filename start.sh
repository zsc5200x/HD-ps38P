#!/bin/bash
# PS38 赛事独立版 — Linux / macOS 终端启动
set -eu
cd "$(dirname "$0")"

# 项目内已下载的浏览器优先使用（避免重复下载到 ~/Library/Caches）
if [ -d .playwright-browsers ]; then
  export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
fi

PYTHON="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q -r requirements.txt
if ! python - <<'CHECK' >/dev/null 2>&1
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    raise SystemExit(0 if Path(pw.chromium.executable_path).exists() else 1)
CHECK
then
  python -m playwright install chromium
fi
[ -f config.json ] || cp config.example.json config.json
exec python run.py "$@"
