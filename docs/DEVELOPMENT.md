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

## 平博来单与皇冠跟单开发

需求、代码核对结果与分阶段方案见 [平博来单与皇冠跟单模块设计](FOLLOW_SYSTEM_DESIGN.md)。已确认：消费已合并赛事，20 秒内同条件订单合组，按规则选择正向／反向，以及固定金额／原单合计金额比例。

跟单服务位于 `ps38app/follow/`，页面位于 `web/modules/copy/`，任务使用独立的 `follow_db_path`。规则默认停用，服务由启动入口统一启动和停止；测试可注入适配器与时钟，使用 `run_once()` 驱动确定性流程。业务分组和页面统计分组分别维护，不假设后台订单赛事 ID 与平博官方目录 ID 相同。

跟单金额口径固定为人民币。业务已确认 PS38 后台的 `THB` 标签实际表示人民币数字，源单适配将 `THB`／`RMB`／`CNY` 统一为 `CNY`，原始标签另存 `reported_currency`，不做汇率换算。规则表单不再填写币种；兼容字段 `currency` 由后端固定为 `CNY`，皇冠账号的实际报价币种仍需为人民币。历史已跳过、已完成或结果未知的任务不因该适配自动重放。

首版支持单条全局规则，并从号池自动轮转分配启用、在线且会话有效的皇冠会员；每组一次只使用一个账号，不拆单。滚球足球单关支持全场／上半场让球与大小。20 秒按原单时间合组，固定金额使用整币，比例金额向下取整；没有可用账号时进入 `WAIT_ACCOUNT`，真实结果未知时保持待核对并固定使用原账号核对。接口、核心和平台适配分别由 `test_follow_api.py`、`test_follow_core.py`、`test_follow_crown.py` 覆盖；`test_follow_integration.py` 验证真实模块与模拟平台的组合流程，页面回归在 `--browser` 模式执行。

调度线程使用 `run_once(background=True)`，只执行本地消费、冻结与排队；`dispatcher.py` 限制网络任务数量，同账号最多一个在途任务。直接调用 `run_once()` 保持同步，便于注入时钟的确定性测试。`follow_claims` 在查盘前原子领取账号与组，在写请求发送前保存 `SUBMITTING` 尝试。重启只释放确认失效的占用；尚存活的请求不因另一个实例启动或停止等待超时而失去保护。

`reconcile.py` 是不访问网络的严格核对解析；`CrownAdapter.reconcile()` 固定原账号，只查询 `get_today_wagers`，不能调用 `FT_bet`。服务对未知结果执行最多 5 次／120 秒自动核对，暂停规则也继续核对已有请求。未知结果未解除时，阻止同账号后续提交。人工登记只允许没有在途占用的 `UNKNOWN` 组，必须保存核对依据，成功需要合法且不重复的票号；与即时平台回执区分来源。操作和重试细节见 [FOLLOW_EXECUTION.md](FOLLOW_EXECUTION.md)。

队列与生命周期回归在 `test_follow_queue.py`、`test_follow_dispatcher.py`、`test_follow_lifecycle.py`，严格字段契约在 `test_follow_reconcile.py`。组合回归包含提交响应丢失后只读核对成功，必须断言写请求仍只有一次。新增状态和表均兼容原任务库；不重置游标，不重放终态或未知组。跨进程占用恢复在 Windows 使用只读进程查询，禁止用 `os.kill(pid, 0)` 检测存活。真实站点今日注单格式尚需用脱敏样本验证，未知格式应保持待核对。

`hdproc` 与外部盘口脚本只作代码参考。算法由本项目源码、订单库、映射库、账号池和配置管理；运行时不导入旧仓库或读取旧账号文件。多账号拆分、赛前及特殊玩法扩展，在设计文档中保留为后续范围；账号池自动选择与轮转已属于首版能力。

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
