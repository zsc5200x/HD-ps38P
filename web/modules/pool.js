/** 号池管理：账号卡片、登录任务、心跳检测与会话状态。 */
import { $, el, showToast } from "./dom.js";
import { fetchJson, postJson } from "./http.js";

const STATUS = {
  online: { label: "在线", tone: "online" },
  logging_in: { label: "登录中", tone: "pending" },
  awaiting_login: { label: "等待登录", tone: "pending" },
  checking: { label: "检测中", tone: "pending" },
  expired: { label: "已失效", tone: "error" },
  error: { label: "异常", tone: "error" },
  idle: { label: "未登录", tone: "idle" },
  disabled: { label: "已停用", tone: "idle" },
};

const PLATFORM_DEFAULTS = {
  ps38_backend: {
    base_url: "https://www.ps38ag.com",
    login_path: "/partner/zh-cn/login",
    dashboard_path: "/partner-ui-v2/report-outstanding-v2",
  },
  crown_backend: { base_url: "", login_path: "/", dashboard_path: "" },
  crown_member: { base_url: "", login_path: "/", dashboard_path: "" },
};

const HEARTBEAT_SLOTS = 24;

let snapshot = null;
let refreshTimer = null;
let loading = false;
const view = { search: "", filter: "all", sort: "default" };
const monitor = { data: null, fetchedAt: 0 };
const ads = { items: [], summary: {}, selected: new Set(), usernames: {}, loading: false, error: "" };
let credentialAccountId = "";
let pendingDestructive = null;

