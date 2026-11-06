# 模块职责与改动边界

本项目在一个本地进程内运行 HTTP 服务、订单采集器和账号池。前端使用原生 ES Modules，由 Python 直接提供静态文件；运行时不依赖 Node 或原始 hdproc 仓库。

## 定位需要修改的模块

| 需求 | 主要位置 | 边界 |
| --- | --- | --- |
| 启动、命令行、环境诊断、导入订单库 | `run.py` | 参数解析与服务生命周期编排 |
| 默认配置、环境变量与配置脱敏 | `ps38app/config.py` | 相对路径统一基于项目目录解析 |
| HTTP 请求与静态资源 | `ps38app/server.py` | 接口委托服务处理，不直接操作浏览器 |
| 订单分页校验、持久化和查询 | `ps38app/store.py` | 保持旧订单数据库兼容 |
| OPEN/SETTLED 轮询和历史补采 | `ps38app/poller.py` | 专属线程持有浏览器和订单写连接 |
| PS38 登录、2FA 与会话恢复 | `ps38app/session.py`、`ps38app/ps38_http.py` | 默认 `browser`，使用本地 profile 或 AdsPower CDP；`http` 或绑定环境时的 `auto` 使用纯 HTTP（SOCKS5 + Cookie + TOTP） |
| PS38 HTTP 传输 | `ps38app/net.py` | 零依赖 SOCKS5/TLS/HTTP 1.1 keep-alive；不启动浏览器 |
| 登录被拒绝 | `ps38app/errors.py` | 密码/锁定等明确拒绝不再连续自动重试 |
| PS38 TOTP 与验证码识别 | `ps38app/totp.py`、`scripts/ocr_captcha.swift` | 标准 RFC 6238；验证码命令为可选外部能力，未配置时降级人工 |
| 号池登录、心跳和状态 | `ps38app/account_pool/` | `service.py` 编排，`providers.py` 执行平台流程，`store.py` 管理本机状态；`crown_http.py` 负责皇冠会员纯 HTTP 登录/心跳/重登 |
| ADS 环境导入 | `ps38app/adspower.py` | 封装本地 AdsPower API，解析环境名/过期日并脱敏投影；管理本次启动的环境，不保存代理密码 |
| 皇冠注单报表 | `ps38app/crown_reports.py` | 只读平台历史注单，按账号与票号幂等同步；本地报表供 HTTP 接口和通知查询，后台任务响应停止 |
| 皇冠赛事读取 | `ps38app/crown_fixtures.py` | 读取已保存会话，不主动触发新登录 |
| 平博官方赛事目录 | `ps38app/pinnacle_fixtures.py`、`fixture_merge.py` | 目录提供赛事身份与赛前／滚球分类，订单统计按身份补充 |
| 赛事对照、联赛绑定与映射存储 | `ps38app/fixture_merge.py`、`fixture_identity.py` | 严格匹配条件及人工确认，独立映射库 |
| 跟单规则、分组与回执 | `ps38app/follow/` | 独立任务库，消费已提交源事件，平台适配与状态机分离 |
| 设置页定位本机数据文件 | `ps38app/reveal.py` | 只接受白名单里的库文件与目录，按系统调用文件管理器 |
| 页面导航与布局 | `web/modules/nav.js`、`web/modules/statusbar.js`、`web/styles/` | 显式模块导入，共用页面壳；桌面端固定视口，页面在壳内滚动 |
| 页面业务 | `web/modules/my-events/`、`merge/`、`copy/`、`settings/`、`pool.js` | 保持 API 数据请求和 DOM 事件职责清晰 |
| Telegram 推送与按钮回调 | `ps38app/notify.py` | 只依赖标准库；配置与送达记录在 `data/notify.json`，发送失败不影响跟单 |
| 分单与累计限额 | `ps38app/follow/limits.py`、`follow_parts` 表 | 限额按已成功+在途计算；分单结果逐份保存，部分成功为 `PARTIAL` |

## 运行数据

订单库 `data/orders.sqlite3`、映射库 `data/fixture_merge.sqlite3`、账号池库 `data/accounts.sqlite3` 和跟单任务库 `data/follow.sqlite3` 分别保存不同业务的数据；主页「跟单视角」的合并单号登记在 `data/copy_orders.sqlite3`，与采集器的写入分开，避免争锁。浏览器 profile、凭证和会话快照也位于 `data/`。皇冠历史注单报表另存于 `data/crown_reports.sqlite3`。整个目录是本机运行状态，已从 Git 排除。

测试通过临时配置把数据库、profile 和账号目录全部指向临时目录。浏览器测试使用真实静态页面及本地模拟数据；测试不需要真实账号。

## 跟单模块边界

[跟单设计](FOLLOW_SYSTEM_DESIGN.md) 记录需求、实现与后续范围。`ps38app/follow/` 负责服务端，`web/modules/copy/` 负责记录页，`follow_db_path` 指向独立任务库。规则默认停用，处理足球单关全场／上半场让球与大小；`follow_allow_prematch` 默认开启，关闭后只处理滚球。规则不绑定单个目标账号，每组从号池自动选择一个启用、在线且会话有效的皇冠会员，可在该账号上顺序分单。

