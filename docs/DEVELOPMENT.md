# 开发说明

## 环境

- Python 支持版本为 3.9 及以上，推荐使用 Python 3.11 或 3.12。
- 运行依赖记录在 `requirements.txt`，开发检查工具记录在 `requirements-dev.txt`。浏览器相关功能使用 Playwright；需要首次安装 Chromium：

  ```bash
  python -m pip install -r requirements.txt
  python -m playwright install chromium
  ```

- 开发时安装完整依赖：

  ```bash
  python -m pip install -r requirements-dev.txt
  ```

- 使用项目目录内的 `.venv`，避免把开发依赖安装到系统 Python。

以下 `python` 命令均在激活 `.venv` 后执行：macOS/Linux 使用 `source .venv/bin/activate`，Windows PowerShell 使用 `.venv\Scripts\Activate.ps1`。模块职责与后续拆分边界见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 目录职责

| 目录 | 内容 |
| --- | --- |
| `ps38app/` | Python 应用代码，包括配置、HTTP 服务、订单存储、采集器、会话与赛事合并 |
| `ps38app/account_pool/` | 账号池的存储、服务、登录与心跳检测 |
| `web/` | 静态页面、原生 JavaScript ES Modules 与 CSS |
| `tests/` | 使用 unittest 的单元、接口和本地浏览器回归测试 |
| `scripts/check.py` | 与 CI 共用的跨平台开发检查入口 |
| `.github/workflows/` | Linux、macOS、Windows 单元检查，以及 Linux Chromium 回归 |
| `data/` | 本地数据库、浏览器配置和账号会话等运行数据，不应提交 |

`run.py` 是命令行入口；`Start.command`、`Start.bat` 和 `start.sh` 是跨平台启动入口。

## 测试

在项目根目录运行统一质量检查（Ruff + 完整单元测试）：

```bash
python scripts/check.py
```

需要单独运行单元测试时：

```bash
# macOS / Linux
.venv/bin/python -m unittest discover -s tests -v

# Windows PowerShell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

浏览器回归默认跳过。加 `--browser` 后会同时验证会员登录流程和真实前端页面的导航、筛选、失败提示；只使用本地模拟数据，不需要真实账号：

```bash
# macOS / Linux
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
python -m playwright install chromium
python scripts/check.py --browser
```

```powershell
# Windows PowerShell
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.playwright-browsers"
python -m playwright install chromium
python scripts/check.py --browser
Remove-Item Env:PLAYWRIGHT_BROWSERS_PATH
```

浏览器测试失败时，先确认 Chromium 已安装、`PLAYWRIGHT_BROWSERS_PATH` 指向该浏览器目录，并清理上次遗留的测试进程后重试。

`check.py` 使用调用它的 Python 解释器，并以项目根目录运行检查。默认模式会跳过浏览器回归；`--browser` 模式在未显式设置浏览器路径且项目已有 `.playwright-browsers/` 时复用该目录。安装浏览器与运行测试必须使用相同路径。

## 代码风格

- Python 使用 4 空格、双引号，格式化目标行宽 100；`.editorconfig` 与 `pyproject.toml` 记录约定。
- Ruff 版本固定在开发依赖中，当前全库检查启用 `E4`、`E7`、`E9`、`F`，检查错误导入、未定义名称等基础问题。
- 对新增或实际修改的 Python 文件使用 `python -m ruff format`。历史模块保留原有排版，格式化迁移按模块单独提交；当前统一检查不强制全库重新排版。
- 可单独执行 `python -m ruff check .`，或对新脚本检查格式：`python -m ruff format --check scripts`。

## 前端约定

- 页面通过原生 ES Modules 加载，不需要打包步骤或 Node 运行时。
- 可复用的页面逻辑放在 `web/modules/`，样式放在 `web/styles/`；模块之间使用显式 `import` / `export`。
- DOM 事件绑定与数据请求分开维护，避免把新页面逻辑继续堆进 `web/app.js`。
- 修改 API 返回结构时，同时检查使用该接口的页面模块和 `tests/` 中的服务端覆盖。

## 配置与敏感数据

- `config.example.json` 是不含真实凭证的模板。个人配置使用根目录的 `config.json`，凭证应通过本机配置或环境变量提供。
- `data/` 中的数据库、浏览器 profile、Cookie、账号密码和会话快照都属于本机数据，不能提交或复制到问题报告中。
- 提交前检查 `git status --short` 和暂存区内容。不要直接使用 `git add -A`；只暂存确认过的源代码、测试与文档。
- 根目录 `screenshots/` 和 `.playwright-mcp/` 已整体忽略，现有文件保留在本机。正式文档图片应在确认无个人数据后放入 `docs/images/`，再显式提交。

## 改动流程

1. 确认改动涉及的模块、API 和数据格式。
2. 为行为变化增加或更新聚焦测试；修复缺陷时，先覆盖触发缺陷的场景。
3. 运行相关测试，再运行 `python scripts/check.py`；页面交互、登录、浏览器会话或账号池流程变化时，再运行 `python scripts/check.py --browser`。
4. 检查 `git diff`、`git status` 和提交文件列表，确认没有配置、会话、数据库或调试产物。
5. 按职责小步提交，保留现有数据库兼容和跨平台启动行为；避免把无关重构混进功能改动。

首次提交前先检查 `git status --short --untracked-files=all`，确认只包含源代码、脱敏配置模板、测试与文档。CI 工作流会在仓库推送到 GitHub 后运行；本地检查不要求配置 Git 远程。
