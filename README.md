# HD PS38 Sports（PS38 赛事独立版）

从 HD 系统里抽出来的 **PS38 赛事（我的赛事）** 单页应用：包含前端页面、后台订单拉取、独立会话维护和本地 SQLite 数据库。

整个文件夹可以直接复制到另一台机器运行，**不依赖 hdproc、不引用原仓库路径**，也不需要原项目的 Chrome/CDP 环境。

使用入口：[快速开始](#快速开始) · [配置](#配置) · [开发与检查](docs/DEVELOPMENT.md) · [模块职责](docs/ARCHITECTURE.md)

## 这个版本有什么 / 没有什么

**有**

- 「我的赛事」页面：投注总量、已结算输赢、未结算/已结算统计、各代理卡片
- 订单表：联赛、比赛、盘口、水位、金额、输赢、订单号、代理、会员、结果、时间
- 筛选：未结算/已结算/全部、代理、关键字、日期、快捷时间、GMT-4 / GMT+8、下单/结算口径
- 比赛视角：按比赛聚合，点开看盘口分组和逐单明细（含 20 秒同盘口合并）
- 跟单视角：按推送合并规则把「同场 + 同盘口 + 同线 + 同比分、且距组内首单 20 秒内」的订单并成一组，显示合并金额、单数、会员数、耗时与水位区间
- 后台订单拉取：代理后台 `bet-list/v2`（OPEN 约 1 秒一轮，SETTLED 约 5 分钟一轮）
- 首次运行自动补采历史已结算订单，也可手动点「补历史」
- 独立浏览器会话：首次登录后保存 Cookie，重启免登录；失效时自动保留窗口等待重新登录/2FA
- 「主页」顶部的 SYSTEM 状态卡：在线会话、滚球场次、最近响应耗时、数据源，附轮询耗时迷你图；卡片底部就是「登录后台」按钮（把后台浏览器窗口弹到前台，不清 Cookie）
- 设置页按「系统中心」版式做成三栏策略配置：左栏「基础跟单 / 玩法比例 / 后台过滤 / 反单规则 / 单账号比例」，中栏「消息通知配置」，右栏「TG 信号接入 + 实时记录」；已接通的跟单字段（模式、金额、账号、方向、时效、水位、来源会员、暂停/保存/保存并启用）可编辑，尚未实现的后台能力以「待接入」占位，不会提交；桌面端整页锁定在一屏内，三栏各自滚动，系统信息收进右上角「系统信息」弹窗（采集策略 / 数据与存储 / 会话与运行，含立即刷新 / 补历史 / 重新登录 / 登录后台），修改仍需编辑 `config.json` 并重启
- 顶部气泡导航：`主页` / `PS38 跟单` / `赛事合并` / `号池管理` / `设置`，左侧保留采集状态；支持 `#/home`、`#/copy`、`#/merge`、`#/pool`、`#/settings` 直接打开
- 「赛事合并」页：读取皇冠和平博当前赛事，在已绑定联赛内按队名、主客顺序和开赛时间对照；订单只补充统计，支持人工确认和严格自动匹配，映射保存在 `data/fixture_merge.sqlite3`
- 「联赛库」：内置 243 条来自 hdproc 的已知皇冠 ↔ 平博联赛配对，首次启动导入后持久化；表内可按联赛名/皇冠 ID/平博 ID 搜索、逐条解绑
- 所有页面共用一套 `page-shell` 公共壳：顶部导航 + 一条全局标题栏（页面名与操作按钮随路由切换），桌面端锁定视口高度，页面内容在壳内滚动、不再整页往下超；底部固定状态栏显示系统状态、系统时间、运行时长、版本号与数据同步状态（`web/modules/statusbar.js`）；窄屏自动回退为普通文档滚动
- 「PS38 跟单」页：左侧「实时性能看板」展示会员在线／空闲／执行中／掉线、任务队列、累计成功率、处理耗时与当前跟单参数，「会员执行池」列出皇冠会员账号、最近执行与平均耗时；右侧为跟单记录卡片（状态色条、投注单数／金额／方向／水位／错误分类）与「执行日志 / 状态时间线」，可按成功、失败、重试、异常筛选。业务上按已确认赛事映射处理滚球足球单关，20 秒持久化合组，支持正向／反向、固定金额／合计金额比例；默认展示有成功回执的订单，可查处理中、失败和待核对记录
- 「号池管理」页：支持 PS38 后台 / 皇冠后台 / 皇冠会员账号的登录、会话保存、单账号与批量心跳检测、启停和清除会话；账号池默认 60 秒检测一次
- 新单到达时页面顶部提示，并自动刷新列表

**没有**

- 不支持改单、撤单、串关、角球或赛前跟单；第一版跟单范围为滚球足球全场／上半场让球和大小
- 除可选的「赛事合并」和号池的皇冠登录/心跳外，不依赖皇冠赔率采集、Telegram 推送、HD WebUI；号池逻辑是按 `hdproc` 协议重写的本地实现，不引用它的运行时代码
- 赛事合并本身不写入订单库或皇冠数据，只保存人工确认或符合严格规则的赛事映射
- 不包含原系统的登录账号、Cookie 或历史订单，需要你自己登录/导入
- 号池不会在会话失效后自动重登；检测到失效后需要在页面点「登录」手动恢复（避免反复提交验证码/2FA）

> 「PS38 跟单」规则默认停用，需要在页面明确设置金额和皇冠会员账号后启用。金额统一按人民币计算，PS38 后台的 THB 标签按人民币原数字处理，不做汇率换算。首次启用从当前新单事件位置开始，不重放历史订单；结果不明确的请求保留为“待核对”，不会自动重复提交。

## 从原系统抽出的逻辑（对照表）

这个目录不是重新实现的一套接口，而是把原项目里「我的赛事」相关的文件抽出来、去掉其它模块依赖后拼成的独立应用。对应关系：

| 本目录 | 原系统位置 | 说明 |
| --- | --- | --- |
| `web/` | `ps3838/dashboard/static/index.html`、`modules/my-events/page.js`、`bindings.js`、`styles/*` | 抽取订单看板后扩展独立的合并、联赛库和号池页面 |
| `ps38app/server.py` | `ps3838/dashboard_server.py` 的 `_my_events*`、`_partner_connection` | REST 查询 + 静态页面，并提供本地合并与号池接口 |
| `ps38app/store.py` | `ps3838/partner_orders.py` 的 `parse_page`、`parse_order`、`OrderStore`、`query_*` | 表结构与原库完全一致，可直接导入旧库 |
| `ps38app/poller.py` | `ps3838/partner_orders_poll.py` + `backfill_partner_orders.py` | OPEN 约 1 秒、SETTLED 约 5 分钟，分页严格校验后整轮提交 |
| `ps38app/session.py` | `src/hdzz/core/watcher/ps3838_watch.py`、`standalone/HD PS38/ps38/client.py` | 独立持久化浏览器、登录、2FA 等待、Cookie 快照、会话失效恢复 |
| `ps38app/config.py` | `ps3838/project_config.py` 的 partner 配置 | 只保留本应用需要的字段 |
| `ps38app/account_pool/` | `src/hdzz/core/member/keeper.py`、`ps3838/crown/crown_local/backend/auth.py`、`crown_member.py`、`src/hdzz/core/watcher/ag_watcher.py` | 账号池、三类登录、会话保存与心跳的本地重写；不依赖 hdproc 运行时 |
| `web/modules/pool.js`、`web/styles/account-pool.css` | `app/webui` 号池页 | 账号列表、登录/检测/清除、心跳状态和新增账号对话框 |

没有搬过来的：皇冠赔率采集、Telegram 推送、`local_signal_push`、HD 跟单引擎、HD WebUI 的同源 `/ps3838/` 代理。当前联赛/赛事合并使用本项目的独立实现。号池只做登录、会话保存和只读心跳，不包含下注或跟单代码。

跟单由本项目 `ps38app/follow/` 独立实现，参考旧系统的平博意图、盘口解析和结果判定逻辑，使用本项目账号池和 `data/follow.sqlite3`。不需要挂载旧硬盘或运行 `hdproc`。设计及实现边界见 [跟单模块文档](docs/FOLLOW_SYSTEM_DESIGN.md)。

## PS38 跟单记录

在 `#/copy` 打开跟单页，点右上角「跟单规则」会跳到「设置」页顶部的规则面板；选择正向或反向、固定金额或比例和皇冠会员账号，保存后按需启用。首版同一时间使用一条全局规则、一个目标账号，可按会员筛选来源；多账号分配与拆单不在首版范围。

- 20 秒窗口按原单时间固定，按同场、同玩法、同方向、同盘口、同时段和同比分合组；每组保存完整原单与规则版本。PS38 原单的 THB、RMB、CNY 标签均按人民币计算，例如 `100 THB` 按 100 元计入；金额不换算，原始标签保留在明细中。
- 固定金额为每组合计处理一次；比例以百分比输入，如 `10` 表示原单合计的 10%。首版使用整币，比例结果向下取整，小于 1 时跳过。
- 网络执行由有界后台任务承接，同一账号同时只执行一组；查盘与等待回执期间继续接收、合组新单。记录显示等待原因、重试次数和下次动作时间。
- 只有已确认未发送的临时故障，或无票号的明确白名单拒绝，才允许退避后重新查盘；每组最多 3 次提交尝试，始终保留原组金额、成员和规则。
- “已成功”需要明确平台回执、严格只读核对结果，或有票号及依据的人工核对登记。人工登记会单独标注来源。列表区分提交金额／水位与实际确认值，平台未回显或人工未填写时保留为空。
- “待核对”表示可能已经被接收但结果无法确定。系统最多自动只读查询 5 次、持续不超过 120 秒；未确认时保持待核对，可再发起只读核对或人工登记。该账号后续提交等待原结果解除，仍受每组原始时效限制；未知请求不会重新提交或切换账号重做。
- 暂停阻止后续处理；已经发出的请求仍需保存最终返回。首次启用、暂停后重新启用与历史补采均不补处理停用期间的存量订单。
- 原单不会因已确认的 THB/CNY 标签差异被跳过。皇冠目标账号仍须实际使用人民币；映射缺失、主客顺序证据不足、盘口不唯一、比分变化和超时等原因会显示在记录中。

本地回归使用模拟响应验证流程，没有使用真实资金验证成交。真实接口字段变化应按待核对处理，并用脱敏响应补充兼容测试。

队列、重试、核对操作与排错见 [跟单执行与核对](docs/FOLLOW_EXECUTION.md)。

如果以后平博/PS3838 改了接口，通常只需要改 `ps38app/session.py` 里的三个路径和 `ps38app/store.py` 里的字段映射。

## 号池管理（账号池）

号池页直接管理三类账号，登录与心跳逻辑参考 `hdproc` 的 PS38 / 皇冠实现后重写：

| 类型 | 登录方式 | 会话保存 | 心跳检测 |
| --- | --- | --- | --- |
| PS38 后台 | Playwright 登录/2FA；`config.json` 主账号由采集器维护 | `data/accounts/<id>/profile/storage_state.json` | 后台 profile API |
| 皇冠后台 | 可见 Chromium 手动登录，可选自动填账号密码 | `data/accounts/<id>/profile/storage_state.json` | 无头浏览器检查后台登录页/已登录界面 |
| 皇冠会员 | 可见 Chromium 登录，监听真实登录响应；支持 iframe、验证码等待与已有会话恢复 | 登录后初始化并稳定采样，接口验证通过后原子保存 `storage_state.json` + `session_meta.json` | 会员站 `get_member_data`；缺少 uid/ver 时提示登录补全，不凭页面文字判定在线 |

- 账号配置在 `data/accounts.sqlite3`；Cookie、uid、密码不会出现在 `/api/pool` 返回里。
- 密码是可选的：填写后只写入 `data/accounts/<id>/credentials.json`（0600），用于自动填充；留空则每次手动登录。
- 每个账号在页面上是一张心跳卡片：状态、会话、最近响应/检测、24 格心跳条和登录/检测/清理操作；旧库首次启动会用运行日志回填心跳历史。
- 号池工作台支持按账号名/域名搜索、按状态筛选，以及默认（主账号优先）/状态/最近检测/账号名称/账号类型排序；卡片区按屏幕宽度自适应，桌面最多 5 列、平板 3 列、手机 1 列。
- 心跳默认每 60 秒一次（新增账号时可设 15–3600 秒）；「全部检测」逐账号串行检查，避免同时拉起多个浏览器。
- 采集器主账号启动时自动登记，登录/检测复用采集器现有会话，不会另开第二个 PS38 浏览器。
- 会话失效只标记「已失效」，不会自动重登；点行内「登录」先验证已有会员会话，有效时直接恢复；失效时打开浏览器，每次任务最多自动提交一次账号密码，验证码由操作者完成。
- 皇冠会员登录与 hdproc 对齐的关键步骤：捕获 `chk_login*` 成功响应中的 uid 和真实请求版本/站点 → `memSet/check`、`service_mainget/login=Y`、`get_version` 初始化 → Cookie/uid/ver 连续三次稳定采样 → `get_member_data` 验证 → 保存 Cookie、localStorage、uid/ver/langx、实际 origin 与浏览器 User-Agent。关闭登录浏览器后使用磁盘快照检测，程序重启后仍可复用。
- 「已保存」表示本地存在会话快照，「在线」表示本次接口验证通过。网络问题保留快照，明确失效/重复登录则提示重新登录；未通过验证的登录不会覆盖旧快照。
- 皇冠会员线路可能跳转至 IP 地址，且证书过期或域名不匹配。`pool_crown_ignore_https_errors` 默认 `true`，与 hdproc 的皇冠证书兼容行为一致，仅作用于皇冠浏览器与会员心跳；设为 `false` 可恢复严格证书校验，PS38 的证书校验策略独立。
- 皇冠站改版时，调整 `ps38app/account_pool/providers.py` / `member_capture.py` / `crown_member.py`，不影响 PS38 订单采集。

号池回归验证：

```bash
.venv/bin/python -m unittest discover -s tests -v
PS38_TEST_BROWSER=1 PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" \
  .venv/bin/python -m unittest tests.test_member_login_browser -v
```

第二条命令启动真实 Chromium，对本机模拟会员站验证 iframe 登录、验证码等待、初始化顺序、稳定会话保存与重启复用，不访问真实会员账号。

号池 API（同源、只返回状态，不返回 Cookie/密码）：

- `GET /api/pool`：账号列表、汇总和最近日志
- `POST /api/pool/save`：新增账号（可带 `password`，留空则手动登录）
- `POST /api/pool/login` / `check` / `check-all`：登录、单账号检测、全部检测
- `POST /api/pool/clear` / `delete` / `toggle` / `cancel`：清除会话、删除、启停、取消任务


## 快速开始

要求：Python 3.9+（推荐 3.11/3.12）、能访问 `https://www.ps38ag.com`。

### macOS

1. 把整个 `ps38-events-standalone` 文件夹复制到目标机器（例如桌面）。
2. 双击 **Start.command**。第一次会自动创建 `.venv`、安装依赖和 Chromium。
3. 程序会打开一个 Chromium 窗口：输入代理后台的账号、密码和 2FA，登录完成即可。
4. 浏览器窗口保持打开（可最小化）；打开 <http://127.0.0.1:8899/> 查看看板。

### Windows

双击 **Start.bat**，步骤相同。

### 命令行

```bash
cd ps38-events-standalone
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp config.example.json config.json # 可选，首次运行会自动生成
python run.py
```

打开 <http://127.0.0.1:8899/>。

### 想让程序自动填账号密码

编辑 `config.json`：

```json
{
  "username": "你的代理后台账号",
  "password": "你的密码"
}
```

程序只会在明确发起登录时提交一次账号密码；**2FA 永远需要你在浏览器里手动完成**。不填也可以，直接在弹出窗口里登录。

## 常用命令

```bash
python run.py                 # 启动看板 + 采集（默认）
python run.py login           # 只登录并验证会话
python run.py backfill        # 登录后补采 history_from 至今的已结算订单
python run.py status          # 查看本地订单库统计（不启动浏览器）
python run.py import-db 路径   # 导入原系统的 partner_orders.sqlite3
python run.py doctor          # 检查 Python/Playwright/Chromium/配置
python run.py install-browser # 安装 Playwright Chromium
```

## 从原系统带走已有订单（推荐）

原系统的订单库在：

```text
hdproc/ps3838/var/db/partner_orders.sqlite3
```

复制本文件夹后，停止本程序，执行：

```bash
python run.py import-db "/path/to/partner_orders.sqlite3"
```

表结构完全一致，导入后页面立刻能看到历史订单，不需要重新补采。目标库已存在时需要加 `--force`。

也可以什么都不导入，首次启动会按 `history_from` 自动补采历史（会慢一些，但新单不受影响）。

## 配置

`config.json`（首次运行从 `config.example.json` 复制）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `host` | `127.0.0.1` | 监听地址；只建议本机，改成 `0.0.0.0` 会暴露到局域网 |
| `port` | `8899` | 看板端口；被占用时改这里或 `PS38_PORT=8898` |
| `db_path` | `data/orders.sqlite3` | 订单库路径（相对本文件夹） |
| `profile_dir` | `data/profile` | 独立浏览器配置/登录状态，删除即退出登录 |
| `base_url` | `https://www.ps38ag.com` | 代理后台地址 |
| `login_path` | `/partner/zh-cn/login` | 登录页路径 |
| `dashboard_path` | `/partner-ui-v2/report-outstanding-v2` | 登录后首页；启动时先打开它复用 Cookie（`/partner/` 会 302 到登录页，不能用来判活） |
| `username` / `password` | 空 | 可选，自动填充用；2FA 仍手动 |
| `agent_codes` | `[]` | 要拉取的代理代码；留空则自动使用身份树里的全部代理 |
| `history_from` | `2026-07-01` | 补历史起始日期，也是代理身份树查询起点 |
| `open_interval_seconds` | `1` | 未结算轮询目标间隔（最低 1 秒） |
| `settled_interval_seconds` | `300` | 已结算回查间隔（最低 30 秒） |
| `settled_window_days` | `7` | 每轮回查最近几天已结算 |
| `page_size` | `200` | 后台分页大小（20–500） |
| `headless` | `false` | 无头模式；会话失效时无法手动登录，建议保持 false |
| `browser_channel` | 空 | 空=Playwright Chromium；也可 `chrome` 用本机 Chrome |
| `minimize_after_login` | `false` | 登录成功后自动最小化窗口 |
| `backfill_on_first_run` | `true` | 数据库为空时自动补历史 |
| `crown_host` / `crown_port` | `127.0.0.1` / `9223` | 本机皇冠赛事服务（仅「赛事合并」使用） |
| `crown_utc_offset_minutes` | `-240` | 皇冠 `start_label` 的时区偏移（GMT-4） |
| `merge_db_path` | `data/fixture_merge.sqlite3` | 联赛/赛事映射库，与订单库分开 |
| `merge_seed_path` | `ps38app/league_library_seed.tsv` | 内置联赛库种子（243 条）；首次启动导入一次，之后解绑不会自动恢复 |
| `pinnacle_catalog_url` | `https://www.ps3838.com/sports-service/sv/compact/events` | 官方足球目录，使用平台返回的滚球/今日分类；无需本地快照采集服务 |
| `pinnacle_snapshot_url` | `http://127.0.0.1:8787/api/snapshot` | 旧数据源兼容入口，仅显式把 `pinnacle_catalog_url` 设为空时使用；该旧服务可能按本机日期删减赛事 |
| `merge_horizon_hours` | `72` | 赛事时间有效性校验上限；赛事范围沿用数据源的滚球/今日分类，不按本机日期重新划分今日 |
| `merge_past_hours` | `24` | 回溯订单统计的时间；历史订单不会新增待匹配赛事 |
| `merge_excluded_leagues` | `[]` | 合并时排除的联赛名（精确匹配） |

环境变量可覆盖：`PS38_CONFIG`、`PS38_HOST`、`PS38_PORT`、`PS38_DB_PATH`、`PS38_USERNAME`、`PS38_PASSWORD`、`PS38_AGENTS`、`PS38_HISTORY_FROM`、`PS38_HEADLESS`、`PS38_CROWN_HOST`、`PS38_CROWN_PORT`、`PS38_CROWN_OFFSET`、`PS38_MERGE_DB`、`PS38_MERGE_SEED`、`PS38_MERGE_HORIZON`、`PS38_MERGE_PAST`、`PS38_PINNACLE_CATALOG`、`PS38_PINNACLE_SNAPSHOT` 等（见 `ps38app/config.py`）。

## 赛事合并（可选）

「赛事合并」把皇冠和平博的当前赛事对上，方便在看板里核对同一场比赛。它只读两边数据，不改写订单库或皇冠数据。订单仅补充当前比赛的单数和流水，不决定待匹配清单；历史订单的滚球标志不能代表比赛现在仍在进行。

赛事范围使用各平台数据源自己的「滚球 + 今日未开赛」分类，不按北京时间或本机自然日重新截断。皇冠号池直接请求 `showtype=live/today`，平博直接读取官方足球目录中的 `l`（滚球）与 `n`（今日），不读取 `e/e1`（早盘），也不经过旧本地快照的日期过滤。官方公开目录用于比赛身份与开赛时间对照，不作为实时赔率源。平台归入今日、但换算为北京时间已到次日的比赛仍会显示，跨午夜滚球也继续保留；已结束、失效缓存及异常时间校验仍生效。平博目录成功结果复用 30 秒以保持单场确认响应速度，手动拉取立即重新请求；接口失败会明确报错，不静默退回范围不完整的旧快照。拉取成功提示、顶部数量、联赛列表和待匹配候选使用同一筛选规则；鼠标停在顶部数量上可查看滚球与今日未开赛的分项数量。已确认的赛事映射独立持久保存并持续显示，不依赖皇冠会话；会话只影响当前皇冠赛事拉取和新增匹配。永久联赛库也不受当前赛事筛选影响。

1. 在号池登录皇冠会员。平博赛事目录默认直接从官网读取，无需启动本地平博快照服务。皇冠优先读取本机服务（默认 `127.0.0.1:9223`）；服务失效或返回旧缓存时，使用号池已保存的有效会员会话只读拉取滚球/今日赛事，不发起新登录。号池暂时网络检查失败时仍允许尝试保存的会话。
2. 首次启动会把 `ps38app/league_library_seed.tsv` 的 243 条已知联赛导入 `data/fixture_merge.sqlite3`；手动绑定成功也会立即写入该数据库并显示在「联赛库」。联赛绑定不设过期时间，刷新页面、重启服务或当前没有比赛都不会清除，保留数据库即可持续复用。「联赛库」支持搜索、查看 ID、逐条解绑；解绑只解除联赛关系，保留已经确认的比赛对照，后续新比赛需重新绑定联赛才能匹配。解绑结果也会保存，重启不会重新导入或自动恢复该联赛。
3. 打开「赛事合并」：左侧「联赛绑定」只列出尚未对应的当前皇冠联赛。命中数据库中已有绑定的联赛直接进入右侧；手动绑定成功后，该联赛从左侧移除，比赛进入右侧。
4. 右侧始终以皇冠为主：只显示已绑定联赛的当前皇冠比赛，按皇冠联赛分组，每场下方显示对应平博候选及手动下拉框；候选限定在已绑定的平博联赛内。没有平博比赛或平博暂不可用时，已绑定联赛的皇冠比赛仍显示并说明原因。待合并计数仅统计已绑定联赛中尚未合并的皇冠比赛。绑定联赛或拉取赛事后会自动匹配符合严格条件的场次，其余可人工确认；接口禁止跨联赛合并。
5. 「自动合并」只处理已绑定联赛，不自动猜测联赛关系。双方队名分别至少相似 85%、平均至少 92%，主客顺序与赛事类别一致，开赛时间已知且相差不超过 15 分钟，两边候选均唯一且没有分差在 3 分以内的竞争候选，才会写入赛事映射。不满足的候选保留供人工确认。
6. 确认成功的赛事立即进入「已合并」，历史映射持续显示，直到用户主动解绑。即使皇冠会话失效，已保存的赛事和联赛绑定仍显示；恢复会话并拉取当前赛事后，自动匹配仅新增未保存过的赛事对照，不覆盖或删除历史记录。自动匹配只学习同一绑定联赛内、主客顺序一致且无歧义的已确认队名别名。
7. 状态条右侧的按钮：`拉取皇冠赛事`、`拉取平博赛事`（重新读取当前赛事并尝试自动匹配）、`联赛库`（查看已知联赛，可搜索、解绑）、`已合并联赛`（按联赛查看已合并赛事）。过期、已结束赛事会被过滤；上游明确返回失效缓存或超过 120 秒未更新时，不使用其旧赛事，也不回退到订单赛事。拉取一侧成功、另一侧不可用时分别提示结果。
8. 「赛事匹配」面板按皇冠联赛分组：联赛标题显示 `皇冠联赛名 + 皇冠 ID → 平博联赛名 + 平博 ID`，点联赛展开皇冠比赛；每场皇冠比赛独立保留最多三条平博候选，默认显示最佳一条。收起行上的「确认比赛」直接保存当前显示的皇冠 → 平博候选；需要更换时，点击比赛行展开候选或手动选择。无候选时不显示快捷确认，时间冲突沿用原有确认提示，提交期间禁用快捷按钮以避免重复保存。右上角可切换 `待合并 / 已合并`，计数按皇冠场次统计。「已合并」也按皇冠联赛折叠分组，标题显示该联赛已合并场数，点击展开已确认的皇冠 → 平博比赛；展开状态与待合并视图分别保存，切换视图和页面内刷新后保留。

单场人工确认只读取并校验当前赛事与联赛绑定，然后保存对应关系，不重新计算整批候选。名称标准化使用有容量上限的缓存，不缓存赛事有效性；数据源时效、已结束赛事、跨联赛和重复绑定校验继续生效。前端收到保存成功的映射后直接更新待合并列表、已合并列表和计数，不再额外等待一次整页读取；保存失败时保留原行并允许重试。候选分数在下一次刷新或拉取赛事时重新计算。

皇冠服务和号池会话都不可用时，合并页会显示原因；已保存的联赛和赛事映射继续保留。

## 会话是怎么自己维护的

- 使用独立的 Playwright 持久化浏览器目录 `data/profile`，并在登录成功后额外保存 `data/profile/storage_state.json`（Cookie 快照）。
- 启动时先复用已有 Cookie，直接请求受保护接口验证；有效就跳过登录。
- 验证失败才打开登录页；配置了账号密码就只提交一次，然后持续等待 2FA。
- 运行中接口返回 401/403 或非 JSON 时，程序把原窗口恢复出来，等待你完成登录/2FA，**不会退出，也不会反复提交密码**。
- 登录窗口被关闭时，程序每 15 秒重试一次；也可以点页面上的「重新登录」立即重建。

注意：`data/profile` 与 `data/orders.sqlite3` 含有你的登录态和订单数据，复制给别人前请删除 `data/`。

## 数据与接口

数据库表（与原系统一致）：

- `partner_orders`：订单主表，主键 `(source, member_identity, order_id)`
- `partner_order_events`：新单事件（用于页面顶部提示），最多保留 5000 条
- `partner_poll_state`：每个代理 OPEN/SETTLED 的最近成功快照
- `partner_poll_runs`：轮询运行记录（自动裁剪）
- `partner_agents`：代理代码到后台 userId 的映射

只读接口：

- `GET /api/my-events`：订单分页
- `GET /api/my-events/summary`：统计卡片
- `GET /api/my-events/fixtures`：比赛聚合
- `GET /api/my-events/copy-groups`：跟单合并组（20 秒窗口，返回 `window_ms` 与本次扫描单数）
- `GET /api/my-events/fixture`：单场订单明细
- `GET /api/my-events/events`：新单事件游标
- `GET /api/status`：采集会话/进度/库统计
- `GET /api/health`：健康检查
- `GET /api/follow`：跟单记录、规则、状态统计与可选账号，默认筛选已成功
- `GET /api/follow/groups/<组编号>`：组内原单、处理尝试、状态历史与核对审计

操作接口（POST）：`/api/actions/refresh`、`/api/actions/backfill`、`/api/actions/login`、`/api/actions/open-login`（把后台浏览器窗口提到前台；窗口被关掉则自动重建且保留 Cookie）。

跟单操作接口（POST）：`/api/follow/rule` 保存规则，`/api/follow/pause` 暂停；`/api/follow/groups/<组编号>/reconcile` 安排一次只读核对；`/api/follow/groups/<组编号>/resolution` 为待核对组登记人工核实结果，成功必须有票号和核对依据，并保留人工来源及审计。没有未知订单重新提交接口。

后台只调用这些只读报表接口：

- `/partner-account/ddd/user-info/login-user/profile`
- `/partner-reporting/ddd/report/win-loss-detail`
- `/partner-reporting/ddd/common/bet-list/v2`

上述平博后台采集接口保持只读。皇冠跟单适配器独立使用已选会员会话，首次使用处于停用状态；不提供撤单或改单。

## 目录结构

```text
ps38-events-standalone/
├── Start.command / Start.bat / start.sh   启动脚本
├── run.py                                 命令行入口
├── config.example.json                    配置模板
├── requirements.txt                       playwright
├── requirements-dev.txt                   开发检查依赖
├── pyproject.toml / .editorconfig          Python 检查与编辑风格
├── AGENTS.md                               项目协作约定
├── scripts/check.py                        跨平台检查入口
├── docs/                                   开发约定与模块职责
├── .github/workflows/quality.yml            跨平台 CI 与浏览器回归
├── ps38app/
│   ├── config.py                          配置加载
│   ├── session.py                         浏览器会话、登录、2FA、接口调用
│   ├── store.py                           SQLite、订单解析/校验、查询
│   ├── poller.py                          OPEN/SETTLED 轮询、补历史、状态
│   ├── server.py                          HTTP API + 静态页面
│   ├── fixture_merge.py                   联赛/赛事映射与匹配服务
│   └── account_pool/                      号池存储、登录与心跳
├── web/                                   前端（原生 ES Modules，无构建）
│   ├── index.html
│   ├── logo.svg                             品牌 logo（SVG）
│   ├── app.js
│   ├── styles.css + styles/
│   └── modules/                           my-events 页面、状态轮询、工具
├── tests/                                 离线测试（不访问网站）
└── data/                                  运行后生成：订单库、浏览器会话
```

## 开发与测试

开发环境、目录职责、敏感数据保护和本地浏览器回归说明见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

安装开发依赖后，可用同一条跨平台命令运行 Ruff 检查与完整单元测试：

```bash
python -m pip install -r requirements-dev.txt
python scripts/check.py
```

加 `python scripts/check.py --browser` 可同时运行本地 Chromium 登录与前端页面回归，浏览器安装命令见开发说明。

GitHub Actions 会在 Linux、macOS、Windows 上检查 Python 3.9 和 3.12，并在 Linux 上运行本地 Chromium 登录与前端页面回归测试。

单独运行完整单元测试：

```bash
python -m unittest discover -s tests -v
```

测试只使用临时 SQLite 和本地 HTTP 服务，不登录、不访问 ps38ag.com。

## 常见问题

**端口被占用**

```bash
PS38_PORT=8898 python run.py
```

看板地址相应变成 <http://127.0.0.1:8898/>。

**浏览器起不来 / 提示 profile 被锁**

同一份文件夹只能有一个采集进程。先退出已运行的程序；仍不行时删除 `data/profile/SingletonLock`（或整个 `data/profile`，需要重新登录）。

**提示未安装 Chromium**

```bash
python run.py install-browser
```

**登录后一直显示「等待登录」**

确认浏览器窗口里已经完成 2FA、进入代理后台首页；程序会持续验证身份接口。确认页面没有停留在登录/验证码页。

**页面上没有订单**

- 看顶部状态条：`采集在线` 才会持续拉取；`等待登录` 时先在浏览器完成登录。
- 检查 `agent_codes` 是否与代理后台的代理代码一致；留空会自动发现。
- 历史订单需要「补历史」或 `import-db`；默认只回查最近 7 天结算。

**导入旧库后想合并新数据**

导入只做一次性复制。导入完成后启动本程序，新的 OPEN/SETTLED 会继续写入同一个库，不会重复。

## 安全边界

- 默认只监听 `127.0.0.1`，页面没有登录保护；不要直接暴露到公网。
- 平博后台采集只读；皇冠跟单需要用户在本地规则页明确启用，默认停用。
- `config.json` 可能含账号密码，`data/` 含登录 Cookie 和订单数据，复制/分享前请清理。