function formatTime(value) {
  const raw = String(value || "");
  if (!raw) return "—";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw.slice(11, 16) || raw.slice(0, 16);
  const pad = (number) => String(number).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatClock(value) {
  const date = new Date(String(value || ""));
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (number) => String(number).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function formatLatency(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return "—";
  if (number < 1000) return `${Math.round(number)}ms`;
  return `${(number / 1000).toFixed(2)}s`;
}

function formatBalance(value) {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) return "未知";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(amount);
}

function statusOf(account) {
  return STATUS[account.status] || { label: account.status || "未知", tone: "idle" };
}

function beatTone(status) {
  if (status === "online") return "online";
  if (status === "expired" || status === "error") return "error";
  if (status === "awaiting_login" || status === "logging_in" || status === "checking") return "pending";
  return "idle";
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = String(value);
}

function logStatus(status) {
  if (status === "success") return "online";
  if (status === "error") return "error";
  if (status === "pending") return "pending";
  return "idle";
}

/** Older backends do not expose heartbeat history yet; rebuild a strip from
    the per-account check/login log so the card still shows real activity. */
function logBeats(account) {
  const logs = (snapshot?.logs || [])
    .filter((item) => item.account_id === account.id
      && (item.action === "check" || item.action === "login"))
    .map((item) => ({
      status: logStatus(item.status),
      latency_ms: 0,
      message: item.message,
      created_at: item.created_at,
    }));
  return logs.slice(-HEARTBEAT_SLOTS);
}

function beatsFor(account) {
  const beats = Array.isArray(account.heartbeats) ? account.heartbeats : [];
  if (beats.length) return beats.slice(-HEARTBEAT_SLOTS);
  const logs = logBeats(account);
  if (logs.length) {
    const last = logs[logs.length - 1];
    const checked = new Date(String(account.last_check || "")).getTime();
    const logged = new Date(String(last.created_at || "")).getTime();
    if (account.last_check && Number.isFinite(checked) && (!Number.isFinite(logged) || checked > logged)) {
      logs.push({
        status: account.status,
        latency_ms: 0,
        message: account.status_message || account.last_error || "",
        created_at: account.last_check,
      });
    }
    return logs.slice(-HEARTBEAT_SLOTS);
  }
  if (account.last_check) {
    return [{ status: account.status, latency_ms: 0, created_at: account.last_check }];
  }
  return [];
}

function lastBeat(account) {
  const beats = beatsFor(account);
  return beats.length ? beats[beats.length - 1] : null;
}

const COUNTRY_NAMES = {
  VN: "越南", JP: "日本", US: "美国", HK: "香港", TW: "台湾", SG: "新加坡",
  TH: "泰国", PH: "菲律宾", ID: "印尼", MY: "马来西亚", KR: "韩国", GB: "英国",
  DE: "德国", FR: "法国", NL: "荷兰", RU: "俄罗斯", BR: "巴西", IN: "印度",
  TR: "土耳其", AE: "阿联酋", CA: "加拿大", AU: "澳大利亚", CN: "中国",
};

function flagOf(code) {
  const upper = String(code || "").toUpperCase();
  if (!/^[A-Z]{2}$/.test(upper)) return "";
  return String.fromCodePoint(...[...upper].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

function networkLabel(network) {
  const net = network || {};
  const country = String(net.country || "").toUpperCase();
  const name = COUNTRY_NAMES[country] || country;
  if (net.mode === "env_proxy") {
    const host = net.host ? `${net.host}${net.port ? `:${net.port}` : ""}` : "环境代理";
    return [flagOf(country) || "", name, host].filter(Boolean).join(" · ");
  }
  return "直连 · 本机出口";
}

function metric(label, value, className = "") {
  const item = el("div", "pool-metric");
  item.append(el("span", "", label));
  item.append(el("strong", className, value));
  return item;
}

function heartbeatBlock(account) {
  const beats = beatsFor(account);
  const display = beats.length ? beats : (account.last_check
    ? [{ status: account.status, latency_ms: 0, created_at: account.last_check }]
    : []);
  const box = el("div", "pool-heartbeat");
  const head = el("div", "pool-heartbeat-head");
  head.append(el("span", "", "心跳检测"));
  head.append(el("span", "", display.length ? `${beats.length || 1} 次 · ${formatTime(display[display.length - 1].created_at)}` : "等待检测"));
  box.append(head);

  const bars = el("div", "pool-heartbeat-bars");
  bars.setAttribute("role", "img");
  const summary = display.length
    ? display.map((beat) => `${statusOf({ status: beat.status }).label} ${formatLatency(beat.latency_ms)}`).join("，")
    : "还没有心跳记录";
  bars.setAttribute("aria-label", `最近心跳：${summary}`);
  const maxLatency = Math.max(80, ...display.map((beat) => Number(beat.latency_ms) || 0));
  const empty = Math.max(0, HEARTBEAT_SLOTS - display.length);
  for (let index = 0; index < empty; index += 1) {
    bars.append(el("span", "pool-beat is-empty"));
  }
  display.forEach((beat, index) => {
    const tone = beatTone(beat.status);
    const bar = el("span", `pool-beat is-${tone}${index === display.length - 1 ? " is-latest" : ""}`);
    const latency = Number(beat.latency_ms) || 0;
    const height = 4 + Math.round(15 * Math.min(1, latency / maxLatency));
    bar.style.setProperty("--bar-h", `${height}px`);
    bar.title = `${formatTime(beat.created_at)} · ${statusOf({ status: beat.status }).label} · ${formatLatency(beat.latency_ms)}`;
    bars.append(bar);
  });
  box.append(bars);

  const latest = display[display.length - 1];
  const foot = el("div", "pool-heartbeat-foot");
  foot.append(el("span", "", `间隔 ${Number(account.check_interval) || 60}s`));
  foot.append(el("span", "", latest ? `${statusOf({ status: latest.status }).label} · ${formatLatency(latest.latency_ms)}` : "—"));
  box.append(foot);
  return box;
}

const ACTION_ICONS = {
  login: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 4H5v16h5"/><path d="M14 8l4 4-4 4"/><path d="M18 12H9"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4l2-6 4 12 2-6h6"/></svg>',
  credentials: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="12" r="3"/><path d="M11 12h9"/><path d="M17 12v3"/><path d="M20 12v2"/></svg>',
  clear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M8 8l8 8"/></svg>',
  delete: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/></svg>',
};

function actionButton(label, action, account, { disabled = false, tone = "" } = {}) {
  const classes = ["pool-card-action"];
  if (action === "login") classes.push("is-primary");
  if (tone) classes.push(tone);
  const button = el("button", classes.join(" "));
  button.type = "button";
  button.title = label;
  button.dataset.poolAction = action;
  button.dataset.poolId = account.id;
  if (ACTION_ICONS[action]) {
    const icon = el("span", "pool-action-icon");
    icon.innerHTML = ACTION_ICONS[action];
    button.append(icon);
  }
  button.append(document.createTextNode(label));
  button.disabled = disabled;
  return button;
}

function cardFor(account) {
  const status = statusOf(account);
  const card = el("article", "pool-card");
  card.dataset.poolId = account.id;
  card.dataset.status = status.tone;

  const head = el("header", "pool-card-head");
  const identity = el("div", "pool-card-identity");
  identity.append(el("span", "pool-card-dot"));
  const names = el("div", "pool-card-names");
  names.append(el("h3", "", account.name || account.username || "未命名"));
  const envLabel = account.env_no
    ? `ADS ${account.env_no}`
    : (account.env_id ? "ADS 环境" : "");
  names.append(el("p", "", [account.username, account.base_url, envLabel].filter(Boolean).join(" · ")));
  identity.append(names);
  head.append(identity);
  head.append(el("span", "pool-platform", account.platform_label || account.platform));
  card.append(head);

  const latest = lastBeat(account);
  const metrics = el("div", "pool-card-metrics");
  metrics.append(metric("状态", status.label, `is-${status.tone}`));
  metrics.append(metric("会话", account.has_session ? "已保存" : "无会话",
    account.has_session ? "is-accent" : "is-muted"));
  metrics.append(metric("最近响应", latest ? formatLatency(latest.latency_ms) : "—"));
  metrics.append(metric("最近检测", account.last_check
    ? formatClock(account.last_check)
    : (latest ? formatClock(latest.created_at) : "—")));
  if (account.platform === "crown_member") {
    const hasBalance = account.balance !== null && account.balance !== undefined && account.balance !== "";
    const currency = account.balance_currency || "";
    metrics.append(metric("会员余额", hasBalance
      ? `${formatBalance(account.balance)}${currency ? ` ${currency}` : ""}`
      : "未知", hasBalance ? "is-accent" : "is-muted"));
    metrics.append(metric("余额快照", account.balance_checked_at
      ? formatClock(account.balance_checked_at)
      : "尚无记录", "is-muted"));
  }
  card.append(metrics);

  if (account.platform === "crown_member") {
    const net = networkLabel(account.network);
    const row = el("p", "pool-card-network");
    row.append(el("span", "pool-card-network-icon", "⇄"));
    const isProxy = (account.network || {}).mode === "env_proxy";
    const text = el("span", "", net);
    text.className = isProxy ? "pool-card-network-text is-proxy" : "pool-card-network-text is-direct";
    row.append(text);
    row.title = isProxy ? "出口：AdsPower 环境绑定的代理" : "出口：本机网络（该环境未配置代理）";
    card.append(row);
  }

  card.append(heartbeatBlock(account));

  const message = account.status === "error" || account.status === "expired"
    ? account.last_error || account.status_message
    : account.status_message;
  if (message) {
    const note = el("p", "pool-card-message", message);
    note.title = message;
    card.append(note);
  }

  const actions = el("footer", "pool-card-actions");
  const busy = ["logging_in", "checking"].includes(account.status);
  const envPending = account.session_backend === "adspower"
    && !["ps38_backend", "crown_member"].includes(account.platform);
  const primary = account.managed_by === "collector";
  actions.append(actionButton("登录", "login", account, { disabled: busy || envPending }));
  actions.append(actionButton("检测", "check", account, { disabled: busy || envPending }));
  actions.append(actionButton("凭据", "credentials", account, { disabled: busy }));
  if (primary || (account.session_backend === "adspower"
      && ["ps38_backend", "crown_member"].includes(account.platform))) {
    actions.append(actionButton("重登", "relogin", account, { disabled: busy }));
  }
  if (!primary) {
    actions.append(actionButton("清除", "clear", account, { disabled: busy }));
    actions.append(actionButton("删除", "delete", account, { disabled: busy, tone: "danger" }));
  }
  card.append(actions);
  return card;
}

function renderCards(accounts) {
  const grid = $("poolCards");
  if (!grid) return;
  grid.replaceChildren();
  for (const account of accounts) grid.append(cardFor(account));
}

const STATUS_RANK = {
  online: 0,
  logging_in: 1,
  awaiting_login: 1,
  checking: 1,
  expired: 2,
  error: 2,
  idle: 3,
  disabled: 3,
};

function filteredAccounts(accounts) {
  const term = view.search.trim().toLowerCase();
  const filtered = accounts.filter((account) => {
    if (view.filter === "online" && account.status !== "online") return false;
    if (view.filter === "pending"
      && !["logging_in", "awaiting_login", "checking"].includes(account.status)) return false;
    if (view.filter === "error" && !["expired", "error"].includes(account.status)) return false;
    if (!term) return true;
    return [account.name, account.username, account.base_url, account.platform_label]
      .filter(Boolean).join(" ").toLowerCase().includes(term);
  });
  const sorted = [...filtered];
  sorted.sort((a, b) => {
    if (view.sort === "status") {
      return (STATUS_RANK[a.status] ?? 9) - (STATUS_RANK[b.status] ?? 9)
        || String(a.name).localeCompare(String(b.name), "zh-CN");
    }
    if (view.sort === "checked") {
      const left = new Date(String(a.last_check || "")).getTime() || 0;
      const right = new Date(String(b.last_check || "")).getTime() || 0;
      return right - left;
    }
    if (view.sort === "name") {
      return String(a.name || "").localeCompare(String(b.name || ""), "zh-CN");
    }
    if (view.sort === "platform") {
      return String(a.platform_label || "").localeCompare(String(b.platform_label || ""), "zh-CN")
        || String(a.name).localeCompare(String(b.name), "zh-CN");
    }
    const aPrimary = a.managed_by === "collector" ? 0 : 1;
    const bPrimary = b.managed_by === "collector" ? 0 : 1;
    return aPrimary - bPrimary
      || String(a.created_at || "").localeCompare(String(b.created_at || ""));
  });
  return sorted;
}

function setFilterActive() {
  for (const button of document.querySelectorAll("[data-pool-filter]")) {
    button.classList.toggle("active", button.dataset.poolFilter === view.filter);
    button.setAttribute("aria-pressed", button.dataset.poolFilter === view.filter ? "true" : "false");
  }
}

function resetView() {
  view.search = "";
  view.filter = "all";
  view.sort = "default";
  const search = $("poolSearch");
  if (search) search.value = "";
  const sort = $("poolSort");
  if (sort) sort.value = "default";
  setFilterActive();
  render();
}

function renderLogs(logs) {
  const list = $("poolLogList");
  if (!list) return;
  list.replaceChildren();
  const count = $("poolLogCount");
  if (count) count.textContent = logs.length ? ` (${logs.length})` : "";
  if (!logs.length) {
    list.append(el("p", "pool-log-empty", "还没有运行日志"));
    return;
  }
  for (const item of logs.slice(0, 12)) {
    const row = el("div", `pool-log-entry pool-log-${item.status === "success" ? "ok" : "warn"}`);
    row.append(el("time", "", formatTime(item.created_at)));
    row.append(el("span", "", item.message));
    list.append(row);
  }
}

let poolTip = null;

function poolTipNode() {
  if (poolTip && poolTip.isConnected) return poolTip;
  poolTip = el("div", "pool-tip");
  poolTip.setAttribute("role", "tooltip");
  poolTip.hidden = true;
  document.body.append(poolTip);
  return poolTip;
}

function showPoolTip(target, text) {
  if (!text) return;
  const tip = poolTipNode();
  tip.textContent = text;
  tip.hidden = false;
  const rect = target.getBoundingClientRect();
  const gap = 8;
  const width = tip.offsetWidth;
  const height = tip.offsetHeight;
  let left = rect.left + rect.width / 2 - width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
  let top = rect.bottom + gap;
  if (top + height > window.innerHeight - 8) top = rect.top - height - gap;
  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(Math.max(8, top))}px`;
}

function hidePoolTip() {
  if (poolTip && poolTip.isConnected) poolTip.hidden = true;
}

function monitorBeatTone(beat) {
  return beat?.ok ? "up" : "down";
}

function renderMonitorCard() {
  const card = $("poolMonitorCard");
  if (!card) return;
  const data = monitor.data;
  card.replaceChildren();
  card.dataset.monitor = "endpoints";

  const head = el("header", "pool-card-head");
  const identity = el("div", "pool-card-identity");
  identity.append(el("span", "pool-card-dot"));
  const names = el("div", "pool-card-names");
  names.append(el("h3", "", "接口监控"));
  const summary = data?.ok === false ? "不可用" : data?.endpoints?.length
    ? `在线 ${data.summary?.online ?? 0} / ${data.summary?.total ?? data.endpoints.length}`
    : "等待检测";
  names.append(el("p", "", summary));
  identity.append(names);
  head.append(identity);
  head.append(el("span", "pool-platform", "HTTP"));
  card.append(head);

  const list = el("div", "pool-monitor-rows");
  const items = data?.endpoints || [];
  if (!items.length) {
    list.append(el("p", "pool-monitor-empty", data?.error || "还没有监控目标"));
  }
  for (const item of items) {
    const row = el("div", "pool-monitor-row");
    row.dataset.status = item.status;
    row.dataset.tip = `${item.url}${item.error ? `｜${item.error}` : "｜正常"}${item.latency == null ? "" : `｜${item.latency}ms`}`;

    const host = el("span", "pool-monitor-host");
    host.textContent = item.host;
    row.append(host);

    // 心跳条与会员卡片保持一致：柱高按延迟缩放，绿=通、红=断。
    const beats = item.beats || [];
    const slots = 12;
    const display = beats.slice(0, slots).slice().reverse();
    const bars = el("span", "pool-monitor-bars");
    bars.setAttribute("aria-hidden", "true");
    const maxLatency = Math.max(80, ...display.map((beat) => Number(beat.latency) || 0));
    const empty = Math.max(0, slots - display.length);
    for (let index = 0; index < empty; index += 1) bars.append(el("i", "pool-beat is-empty"));
    display.forEach((beat, index) => {
      const bar = el("i", `pool-beat ${beat.ok ? "is-online" : "is-error"}`);
      const latency = Number(beat.latency) || 0;
      const height = 4 + Math.round(15 * Math.min(1, latency / maxLatency));
      bar.style.setProperty("--bar-h", `${height}px`);
      if (index === display.length - 1) bar.classList.add("is-latest");
      bar.dataset.tip = `${beat.at}｜${beat.ok ? "正常" : beat.error || "失败"}｜${latency}ms`;
      bars.append(bar);
    });
    row.append(bars);

    const right = el("span", "pool-monitor-right");
    const rate = el("span", `pool-monitor-rate is-${item.status}`);
    rate.textContent = item.samples ? `${Math.round(item.uptime)}%` : "—";
    right.append(rate);
    const cost = el("span", "pool-monitor-cost");
    cost.textContent = item.latency == null ? "—" : formatLatency(item.latency);
    right.append(cost);
    row.append(right);
    list.append(row);
  }
  card.append(list);

}

async function loadMonitor(force = false) {
  const now = Date.now();
  if (!force && monitor.data && now - monitor.fetchedAt < 8000) return;
  try {
    const response = await fetch(`/api/monitor?t=${now}`, { cache: "no-store" });
    monitor.data = await response.json();
  } catch (error) {
    monitor.data = { ok: false, error: "接口监控请求失败" };
  }
  monitor.fetchedAt = Date.now();
  renderMonitorCard();
}

function render() {
  const accounts = snapshot?.accounts || [];
  const visible = filteredAccounts(accounts);
  renderCards(visible);

  const summary = snapshot?.summary || {};
  setText("poolTotal", summary.total ?? accounts.length);
  setText("poolOnline", summary.online ?? 0);
  setText("poolPending", summary.pending ?? 0);
  setText("poolError", summary.error ?? 0);
  setText("poolInterval", snapshot?.check_interval ?? 60);
  setText("poolFilterCount", `${visible.length} / ${accounts.length} 个账号`);

  const noAccounts = accounts.length === 0;
  const noMatch = accounts.length > 0 && visible.length === 0;
  const empty = $("poolEmpty");
  if (empty) empty.hidden = !noAccounts && !noMatch;
  const grid = $("poolCards");
  if (grid) grid.hidden = noAccounts || noMatch;
  if (noMatch) {
    setText("poolEmptyTitle", "没有匹配的账号");
    setText("poolEmptyText", "调整搜索词或筛选条件后再试。");
    const action = $("poolEmptyAdd");
    if (action) action.textContent = "清除筛选";
  } else {
    setText("poolEmptyTitle", "还没有账号");
    setText("poolEmptyText", "新增账号后，这里会显示登录、会话与心跳状态。");
    const action = $("poolEmptyAdd");
    if (action) action.textContent = "新增账号";
  }
  renderLogs(snapshot?.logs || []);
}

async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const { response, payload } = await fetchJson("/api/pool", { cache: "no-store" },
      { timeoutMs: 8000, allowInvalidJson: true });
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    snapshot = payload;
    render();
    if (isPoolPage()) loadMonitor();
  } catch (error) {
    if (!snapshot) {
      const empty = $("poolEmpty");
      if (empty) empty.hidden = false;
    }
  } finally {
    loading = false;
  }
}

async function runAction(action, accountId) {
  const labels = {
    login: "已发起登录，请在浏览器完成登录/验证",
    check: "已开始检测会话",
    clear: "已清除保存的会话",
    delete: "账号已删除",
  };
  if (action === "clear" || action === "delete") {
    // Native window.confirm can be suppressed by the browser, which makes
    // delete look broken with no request at all. Use an in-page double click.
    const now = Date.now();
    const pending = pendingDestructive;
    const same = pending
      && pending.action === action
      && pending.id === accountId
      && now - pending.at < 5000;
    if (!same) {
      pendingDestructive = { action, id: accountId, at: now };
      showToast(action === "delete" ? "再点一次「删除」确认" : "再点一次「清除」确认");
      return;
    }
    pendingDestructive = null;
  }
  try {
    if (action === "relogin") {
      const payload = accountId === "primary-ps38"
        ? await postJson("/api/actions/login", {})
        : await postJson("/api/pool/login", { id: accountId, force: true });
      showToast(payload.message || "已发起重新登录");
      refresh();
      return;
    }
    const payload = await postJson(`/api/pool/${action}`, { id: accountId });
    showToast(payload.message || labels[action] || "操作完成");
    refresh();
  } catch (error) {
    showToast(`操作失败：${error?.message || error}`);
  }
}

function onCardsClick(event) {
  const button = event.target.closest("[data-pool-action]");
  if (!button) return;
  if (button.dataset.poolAction === "add") {
    openDialog();
    return;
  }
  if (button.dataset.poolAction === "credentials") {
    openCredentials(button.dataset.poolId);
    return;
  }
  runAction(button.dataset.poolAction, button.dataset.poolId);
}

function platformDefaults(platform) {
  return PLATFORM_DEFAULTS[platform] || PLATFORM_DEFAULTS.ps38_backend;
}

function applyPlatformDefaults() {
  const platform = $("poolPlatform")?.value || "ps38_backend";
  // 皇冠会员只需要账号密码：站点地址由监控里延迟最低的入口自动填入。
  const memberOnly = platform === "crown_member";
  for (const field of document.querySelectorAll("[data-pool-field]")) {
    field.hidden = memberOnly;
  }
  const base = $("poolBaseUrl");
  if (base) base.required = !memberOnly;
  const defaults = platformDefaults(platform);
  const login = $("poolLoginPath");
  const dashboard = $("poolDashboardPath");
  const known = Object.values(PLATFORM_DEFAULTS).map((item) => item.base_url).filter(Boolean);
  if (base && (!base.value.trim() || known.includes(base.value.trim()))) {
    base.value = defaults.base_url;
  }
  if (login) login.value = defaults.login_path;
  if (dashboard) dashboard.value = defaults.dashboard_path;
  const hint = $("poolFormHint");
  if (hint) {
    hint.textContent = platform === "ps38_backend"
      ? "PS38 后台使用独立浏览器目录；主账号由采集器维护，这里新建的是额外账号。"
      : platform === "crown_member"
        ? "皇冠会员只需填写账号与密码；登录时自动使用接口监控里延迟最低的站点。"
        : "皇冠后台登录后保存浏览器会话；心跳通过无头浏览器检查后台登录界面。";
  }
}

function openDialog() {
  const dialog = $("poolDialog");
  if (!dialog) return;
  const form = $("poolForm");
  form?.reset();
  const error = $("poolFormError");
  if (error) error.textContent = "";
  const platform = $("poolPlatform");
  if (platform) platform.value = "ps38_backend";
  const interval = $("poolIntervalInput");
  if (interval) interval.value = String(snapshot?.check_interval || 60);
  applyPlatformDefaults();
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeDialog() {
  const dialog = $("poolDialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

/* ── 账号凭据 ──────────────────────────────────────────────────────────── */

function openCredentials(accountId) {
  const account = (snapshot?.accounts || []).find((item) => item.id === accountId);
  const dialog = $("poolCredDialog");
  if (!account || !dialog) return;
  credentialAccountId = accountId;
  const username = account.credential_username
    || (account.managed_by === "collector"
      ? (account.login_id || "")
      : (account.username && account.username !== "手动登录" ? account.username : ""));
  const userInput = $("poolCredUsername");
  if (userInput) userInput.value = username;
  const password = $("poolCredPassword");
  if (password) password.value = "";
  const totp = $("poolCredTotp");
  if (totp) totp.value = "";
  const hint = $("poolCredHint");
  if (hint) {
    const parts = [
      account.has_credentials ? "密码已保存" : "还没有保存密码",
      account.has_totp ? "TOTP 已保存" : "TOTP 未设置",
      account.env_no ? `ADS 环境 ${account.env_no}` : "",
    ].filter(Boolean);
    hint.textContent = `${parts.join(" · ")}。凭据只保存在本机 0600 文件，不通过接口返回；PS38 会话失效时会尝试自动登录，识别失败转人工。`;
  }
  const error = $("poolCredError");
  if (error) error.textContent = "";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeCredentials() {
  const dialog = $("poolCredDialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

async function submitCredentials(event) {
  event.preventDefault();
  const error = $("poolCredError");
  if (error) error.textContent = "";
  const payload = {
    id: credentialAccountId,
    username: $("poolCredUsername")?.value.trim() || "",
    password: $("poolCredPassword")?.value || "",
    totp_secret: $("poolCredTotp")?.value.trim() || "",
  };
  if (!payload.username) {
    if (error) error.textContent = "请填写登录账号";
    return;
  }
  try {
    const result = await postJson("/api/pool/credentials", payload);
    closeCredentials();
    showToast(result.message || "账号凭据已保存");
    refresh();
  } catch (exc) {
    if (error) error.textContent = String(exc?.message || exc);
  }
}

/* ── ADS 账号导入 ──────────────────────────────────────────────────────── */

function adsDialog() {
  return $("poolAdsDialog");
}

function adsSiteDefault(platform) {
  if (platform === "ps38_backend") return "https://www.ps38ag.com";
  const accounts = snapshot?.accounts || [];
  const member = accounts.find((item) => item.platform === "crown_member" && item.base_url);
  return member?.base_url || "";
}

function applyAdsPlatformDefaults() {
  const platform = $("poolAdsPlatform")?.value || "crown_member";
  const site = $("poolAdsSite");
  if (!site) return;
  const known = new Set(
    [...(snapshot?.accounts || []).map((item) => item.base_url), "https://www.ps38ag.com"]
      .filter(Boolean),
  );
  if (!site.value.trim() || known.has(site.value.trim())) {
    site.value = adsSiteDefault(platform);
  }
}

function adsFiltered() {
  const term = String($("poolAdsSearch")?.value || "").trim().toLowerCase();
  if (!term) return ads.items;
  return ads.items.filter((item) => (
    [item.profile_no, item.name, item.account, item.group_name, item.profile_id]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(term)
  ));
}

function adsImportable(item) {
  return !item.bound_account;
}

function updateAdsSelection() {
  const importable = adsFiltered().filter(adsImportable);
  const filteredSelected = importable.filter((item) => ads.selected.has(item.profile_id)).length;
  const selectedCount = ads.items.filter(
    (item) => adsImportable(item) && ads.selected.has(item.profile_id),
  ).length;
  const selectAll = $("poolAdsSelectAll");
  if (selectAll) {
    selectAll.checked = importable.length > 0 && filteredSelected === importable.length;
    selectAll.indeterminate = filteredSelected > 0 && filteredSelected < importable.length;
    selectAll.disabled = ads.loading || importable.length === 0;
  }
  const button = $("poolAdsImport");
  if (button) {
    button.disabled = ads.loading || selectedCount === 0;
    button.textContent = selectedCount ? `导入所选（${selectedCount}）` : "导入所选";
  }
  const status = $("poolAdsStatus");
  if (status && !ads.loading) {
    if (ads.error) {
      status.textContent = "读取 AdsPower 环境失败";
      return;
    }
    const summary = ads.summary || {};
    const needAccount = summary.need_account ? ` · 需填账号 ${summary.need_account}` : "";
    status.textContent = `共 ${summary.total ?? ads.items.length} 个环境 · 已绑定 ${summary.bound ?? 0} · 已过期 ${summary.expired ?? 0} · 可导入 ${summary.available ?? 0}${needAccount}`;
  }
}

function adsBadge(text, tone) {
  return el("span", `pool-ads-badge${tone ? ` is-${tone}` : ""}`, text);
}

function renderAdsList() {
  const list = $("poolAdsList");
  if (!list) return;
  list.replaceChildren();
  if (ads.loading) {
    list.append(el("p", "pool-ads-empty", "正在读取 AdsPower 环境…"));
    updateAdsSelection();
    return;
  }
  if (ads.error) {
    list.append(el("p", "pool-ads-empty", ads.error));
    updateAdsSelection();
    return;
  }
  const rows = adsFiltered();
  if (!rows.length) {
    list.append(el("p", "pool-ads-empty", "没有匹配的环境"));
    updateAdsSelection();
    return;
  }
  for (const item of rows) {
    const bound = Boolean(item.bound_account);
    const row = el("div", "pool-ads-row");
    row.dataset.envId = item.profile_id;
    if (bound) row.classList.add("is-bound");
    if (item.expired) row.classList.add("is-expired");

    const check = el("input", "pool-ads-check");
    check.type = "checkbox";
    check.checked = ads.selected.has(item.profile_id);
    check.disabled = bound;
    check.setAttribute("aria-label", `选择环境 ${item.profile_no || item.profile_id}`);
    check.addEventListener("change", () => {
      if (check.checked) ads.selected.add(item.profile_id);
      else ads.selected.delete(item.profile_id);
      updateAdsSelection();
    });

    const main = el("div", "pool-ads-main");
    main.append(el("strong", "", item.name || item.profile_id));
    const meta = [
      item.profile_no ? `No.${item.profile_no}` : "",
      item.account ? `账号 ${item.account}` : "账号未识别",
      item.group_name || "",
      item.proxy_host ? `${item.proxy_host}${item.proxy_port ? `:${item.proxy_port}` : ""}` : "",
    ].filter(Boolean);
    main.append(el("span", "pool-ads-meta", meta.join(" · ")));

    const username = el("input", "pool-ads-username");
    username.type = "text";
    username.value = ads.usernames[item.profile_id] ?? item.account ?? "";
    username.placeholder = "登录账号";
    username.disabled = bound;
    username.setAttribute("aria-label", `环境 ${item.profile_no || item.profile_id} 的登录账号`);
    username.addEventListener("input", () => {
      ads.usernames[item.profile_id] = username.value;
    });

    const badges = el("div", "pool-ads-badges");
    if (bound) badges.append(adsBadge(`已绑定 ${item.bound_account.name}`, "bound"));
    if (item.expired) badges.append(adsBadge("已过期", "expired"));
    if (item.stopped) badges.append(adsBadge("已停用"));
    if (!bound && !item.account) badges.append(adsBadge("需填账号", "warn"));
    if (!bound && item.account && !item.expired && !item.stopped) {
      badges.append(adsBadge("可导入", "ready"));
    }

    row.append(check, main, username, badges);
    list.append(row);
  }
  updateAdsSelection();
}

async function loadAdsProfiles(refresh = false) {
  if (ads.loading) return;
  ads.loading = true;
  ads.error = "";
  const error = $("poolAdsError");
  if (error) error.textContent = "";
  const status = $("poolAdsStatus");
  if (status) status.textContent = "正在读取 AdsPower 环境…";
  renderAdsList();
  try {
    const url = `/api/adspower/profiles${refresh ? "?refresh=1" : ""}`;
    const { response, payload } = await fetchJson(url, { cache: "no-store" },
      { timeoutMs: 25000, allowInvalidJson: true });
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    ads.items = Array.isArray(payload.items) ? payload.items : [];
    ads.summary = payload.summary || {};
    const available = new Set(
      ads.items.filter((item) => !item.bound_account).map((item) => item.profile_id),
    );
    ads.selected = new Set([...ads.selected].filter((id) => available.has(id)));
    const site = $("poolAdsSite");
    if (site && !site.value.trim()) site.value = adsSiteDefault($("poolAdsPlatform")?.value);
  } catch (exc) {
    ads.items = [];
    ads.summary = {};
    const message = String(exc?.message || exc);
    ads.error = message === "HTTP 404"
      ? "服务端没有加载 ADS 导入接口，请重启程序后再试"
      : message;
    if (error) error.textContent = ads.error;
  } finally {
    ads.loading = false;
    renderAdsList();
    updateAdsSelection();
  }
}

function openAdsDialog() {
  const dialog = adsDialog();
  if (!dialog) return;
  ads.selected = new Set();
  ads.usernames = {};
  ads.error = "";
  const search = $("poolAdsSearch");
  if (search) search.value = "";
  const platform = $("poolAdsPlatform");
  if (platform) platform.value = "auto";
  const site = $("poolAdsSite");
  if (site) site.value = adsSiteDefault("crown_member");
  const error = $("poolAdsError");
  if (error) error.textContent = "";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  loadAdsProfiles(true);
}

function closeAdsDialog() {
  const dialog = adsDialog();
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

async function submitAdsImport() {
  const error = $("poolAdsError");
  if (error) error.textContent = "";
  const selected = ads.items
    .filter((item) => !item.bound_account && ads.selected.has(item.profile_id))
    .map((item) => item.profile_id);
  if (!selected.length) {
    if (error) error.textContent = "请先选择要导入的环境";
    return;
  }
  const platform = $("poolAdsPlatform")?.value || "auto";
  const baseUrl = $("poolAdsSite")?.value.trim() || "";
  if (["crown_member", "crown_backend"].includes(platform) && !baseUrl) {
    if (error) error.textContent = "请填写站点地址";
    return;
  }
  const usernames = {};
  for (const envId of selected) {
    const item = ads.items.find((entry) => entry.profile_id === envId);
    usernames[envId] = String(ads.usernames[envId] ?? item?.account ?? "").trim();
    if (!usernames[envId]) {
      if (error) error.textContent = `请填写环境 ${item?.profile_no || envId} 的登录账号`;
      return;
    }
  }
  const button = $("poolAdsImport");
  if (button) {
    button.disabled = true;
    button.textContent = "导入中…";
  }
  try {
    const result = await postJson("/api/pool/import-envs", {
      platform,
      base_url: baseUrl,
      env_ids: selected,
      usernames,
      enabled: true,
    });
    const skipped = Array.isArray(result.skipped) ? result.skipped : [];
    if (skipped.length) {
      const detail = skipped
        .map((entry) => `${entry.env_id}：${entry.reason}`)
        .join("；");
      showToast(result.message || "部分环境未导入");
      await loadAdsProfiles(true);
      if (error && !ads.error) {
        error.textContent = `已导入 ${(result.created || []).length} 个，跳过 ${skipped.length} 个。${detail}`;
      }
      refresh();
      return;
    }
    closeAdsDialog();
    showToast(result.message || "已导入所选环境");
    refresh();
  } catch (exc) {
    const message = String(exc?.message || exc);
    if (error) {
      error.textContent = message === "HTTP 404"
        ? "服务端没有加载 ADS 导入接口，请重启程序后再试"
        : message;
    }
  } finally {
    updateAdsSelection();
  }
}

async function submitDialog(event) {
  event.preventDefault();
  const error = $("poolFormError");
  if (error) error.textContent = "";
  const payload = {
    platform: $("poolPlatform")?.value,
    name: $("poolName")?.value.trim(),
    base_url: $("poolBaseUrl")?.value.trim(),
    login_path: $("poolLoginPath")?.value.trim() || "/",
    dashboard_path: $("poolDashboardPath")?.value.trim() || "",
    username: $("poolUsername")?.value.trim(),
    password: $("poolPassword")?.value || "",
    check_interval: Number($("poolIntervalInput")?.value || 60),
  };
  const memberMode = payload.platform === "crown_member";
  if ((!memberMode && !payload.base_url) || !payload.username) {
    if (error) error.textContent = memberMode ? "登录账号为必填项" : "站点地址和登录账号为必填项";
    return;
  }
  try {
    const result = await postJson("/api/pool/save", payload);
    closeDialog();
    showToast(result.message || "账号已保存");
    refresh();
  } catch (exc) {
    if (error) error.textContent = String(exc?.message || exc);
  }
}

async function checkAll() {
  try {
    const result = await postJson("/api/pool/check-all", {});
    showToast(result.message || "已开始检测全部账号");
    refresh();
  } catch (error) {
    showToast(`操作失败：${error?.message || error}`);
  }
}

function isPoolPage() {
  return String(window.location.hash || "").replace(/^#\/?/, "").toLowerCase() === "pool";
}

export function bindPoolEvents() {
  $("poolCheckAll")?.addEventListener("click", checkAll);
  $("poolAdd")?.addEventListener("click", openDialog);
  $("poolImportAds")?.addEventListener("click", openAdsDialog);
  $("poolCredClose")?.addEventListener("click", closeCredentials);
  $("poolCredCancel")?.addEventListener("click", closeCredentials);
  $("poolCredForm")?.addEventListener("submit", submitCredentials);
  $("poolCredDialog")?.addEventListener("click", (event) => {
    if (event.target === $("poolCredDialog")) closeCredentials();
  });
  $("poolAdsClose")?.addEventListener("click", closeAdsDialog);
  $("poolAdsCancel")?.addEventListener("click", closeAdsDialog);
  $("poolAdsRefresh")?.addEventListener("click", () => loadAdsProfiles(true));
  $("poolAdsSearch")?.addEventListener("input", renderAdsList);
  $("poolAdsPlatform")?.addEventListener("change", applyAdsPlatformDefaults);
  $("poolAdsSelectAll")?.addEventListener("change", (event) => {
    const checked = Boolean(event.target.checked);
    for (const item of adsFiltered()) {
      if (!adsImportable(item)) continue;
      if (checked) ads.selected.add(item.profile_id);
      else ads.selected.delete(item.profile_id);
    }
    renderAdsList();
  });
  $("poolAdsImport")?.addEventListener("click", submitAdsImport);
  $("poolAdsDialog")?.addEventListener("click", (event) => {
    if (event.target === $("poolAdsDialog")) closeAdsDialog();
  });
  $("poolEmptyAdd")?.addEventListener("click", () => {
    const accounts = snapshot?.accounts || [];
    if (accounts.length && filteredAccounts(accounts).length === 0) resetView();
    else openDialog();
  });
  $("poolCards")?.addEventListener("click", onCardsClick);
  $("poolCards")?.addEventListener("pointerover", (event) => {
    const target = event.target.closest?.("[data-tip]");
    if (!target) return;
    if (target.contains(event.relatedTarget)) return;
    showPoolTip(target, target.dataset.tip);
  });
  $("poolCards")?.addEventListener("pointerout", (event) => {
    const target = event.target.closest?.("[data-tip]");
    if (!target) return;
    if (target.contains(event.relatedTarget)) return;
    hidePoolTip();
  });
  window.addEventListener("scroll", hidePoolTip, true);
  window.addEventListener("resize", hidePoolTip);
  $("poolSearch")?.addEventListener("input", (event) => {
    view.search = event.target.value;
    render();
  });
  for (const button of document.querySelectorAll("[data-pool-filter]")) {
    button.addEventListener("click", () => {
      view.filter = button.dataset.poolFilter || "all";
      setFilterActive();
      render();
    });
  }
  $("poolSort")?.addEventListener("change", (event) => {
    view.sort = event.target.value || "default";
    render();
  });
  $("poolPlatform")?.addEventListener("change", applyPlatformDefaults);
  $("poolForm")?.addEventListener("submit", submitDialog);
  for (const node of document.querySelectorAll("[data-pool-dialog-close]")) {
    node.addEventListener("click", closeDialog);
  }
  $("poolDialog")?.addEventListener("click", (event) => {
    if (event.target === $("poolDialog")) closeDialog();
  });
  window.addEventListener("ps38:pagenav", (event) => {
    if (event.detail?.page === "pool") refresh();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && isPoolPage()) refresh();
  });
  setFilterActive();
  renderMonitorCard();
  refresh();
  if (refreshTimer) window.clearInterval(refreshTimer);
  refreshTimer = window.setInterval(() => {
    if (!document.hidden && isPoolPage()) refresh();
  }, 5000);
}
