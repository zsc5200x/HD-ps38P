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
| PS38 登录、2FA 与会话恢复 | `ps38app/session.py` | 交互式登录与只读报表请求 |
| 号池登录、心跳和状态 | `ps38app/account_pool/` | `service.py` 编排，`providers.py` 执行平台流程，`store.py` 管理本机状态 |
| 皇冠赛事读取 | `ps38app/crown_fixtures.py` | 读取已保存会话，不主动触发新登录 |
| 赛事对照、联赛绑定与映射存储 | `ps38app/fixture_merge.py`、`fixture_identity.py` | 严格匹配条件及人工确认，独立映射库 |
| 页面导航与布局 | `web/modules/nav.js`、`web/styles/` | 显式模块导入，共用页面壳 |
| 页面业务 | `web/modules/my-events/`、`merge/`、`pool.js` | 保持 API 数据请求和 DOM 事件职责清晰 |

## 运行数据

订单库 `data/orders.sqlite3`、映射库 `data/fixture_merge.sqlite3`、账号池库 `data/accounts.sqlite3` 分别保存不同业务的数据。浏览器 profile、凭证和会话快照也位于 `data/`。整个目录是本机运行状态，已从 Git 排除。

测试通过临时配置把数据库、profile 和账号目录全部指向临时目录。浏览器测试使用真实静态页面及本地模拟数据；测试不需要真实账号。

## 后续拆分顺序

现有入口和导入路径继续保持兼容。需要扩展某一部分时，再按下面顺序拆分相应职责，并让测试先覆盖行为：

1. 将 `server.py` 中的订单、号池、合并请求处理逐步分开，共享请求解析和响应逻辑。
2. 将 `fixture_merge.py` 中的数据源读取、匹配规则、SQLite 存储和服务编排分别抽出。
3. 将 `store.py` 中的上游数据解析、订单写入和查询逐步分开。
4. 为不断增加的配置项引入类型约束，并保留现有 JSON 和环境变量入口。

这些是后续维护边界，当前未迁移上述业务模块。修改匹配阈值、订单归并规则或数据库表结构时，应明确说明行为变化，并运行相应回归；结构整理应保持业务结果一致。

返回 [开发说明](DEVELOPMENT.md) 或 [项目说明](../README.md)。
