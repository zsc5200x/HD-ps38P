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

需求、代码核对结果与分阶段方案见 [平博来单与皇冠跟单模块设计](FOLLOW_SYSTEM_DESIGN.md)。主页按 20 秒窗口生成展示批次；跟单服务以该批次定位来单，首单进入后固定等待 10 秒再冻结推送，窗口不因后续订单延长。

跟单服务位于 `ps38app/follow/`，页面位于 `web/modules/copy/`，任务使用独立的 `follow_db_path`。规则默认停用，服务由启动入口统一启动和停止；测试可注入适配器与时钟，使用 `run_once()` 驱动确定性流程。跟单批次与主页展示聚合各自维护，不假设后台订单赛事 ID 与平博官方目录 ID 相同。

跟单金额口径固定为人民币。业务已确认 PS38 后台的 `THB` 标签实际表示人民币数字，源单适配将 `THB`／`RMB`／`CNY` 统一为 `CNY`，原始标签另存 `reported_currency`，不做汇率换算。规则表单不再填写币种；兼容字段 `currency` 由后端固定为 `CNY`，皇冠账号的实际报价币种仍需为人民币。历史已跳过、已完成或结果未知的任务不因该适配自动重放。

当前支持单条全局规则，并从号池自动轮转分配启用、在线且会话有效的皇冠会员；首单进入跟单服务后开启固定 10 秒待跟单窗口，同批次后续订单并入但不重置倒计时；满 10 秒冻结成员与合计金额并生成独立跟单批次号（首个沿用页面 8 位号，之后 `-2`、`-3`），批次金额可按规则分单。冻结后到达的订单进入下一批重新计时，已处理源单不会重复跟单；窗口状态持久化，重启后按原截止时间恢复。主页「跟单视角」自身仍按 20 秒窗口生成展示批次；规则关闭时只登记批次并显示“跟单未开启”，不发请求。足球单关支持全场／上半场让球与大小；`follow_allow_prematch` 默认开启，关闭后仅处理滚球。固定金额使用整币，比例金额向上取整；没有可用账号时进入 `WAIT_ACCOUNT`。接口、核心和平台适配分别由 `test_follow_api.py`、`test_follow_core.py`、`test_follow_crown.py` 覆盖；`test_follow_integration.py` 验证真实模块与模拟平台的组合流程，页面回归在 `--browser` 模式执行。

调度线程使用 `run_once(background=True)`，只执行本地消费、冻结与排队；`dispatcher.py` 限制网络任务数量，同账号最多一个在途任务。直接调用 `run_once()` 保持同步，便于注入时钟的确定性测试。`follow_claims` 在查盘前原子领取账号与组，在写请求发送前保存 `SUBMITTING` 尝试。重启只释放确认失效的占用；尚存活的请求不因另一个实例启动或停止等待超时而失去保护。

皇冠报表服务 `crown_reports.py` 随程序启动和停止，把历史注单按账号与票号幂等同步至 `data/crown_reports.sqlite3`；`GET /api/crown-reports` 读取本地明细、汇总和同步状态。需要手动补采时使用 `scripts/sync_crown_reports.py`，测试只调用模拟平台。

推送服务 `notify.py` 随 `run.py` 启动和停止，设置页保存后即时生效；机器人配置和已送达消息保存在 `data/notify.json`，未启用时不会发起网络请求。`tests/test_notify.py` 覆盖消息版式、配置校验、按钮回调以及发送一次并就地更新结果。

分单与限额：`rules.split_stakes` 把组金额按整数均分（总和不变，每份至少 1 元），服务按 `split_delay_seconds` 顺序提交；全部份成功才是 `SUCCEEDED`，部分成功写入新状态 `PARTIAL`，每份票号保存在 `follow_parts`。单场／盘口限额在冻结阶段按「已成功金额 + 在途预留」计算，剩余额度不足最小下注时跳过该组。

原单校验只比对金额、赔率、盘口线、方向、时段、比分和赛事身份；页面辅助字段刷新不会误跳过。皇冠 `FT_order_view` 返回 `VariableStandard` 时，先做 `right_menu → sport_menu → order` 菜单预热再复核；仍为变盘则按可重试错误返回，在最长原单年龄内继续重试，不盲目提交（皇冠对未复核的 FT_bet 会返回 betError000）。 变盘重试时会轮换未试过的皇冠会员账号。提交前先只读保存今日票号基线；提交响应中断（`UNCERTAIN_RESULT`）后查询 `get_today_wagers`，仅接受基线之外、GID 和精确金额唯一匹配且未被其他组或分单使用的新票。缺少基线、查询失败、无匹配或多匹配均保留失败说明并要求人工核实，不能以“未找到”证明未成交。 「输赢结果」弹窗只显示皇冠回执号对应的结算：按票号调用皇冠 `get_history_data` + `history_switch`（按皇冠日期，票号可带 OU 前缀），显示联赛、对阵、玩法、水位、盈利和赛果；分单时汇总所有票号的盈利，不显示单个回执号；没有皇冠票号时明确提示“未跟单成功”，不再退回平博原单的输赢。

当前版本保留提交响应中断后的即时只读核对，已移除旧版后台核对任务、页面人工登记及对应接口，`reconcile.py` 与 `test_follow_reconcile.py` 不再存在。没有有效成功回执且未能唯一核对到新票的提交记录为 `REJECTED`；这包括响应不明确的情况，因此“失败”不能作为平台未成交的证明，应到平台核实。旧库的 `UNKNOWN` 与核对审计仍保留兼容，但不会自动查询或重发。操作和重试细节见 [FOLLOW_EXECUTION.md](FOLLOW_EXECUTION.md)。

队列与生命周期回归在 `test_follow_queue.py`、`test_follow_dispatcher.py`、`test_follow_lifecycle.py`，分单与限额回归在 `test_follow_split.py`。新增状态和表兼容原任务库；不重置游标，不重放终态或历史未知组。跨进程占用恢复在 Windows 使用只读进程查询，禁止用 `os.kill(pid, 0)` 检测存活。

`hdproc`、`跟单引擎/` 与外部盘口脚本只作代码参考。算法由本项目源码、订单库、映射库、账号池和配置管理；运行时不导入旧仓库或读取旧账号文件。账号池自动轮转、同账号顺序分单和赛前开关已实现；同组跨账号分单及特殊玩法仍属后续范围。

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

推送跟单结束后只编辑原卡片（更新「跟单成功」金额和水位），不再追加分隔条、结果块或票号。
