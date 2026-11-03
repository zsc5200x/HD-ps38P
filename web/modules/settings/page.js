/** 设置页：把 /api/status 的运行配置按「采集 / 存储 / 会话」三栏只读展示。 */

const $ = (id) => document.getElementById(id);

const state = { payload: null, bound: false, elapsed: 0 };

const SESSION_TEXT = {
  starting: "启动中",
  login: "等待登录",
  online: "采集在线",
  error: "采集异常",
  stopped: "已停止",
};

const SESSION_TONE = {
  starting: "is-muted",
  login: "is-warning",
  online: "is-online",
  error: "is-error",
  stopped: "is-muted",
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function display(value, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function time(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "—"
    : parsed.toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
}

function field(label, value, { mono = false, wide = false } = {}) {
  const row = node("div", null, `settings-field${wide ? " settings-field-wide" : ""}`);
  row.append(node("span", label, "settings-field-label"));
  const content = node("b", display(value), "settings-field-value");
  if (mono) content.classList.add("settings-mono");
  row.append(content);
  return row;
}

function fill(id, rows) {
  const host = $(id);
  if (!host) return;
  const fragment = document.createDocumentFragment();
  for (const row of rows) fragment.append(row);
  host.replaceChildren(fragment);
}

function renderBar(payload) {
  const session = payload.session || {};
  const status = String(session.status || "starting");
  const badge = $("settingsState");
  if (badge) {
    badge.textContent = SESSION_TEXT[status] || status;
    badge.className = `settings-state ${SESSION_TONE[status] || "is-muted"}`;
  }
  const text = $("settingsStateText");
  if (text) {
    text.textContent = session.message
      ? `最近事件 · ${session.message}`
      : "运行配置只读展示，修改请编辑 config.json。";
  }
  const updated = $("settingsUpdated");
  if (updated) updated.textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
}

function renderAgents(config) {
  const host = $("settingsAgentList");
  if (!host) return;
  const codes = Array.isArray(config.agent_codes) ? config.agent_codes : [];
  if (!codes.length) {
    host.replaceChildren(node("p", "采集器未返回代理列表。", "cfg-empty"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const code of codes) {
    const row = node("div", null, "cfg-agent");
    const name = node("div", null, "cfg-agent-name");
    name.append(node("strong", code), node("small", "PS38 代理"));
    const tags = node("div", null, "cfg-agent-tags");
    tags.append(
      node("span", "全部盘口", "cfg-chip cfg-chip-muted"),
      node("span", "跟单", "cfg-chip cfg-chip-muted"),
    );
    row.append(name, tags);
    fragment.append(row);
  }
  host.replaceChildren(fragment);
}

function renderCollect(config) {
  fill("settingsCollectFields", [
    field("服务监听", `${display(config.host, "127.0.0.1")}:${display(config.port)}`, { mono: true }),
    field("数据源", display(config.base_url), { mono: true, wide: true }),
    field("采集代理", Array.isArray(config.agent_codes) && config.agent_codes.length
      ? config.agent_codes.join(" / ") : "—", { wide: true }),
    field("未结算轮询", config.open_interval_seconds != null ? `${config.open_interval_seconds} 秒` : "—", { mono: true }),
    field("已结算轮询", config.settled_interval_seconds != null ? `${config.settled_interval_seconds} 秒` : "—", { mono: true }),
    field("已结算窗口", config.settled_window_days != null ? `${config.settled_window_days} 天` : "—", { mono: true }),
    field("历史起点", display(config.history_from), { mono: true }),
    field("浏览器模式", config.headless === true ? "无头" : config.headless === false ? "可见窗口" : "—"),
    field("赛事合并窗口", config.merge_horizon_hours != null ? `${config.merge_horizon_hours} 小时` : "—", { mono: true }),
  ]);
  renderAgents(config);
}

function renderStore(payload) {
  const config = payload.config || {};
  const db = payload.db || {};
  fill("settingsStoreFields", [
    field("订单库", display(db.path || config.db_path), { mono: true, wide: true }),
    field("跟单库", display(config.follow_db_path), { mono: true, wide: true }),
    field("映射库", display(config.merge_db_path), { mono: true, wide: true }),
    field("号池库", display(config.pool_db_path), { mono: true, wide: true }),
    field("账号目录", display(config.pool_dir), { mono: true, wide: true }),
    field("登录凭证", config.has_credentials === true ? "已配置" : config.has_credentials === false ? "未配置（使用已保存会话）" : "—"),
  ]);
  const tag = $("settingsDbTag");
  if (tag) {
    tag.textContent = db.ready === true ? "就绪" : db.ready === false ? "不可用" : "本机";
    tag.className = `settings-tag ${db.ready === true ? "is-online" : db.ready === false ? "is-error" : ""}`.trim();
  }
  const counts = payload.counts || {};
  const host = $("settingsCounts");
  if (!host) return;
  const items = [
    ["订单总量", counts.total],
    ["未结算", counts.open],
    ["已结算", counts.settled],
  ];
  const fragment = document.createDocumentFragment();
  for (const [label, value] of items) {
    const cell = node("div", null, "settings-count");
    cell.append(node("span", label), node("b", value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN")));
    fragment.append(cell);
  }
  host.replaceChildren(fragment);
}

function renderSession(payload) {
  const session = payload.session || {};
  const profile = session.profile || {};
  const backfill = payload.backfill || {};
  fill("settingsSessionFields", [
    field("登录账号", profile.login_id || profile.user_code || "—", { mono: true }),
    field("代理节点", Array.isArray(session.agents) && session.agents.length ? session.agents.join(" / ") : "—", { mono: true, wide: true }),
    field("最近未结算轮询", time(session.last_open_at), { mono: true }),
    field("最近已结算轮询", time(session.last_settled_at), { mono: true }),
    field("轮询次数", session.poll_count != null ? Number(session.poll_count).toLocaleString("zh-CN") : "—", { mono: true }),
    field("本轮耗时", session.last_cycle_seconds != null ? `${session.last_cycle_seconds} 秒` : "—", { mono: true }),
    field("新增订单", session.new_orders != null ? `${Number(session.new_orders).toLocaleString("zh-CN")} 笔` : "—", { mono: true }),
    field("补采状态", backfill.running ? `进行中 ${Number(backfill.done || 0)}/${Number(backfill.total || 0)}` : "未运行", { wide: true }),
    field("版本", display(payload.version), { mono: true }),
  ]);
  const tag = $("settingsSessionTag");
  if (tag) {
    const status = String(session.status || "starting");
    tag.textContent = SESSION_TEXT[status] || status;
    tag.className = `settings-tag ${SESSION_TONE[status] || ""}`.trim();
  }
}

function render() {
  const payload = state.payload;
  if (!payload) return;
  renderBar(payload);
  renderCollect(payload.config || {});
  renderStore(payload);
  renderSession(payload);
}

export function bindSettingsEvents() {
  if (state.bound || !$("settingsPage")) return;
  state.bound = true;
  $("settingsSystemOpen")?.addEventListener("click", () => $("settingsSystemDialog")?.showModal());
  $("settingsSystemClose")?.addEventListener("click", () => $("settingsSystemDialog")?.close());
  window.addEventListener("ps38:status", (event) => {
    const detail = event.detail;
    if (!detail || detail.ok === false) {
      const badge = $("settingsState");
      if (badge) {
        badge.textContent = "无法连接";
        badge.className = "settings-state is-error";
      }
      const text = $("settingsStateText");
      if (text) text.textContent = detail?.error || "读取运行状态失败，请确认服务仍在运行。";
      return;
    }
    state.payload = detail;
    state.elapsed = 0;
    render();
  });
}
