# 项目协作约定

- 回复和新增说明文档使用简体中文。
- 项目使用 Python 3.9+、Playwright、SQLite 和原生 JavaScript ES Modules；保持 `python run.py` 与现有双击启动入口可用。
- 后端代码放在 `ps38app/`，页面模块放在 `web/modules/`，样式放在 `web/styles/`，测试放在 `tests/`。
- 修改前阅读 [开发说明](docs/DEVELOPMENT.md) 和 [模块职责](docs/ARCHITECTURE.md)。
- 使用项目 `.venv`，安装 `requirements-dev.txt`；交付前运行 `python scripts/check.py`。涉及页面交互、登录或会话时，再运行 `python scripts/check.py --browser`。
- 测试使用临时目录、本地 HTTP 服务或模拟响应；服务器测试配置使用 `tests.support.isolated_config()`，不要读取个人 `config.json` 或写入项目 `data/`。
- `config.json`、`data/`、浏览器 profile、Cookie、密码、调试日志和临时截图不得提交。可分享配置只写入 `config.example.json`。
- 按职责小步修改，保持数据库兼容；新建 SQLite 连接应明确提交或回滚并关闭，后台任务应可停止并释放资源。