服务从已提交的新单事件定位主页「跟单视角」生成的 20 秒展示批次：该批次首单进入服务时开启固定 10 秒待跟单窗口，窗口内同批次新单并入但不重置倒计时，满 10 秒冻结成员与合计金额并分配独立跟单批次号，随后进入映射、查盘与提交。冻结后到达的源单另起下一批重新计时；已处理源单按订单键去重，不重复跟单。窗口截止时间与成员持久化在 `follow_pending`／`follow_pending_members`，重启后按原截止时间恢复。规则关闭时主页请求只登记批次为“跟单未开启”，不发下注请求。20 秒只约束展示批次成员，10 秒是跟单端的固定待单窗口。采集线程负责订单采集；查盘和任务等待由独立服务承接，HTTP 层仅委托服务。`run.py` 管理服务启动，服务器关闭时停止后台任务。

`follow/service.py` 编排本地调度、分类重试与分单执行；`follow/dispatcher.py` 使用固定大小执行器承接网络任务，不建立无界等待队列，按组键和账号去重。没有空位的组留在 SQLite，下一轮继续调度。查盘和提交共享账号占用；`follow_claims` 的组主键和账号唯一约束同时保护多服务实例。

账号候选来自本项目账号池，不由跟单规则或页面手工指定。服务在空闲候选中按持久化游标轮转，并优先尝试本组尚未用过的账号；账号无可用会话或候选均忙时组进入 `WAIT_ACCOUNT`。组与提交尝试都记录实际账号，同一账号的在途任务串行执行；当前已取消旧版 `UNKNOWN` 账号门禁。

任务库的 `follow_history` 保存状态与原因变化，`follow_attempts` 记录提交尝试，`follow_parts` 保存逐份结果；`follow_reconciliations` 仅为旧数据兼容保留。`PREPARING` 的失效占用可恢复准备，`SUBMITTING` 中断后组记为 `REJECTED`，原尝试保留 `UNKNOWN` 以说明请求结果不明。同一次请求迟到的有效成功回执仍可纠正失败记录。活占用不能按固定超时直接清除。

提交响应中断时保留即时只读核对：仅接受提交前今日票号基线之外、GID 和精确金额唯一匹配且未使用的新票。当前已删除 `follow/reconcile.py`、旧版后台核对任务、人工登记页面及对应接口。提交没有有效成功回执且未能唯一核对到新票时记为 `REJECTED`，不表示已经证实未成交；需要到平台核实。旧 `UNKNOWN` 组与审计记录保留，但不会进入自动核对或重发流程。重试由平台返回码和适配器标记驱动，部分错误策略会换号、调整金额或延长等待期限，具体见 [FOLLOW_EXECUTION.md](FOLLOW_EXECUTION.md)。

人民币口径由 `follow/source.py` 规范化：本后台的 THB 标签与 CNY／RMB 一样按原数字计算，保留 `reported_currency` 供明细核对；不修改原始订单库，也不做汇率换算。规则页不再配置币种，目标皇冠报价仍核对实际人民币账号。

HTTP 服务先绑定端口，再初始化跟单状态恢复，重复启动失败不会修改运行中任务。皇冠请求共享准备阶段时间预算，并在请求间及响应体分块读取时响应停止；系统 DNS 解析和底层连接／响应头处理仍可能超出预算，不能将 `stop()` 的等待上限视为网络调用已必然退出。

订单唯一键、源赛事身份、已确认映射键、皇冠比赛 ID 和实际盘口行 ID 分别表达不同对象。既有订单衍生赛事 ID 与官方目录 ID 不同，应建立显式关联；`reversedOrder` 的平台主客顺序与业务反向规则分别处理。

持久化组保存跟单批次号、冻结成员、规则版本、计划金额及状态；批次号唯一约束吸收事件重读，冻结后新增成员不能改变已建组。待跟单窗口、截止时间与成员在冻结建组的同一事务内结算并删除。任务消费游标和组变化原子保存。会话继续由现有账号池管理，独立任务库只保存账号引用和脱敏业务记录。

## 后续拆分顺序

现有入口和导入路径继续保持兼容。需要扩展某一部分时，再按下面顺序拆分相应职责，并让测试先覆盖行为：

1. 将 `server.py` 中的订单、号池、合并请求处理逐步分开，共享请求解析和响应逻辑。
2. 将 `fixture_merge.py` 中的数据源读取、匹配规则、SQLite 存储和服务编排分别抽出。
3. 将 `store.py` 中的上游数据解析、订单写入和查询逐步分开。
4. 为不断增加的配置项引入类型约束，并保留现有 JSON 和环境变量入口。

这些是后续维护边界，当前未迁移上述业务模块。修改匹配阈值、订单归并规则或数据库表结构时，应明确说明行为变化，并运行相应回归；结构整理应保持业务结果一致。

返回 [开发说明](DEVELOPMENT.md) 或 [项目说明](../README.md)。
