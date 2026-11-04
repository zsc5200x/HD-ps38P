/** 设置页：把 /api/status 的运行配置按「采集 / 存储 / 会话」三栏只读展示。 */

import { showToast } from "../dom.js";
import { fetchJson, postJson } from "../http.js";

const $ = (id) => document.getElementById(id);

const state = { payload: null, notify: null, bound: false, elapsed: 0 };

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

function seconds(value) {
  return value === null || value === undefined ? "—" : `${value} 秒`;
}

function days(value) {
  return value === null || value === undefined ? "—" : `${value} 天`;
}

function hours(value) {
  return value === null || value === undefined ? "—" : `${Number(value)} 小时`;
}

function count(value) {
  return value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN");
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

function fileLabel(value) {
  const parts = String(value).split(/[\\/]+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(value);
}

function fieldRow(label, value, { mono = false, title = "" } = {}) {
  const row = node("div", null, "settings-row");
  row.append(node("span", label, "settings-row-label"));
  const content = node("b", display(value), `settings-row-value${mono ? " settings-mono" : ""}`);
  if (title) content.title = title;
  row.append(content);
  return row;
}

function statGrid(items) {
  const grid = node("div", null, "settings-stats");
  for (const [label, value] of items) {
    const cell = node("div", null, "settings-stat");
    cell.append(node("span", label), node("b", display(value)));
    grid.append(cell);
  }
  return grid;
}

function chipsRow(label, items, emptyText = "—") {
  const row = node("div", null, "settings-row settings-row-chips");
  row.append(node("span", label, "settings-row-label"));
  const box = node("div", null, "settings-chips");
  if (Array.isArray(items) && items.length) {
    for (const item of items) box.append(node("span", item, "settings-chip"));
  } else {
    box.append(node("span", emptyText, "settings-row-empty"));
  }
  row.append(box);
  return row;
}

function pillRow(label, text, tone = "") {
  const row = node("div", null, "settings-row");
  row.append(node("span", label, "settings-row-label"));
  row.append(node("span", text, `settings-pill${tone ? ` ${tone}` : ""}`));
  return row;
}

function pathRow(label, fullPath, kind) {
  const row = node("div", null, "settings-row settings-row-path");
  row.append(node("span", label, "settings-row-label"));
  const path = String(fullPath || "").trim();
  const wrap = node("span", null, "settings-path");
  if (!path) {
    wrap.append(node("span", "未配置", "settings-row-empty"));
    row.append(wrap);
    return row;
  }
  wrap.append(node("b", fileLabel(path), "settings-path-name"));
  wrap.title = path;

  const button = node("button", "打开", "settings-open");
  button.type = "button";
  button.dataset.openKind = kind;
  button.title = path;
  button.setAttribute("aria-label", `在文件管理器中打开${label}`);
  row.append(wrap, button);
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
  const codes = Array.isArray(config.agent_codes) ? config.agent_codes : [];
  fill("settingsCollectFields", [
    fieldRow("服务监听", `${display(config.host, "127.0.0.1")}:${display(config.port)}`, {
      mono: true,
    }),
    fieldRow("数据源", display(config.base_url), {
      mono: true,
      title: display(config.base_url, ""),
    }),
    statGrid([
      ["未结算轮询", seconds(config.open_interval_seconds)],
      ["已结算轮询", seconds(config.settled_interval_seconds)],
      ["已结算窗口", days(config.settled_window_days)],
      ["历史起点", display(config.history_from)],
      ["合并窗口", hours(config.merge_horizon_hours)],
      [
        "浏览器模式",
        config.headless === true ? "无头" : config.headless === false ? "可见窗口" : "—",
      ],
    ]),
    chipsRow("采集代理", codes, "未配置代理"),
  ]);
  renderAgents(config);
}

function renderStore(payload) {
  const config = payload.config || {};
  const db = payload.db || {};
  fill("settingsStoreFields", [
    pathRow("订单库", db.path || config.db_path, "orders_db"),
    pathRow("跟单库", config.follow_db_path, "follow_db"),
    pathRow("映射库", config.merge_db_path, "merge_db"),
    pathRow("号池库", config.pool_db_path, "pool_db"),
    pathRow("账号目录", config.pool_dir, "pool_dir"),
    pillRow(
      "登录凭证",
      config.has_credentials === true ? "已配置" : "使用已保存会话",
      config.has_credentials === true ? "is-online" : "is-muted",
    ),
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
    cell.append(node("span", label), node("b", count(value)));
    fragment.append(cell);
  }
  host.replaceChildren(fragment);
}

function renderSession(payload) {
  const session = payload.session || {};
  const profile = session.profile || {};
  const backfill = payload.backfill || {};
  fill("settingsSessionFields", [
    fieldRow("登录账号", profile.login_id || profile.user_code || "—", { mono: true }),
    chipsRow("代理节点", session.agents, "未返回代理"),
    statGrid([
      ["轮询次数", count(session.poll_count)],
      ["本轮耗时", session.last_cycle_seconds != null ? `${session.last_cycle_seconds} 秒` : "—"],
      ["新增订单", session.new_orders != null ? `${count(session.new_orders)} 笔` : "—"],
    ]),
    fieldRow("最近未结算", time(session.last_open_at), { mono: true }),
    fieldRow("最近已结算", time(session.last_settled_at), { mono: true }),
    pillRow(
      "补采状态",
      backfill.running
        ? `进行中 ${count(backfill.done)}/${count(backfill.total)}`
        : "未运行",
      backfill.running ? "is-warning" : "is-muted",
    ),
    fieldRow("版本", display(payload.version), { mono: true }),
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

async function openPath(button) {
  const kind = button.dataset.openKind;
  if (!kind || button.disabled) return;
  button.disabled = true;
  try {
    const payload = await postJson("/api/actions/open-path", { kind });
    showToast(payload.message || "已打开");
  } catch (error) {
    showToast(error?.message || "打开路径失败");
  } finally {
    button.disabled = false;
  }
}

function setNotifyTag(text, tone = "muted") {
  const tag = $("notifyPanelTag");
  if (!tag) return;
  tag.textContent = text;
  tag.className = `cfg-chip ${tone === "on" ? "cfg-chip-on" : tone === "error" ? "cfg-chip-error" : "cfg-chip-muted"}`;
}

function setChipState(id, text, tone = "") {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.className = tone ? `is-${tone}` : "";
}

function renderNotify(data) {
  state.notify = data;
  const enabled = Boolean(data.enabled);
  const configured = Boolean(data.configured);
  const token = $("notifyToken");
  if (token) {
    token.value = "";
    token.placeholder = data.token_hint ? `已保存 ${data.token_hint}，留空保留` : "输入 Bot Token";
  }
  const chat = $("notifyChat");
  if (chat) chat.value = data.chat_id || "";
  const proxy = $("notifyProxy");
  if (proxy) proxy.value = data.proxy || "";
  const enabledInput = $("notifyEnabled");
  if (enabledInput) enabledInput.checked = enabled;
  const resultInput = $("notifyPushResult");
  if (resultInput) resultInput.checked = Boolean(data.push_result);
  const emojiInput = $("notifyCustomEmoji");
  if (emojiInput) emojiInput.checked = data.custom_emoji !== false;

  setNotifyTag(
    enabled && configured ? "已启用" : configured ? "已配置" : enabled ? "待配置" : "未配置",
    enabled && configured ? "on" : configured ? "on" : "muted",
  );
  setChipState("notifyBotStatus", data.bot_username ? `@${data.bot_username}` : configured ? "已配置" : "未认证", data.bot_username || configured ? "on" : "");
  setChipState("notifyChatStatus", data.chat_id ? "已设置" : "未设置", data.chat_id ? "on" : "");
  setChipState("notifyProxyStatus", data.proxy ? "代理" : "直连", "on");
  setChipState("notifyBackendStatus", "未接入", "");
  const pushTag = $("notifyPushTag");
  if (pushTag) {
    pushTag.textContent = enabled && configured ? "已启用" : "随机器人启用";
    pushTag.className = `cfg-chip ${enabled && configured ? "cfg-chip-on" : "cfg-chip-muted"}`;
  }
  const hint = $("notifyHint");
  if (hint) {
    hint.textContent = data.custom_emoji_error
      ? `自定义表情不可用，已回退普通表情：${data.custom_emoji_error}`
      : data.last_error
        ? `最近错误：${data.last_error}`
        : data.last_sent_at
        ? `最近发送：${data.last_sent_at} · 已发送 ${data.sent_count || 0} 条`
        : "启用后按「新注单提醒」推送，成功/失败会就地更新；发送失败自动重试，不影响跟单执行。";
  }
}

async function loadNotify() {
  try {
    const { response, payload } = await fetchJson(
      "/api/notify",
      { cache: "no-store" },
      { timeoutMs: 8000, allowInvalidJson: true },
    );
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    renderNotify(payload);
  } catch (error) {
    setNotifyTag("不可用", "error");
    const hint = $("notifyHint");
    if (hint) hint.textContent = `推送服务不可用：${error?.message || "请求失败"}`;
  }
}

async function saveNotify() {
  const button = $("notifySave");
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const body = {
      enabled: Boolean($("notifyEnabled")?.checked),
      push_result: Boolean($("notifyPushResult")?.checked),
      custom_emoji: Boolean($("notifyCustomEmoji")?.checked),
      chat_id: $("notifyChat")?.value.trim() || "",
      proxy: $("notifyProxy")?.value.trim() || "",
    };
    const token = $("notifyToken")?.value.trim() || "";
    if (token) body.bot_token = token;
    if (body.enabled) {
      const hasToken = Boolean(token || state.notify?.has_token);
      if (!hasToken || !body.chat_id) {
        showToast("启用推送前请先填写 BOT TOKEN 和 CHAT ID");
        return;
      }
    }
    const payload = await postJson("/api/notify", body);
    showToast(payload.ok === false ? payload.error : "推送配置已保存");
    await loadNotify();
  } catch (error) {
    showToast(error?.message || "保存推送配置失败");
  } finally {
    if (button) button.disabled = false;
  }
}

async function testNotify() {
  const button = $("notifyTest");
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const payload = await postJson("/api/notify/test", {});
    showToast(payload.message || "测试消息已发送");
    await loadNotify();
  } catch (error) {
    showToast(error?.message || "测试推送失败");
  } finally {
    if (button) button.disabled = false;
  }
}

async function clearNotify() {
  const button = $("notifyClear");
  if (button?.disabled) return;
  if (!window.confirm("清除机器人配置并停用所有推送？")) return;
  if (button) button.disabled = true;
  try {
    const payload = await postJson("/api/notify/clear", {});
    showToast(payload.message || "已清除推送配置");
    await loadNotify();
  } catch (error) {
    showToast(error?.message || "清除推送配置失败");
  } finally {
    if (button) button.disabled = false;
  }
}

export function bindSettingsEvents() {
  const page = $("settingsPage");
  if (state.bound || !page) return;
  state.bound = true;

  page.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-open-kind]") : null;
    if (target) openPath(target);
  });

  $("notifySave")?.addEventListener("click", saveNotify);
  $("notifyTest")?.addEventListener("click", testNotify);
  $("notifyClear")?.addEventListener("click", clearNotify);
  window.addEventListener("ps38:pagenav", (event) => {
    if (event.detail?.page === "settings") loadNotify();
  });
  if (!$("settingsPage")?.hidden) loadNotify();

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
