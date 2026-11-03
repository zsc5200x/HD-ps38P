import { pauseFollow, readFollowDetail, readFollowRecords, requestFollowReconcile, resolveFollowResult, saveFollowRule } from "./api.js";

const $ = (id) => document.getElementById(id);
const state = {
  filter: "SUCCEEDED", search: "", limit: 50, offset: 0, total: 0, rows: [], accounts: [],
  rule: null, summary: {}, service: {}, logFilter: "ALL",
  dirty: false, ready: false, loading: false, mutating: false,
  request: 0, detailRequest: 0, lastRefresh: 0, bound: false,
  renderedQuery: null,
  detailId: null, detailDirty: false, detailBusy: false, detailLoading: false,
  detailFeedback: "", detailError: "",
  // 请求占用按组保留；视图代数防止关闭、重开后的迟到响应覆盖新输入。
  detailView: 0, pendingDetails: new Set(),
};
const labels = {
  SUCCEEDED: ["跟单成功", "success"], UNKNOWN: ["待核对", "unknown"],
  GROUPING: ["合组中", "processing"], WAIT_MAPPING: ["等待赛事匹配", "processing"],
  READY: ["等待处理", "processing"], WAIT_ACCOUNT: ["等待可用会员", "processing"],
  WAIT_MARKET: ["等待盘口", "processing"],
  PREPARING: ["核验盘口中", "processing"], RETRY_WAIT: ["等待重试", "processing"],
  PREPARED: ["已匹配盘口", "processing"], SUBMITTING: ["提交中", "processing"],
  REJECTED: ["已拒绝", "failed"], FAILED: ["失败", "failed"],
  SKIPPED: ["已跳过", "muted"], EXPIRED: ["已过期", "muted"],
  LATE_ORDER: ["迟到待核对", "unknown"],
};
const sideLabels = { HOME: "主队", AWAY: "客队", OVER: "大", UNDER: "小" };
const periodLabels = { FT: "全场", HT: "上半场" };
const marketLabels = { HANDICAP: "让球", TOTAL: "大小" };
const terminalStatuses = new Set(["SUCCEEDED", "REJECTED", "FAILED", "SKIPPED", "EXPIRED"]);
const stageLabels = {
  GROUPING: "合组", WAIT_MAPPING: "赛事匹配", READY: "排队", WAIT_ACCOUNT: "分配会员",
  WAIT_MARKET: "等待盘口",
  PREPARING: "查盘", RETRY_WAIT: "重试等待", PREPARED: "盘口就绪", SUBMITTING: "提交",
  SUCCEEDED: "完成", REJECTED: "结束", FAILED: "结束", SKIPPED: "跳过", EXPIRED: "过期",
  UNKNOWN: "待核对", LATE_ORDER: "迟到核对",
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

function number(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
}

function date(value) {
  if (!value) return "—";
  const parsed = new Date(typeof value === "number" && value < 100_000_000_000 ? value * 1000 : value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

function shortTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "—"
    : parsed.toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

function effectiveStatus(row) {
  return row.status === "SUCCEEDED" && !row.receipt_id ? "UNKNOWN" : row.status;
}

function statusBadge(row) {
  let [label, tone] = labels[effectiveStatus(row)] || ["待核对", "unknown"];
  if (row.receipt_source === "manual") label = effectiveStatus(row) === "SUCCEEDED" ? "人工核对成功" : effectiveStatus(row) === "REJECTED" ? "人工确认未成交" : label;
  return node("span", label, `copy-badge copy-badge-${tone}`);
}

function accountName(id) {
  const account = state.accounts.find((item) => item.id === id);
  return account ? account.label || account.name || account.username || id : display(id);
}

function followEligible(account) {
  return account.follow_eligible === true;
}

function lineLabel(value, side, kind) {
  const raw = display(value);
  const signed = kind === "HANDICAP" && Number(value) > 0 ? `+${raw}` : raw;
  return `${sideLabels[side] || display(side)} ${signed}`;
}

function pickLabel(row, source = false) {
  const side = source ? row.source_side : row.target_side;
  const line = source ? row.source_line : row.target_line;
  if (!side) return "待匹配盘口";
  return `${periodLabels[row.period] || display(row.period)} · ${marketLabels[row.market_type] || display(row.market_type)} · ${lineLabel(line, side, row.market_type)}`;
}

function visible() {
  return !$('copyPage')?.hidden && !document.hidden;
}

function ruleVisible() {
  return !$('settingsPage')?.hidden && !document.hidden;
}

function setDirty(value) {
  state.dirty = value;
  $("copyRuleDirty").hidden = !value;
  if (value) $("copyRuleFeedback").textContent = "";
}

function amountHelp() {
  const ratio = $("copyAmountMode").value === "ratio";
  $("copyAmountLabel").textContent = ratio ? "跟单比例（%）" : "固定金额（元）";
  $("copyAmountValue").step = ratio ? "0.01" : "1";
  $("copyAmountValue").min = ratio ? "0.01" : "1";
  $("copyAmountHelp").textContent = ratio
    ? "例如 10 表示原单合计的 10%，向下取整；不足 1 不处理。"
    : "每组使用一次固定金额，按人民币整元计算。";
}

function populateRule(rule) {
  $("copyDirection").value = rule.direction || "forward";
  $("copyAmountMode").value = rule.amount_mode || "fixed";
  $("copyAmountValue").value = rule.amount_value ?? "";
  $("copyMaxAge").value = rule.max_age_seconds ?? 120;
  $("copyMinOdds").value = rule.min_odds ?? "";
  $("copyMaxOdds").value = rule.max_odds ?? "";
  $("copyMembers").value = Array.isArray(rule.member_codes) ? rule.member_codes.join(", ") : "";
  syncRuleControls();
  amountHelp();
}

/** 把隐藏的规则字段同步到分段按钮与开关。 */
function syncRuleControls() {
  const mode = $("copyAmountMode")?.value || "fixed";
  for (const button of document.querySelectorAll("[data-rule-mode]")) {
    button.setAttribute("aria-pressed", String(button.dataset.ruleMode === mode));
  }
  const reverse = ($("copyDirection")?.value || "forward") === "reverse";
  const toggle = $("cfgReverseSwitch");
  if (toggle) toggle.checked = reverse;
  const chip = $("cfgDirectionChip");
  if (chip) {
    chip.textContent = reverse ? "反向跟单" : "正向跟单";
    chip.className = `cfg-chip ${reverse ? "cfg-chip-on" : "cfg-chip-muted"}`;
  }
  const hint = $("cfgDirectionHint");
  if (hint) hint.textContent = reverse ? "开启后按反向处理整组" : "当前按正向处理整组";
}

function renderService(payload, forceForm) {
  const service = payload.service || {};
  const rule = payload.rule || {};
  const enabled = service.enabled === true;
  state.rule = rule;
  state.accounts = Array.isArray(payload.accounts) ? payload.accounts : state.accounts;
  const direction = rule.direction === "reverse" ? "反向" : "正向";
  const amount = rule.amount_mode === "ratio" ? `原单合计 × ${display(rule.amount_value)}%` : `每组 ${display(rule.amount_value)} 元`;
  $("copyRuleSummary").textContent = `${enabled ? "已启用" : "已暂停"} · ${direction} · ${amount} · 20 秒合组 · 号池自动分配`;
  if (!state.dirty || forceForm) populateRule(rule);
  const stateChip = $("cfgRuleState");
  if (stateChip) {
    stateChip.textContent = enabled ? "已启用" : "已暂停";
    stateChip.className = `cfg-chip ${enabled ? "cfg-chip-on" : "cfg-chip-muted"}`;
  }
  const eligibleCount = state.accounts.filter(followEligible).length;
  setText("cfgAccountPoolSummary", eligibleCount
    ? `${eligibleCount} 个自动分配候选；执行前校验会话，忙账号不重复占用。`
    : "暂无自动分配候选；执行前校验会话，忙账号不重复占用。");
  $("copyPause").disabled = !enabled || state.mutating;
  $("copySaveRule").disabled = state.mutating;
  $("copyEnableRule").disabled = state.mutating;
}

function renderSummary(summary) {
  state.summary = summary || {};
  for (const count of document.querySelectorAll("[data-copy-count]")) {
    count.textContent = number(state.summary[count.dataset.copyCount] ?? 0, 0);
  }
  renderBoard();
}

function setText(id, text) {
  const element = $(id);
  if (element) element.textContent = text;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const value = Math.round(seconds);
  if (value < 60) return `${value} 秒`;
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  if (minutes < 60) return `${minutes} 分 ${String(rest).padStart(2, "0")} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${String(minutes % 60).padStart(2, "0")} 分`;
}

function rowSeconds(row) {
  const start = new Date(row.created_at).getTime();
  const end = new Date(row.updated_at || row.created_at).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return (end - start) / 1000;
}

function ruleAmountLabel(rule) {
  if (!rule || !rule.amount_mode) return "—";
  return rule.amount_mode === "ratio"
    ? `原单合计 × ${display(rule.amount_value)}%`
    : `每组 ${display(rule.amount_value)} 元`;
}

function renderBoard() {
  const service = state.service || {};
  const rule = state.rule || {};
  const accounts = state.accounts.filter((account) => !account.platform || account.platform === "crown_member");
  const online = accounts.filter((account) => account.enabled !== false && account.status === "online");
  const eligible = accounts.filter(followEligible);
  const active = Math.min(Number(service.active_tasks || 0), eligible.length);
  setText("copyOnlineMembers", number(online.length, 0));
  setText("copyIdleMembers", number(Math.max(0, eligible.length - active), 0));
  setText("copyBusyMembers", number(Math.min(active, eligible.length), 0));
  setText("copyOfflineMembers", number(accounts.length - online.length, 0));
  setText("copyQueuedTasks", number(service.queued_tasks ?? 0, 0));
  setText("copyActiveTasks", number(service.active_tasks ?? 0, 0));

  const summary = state.summary || {};
  const succeeded = Number(summary.succeeded || 0);
  const processed = Number(summary.total || 0) || ["succeeded", "processing", "failed", "unknown"]
    .reduce((total, key) => total + Number(summary[key] || 0), 0);
  setText("copySuccessRate", processed ? `${Math.round((succeeded / processed) * 1000) / 10}%` : "—");
  const rateNode = $("copySuccessRate");
  if (rateNode) rateNode.className = processed ? (succeeded ? "copy-tone-green" : "copy-tone-danger") : "copy-tone-muted";

  const durations = state.rows
    .filter((row) => terminalStatuses.has(effectiveStatus(row)))
    .map(rowSeconds)
    .filter((value) => value !== null);
  const average = durations.length ? durations.reduce((total, value) => total + value, 0) / durations.length : null;
  setText("copyAvgDuration", average === null ? "—" : formatDuration(average));
  setText("copyOrderTotal", `${number(state.rows.reduce((total, row) => total + Number(row.order_count || 0), 0), 0)} 笔`);

  const stake = summary.confirmed_stake;
  const currency = summary.currency || "CNY";
  setText("copyConfirmedStake", stake === null || stake === undefined || String(stake) === "" ? "—" : `${number(stake)} ${currency}`);
  setText("copyRuleVersion", rule.version ? `v${rule.version}` : "—");

  const params = [
    ["跟单金额", ruleAmountLabel(rule)],
    ["合组窗口", `${number(rule.window_seconds ?? 20, 0)} 秒`],
    ["跟单方向", rule.direction === "reverse" ? "反向跟单" : "正向跟单"],
    ["执行账号", `号池自动分配 · ${number(eligible.length, 0)} 个候选`],
  ];
  const list = $("copyParamList");
  if (list) {
    const fragment = document.createDocumentFragment();
    for (const [label, value] of params) {
      const item = node("div");
      item.append(node("dt", label), node("dd", value));
      fragment.append(item);
    }
    list.replaceChildren(fragment);
  }
  renderPool(accounts);
}

function renderPool(accounts) {
  const list = $("copyPoolList");
  if (!list) return;
  if (!accounts.length) {
    list.replaceChildren(node("p", "号池暂未返回皇冠会员账号，请到「号池管理」添加。", "copy-pool-empty"));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const account of accounts) {
    const name = account.label || account.name || account.username || account.id;
    const own = state.rows.filter((row) => row.account_id === account.id);
    const durations = own
      .filter((row) => terminalStatuses.has(effectiveStatus(row)))
      .map(rowSeconds)
      .filter((value) => value !== null);
    const times = own.map((row) => row.updated_at || row.created_at).filter(Boolean).sort();
    const isOnline = account.enabled !== false && account.status === "online";
    const isEligible = followEligible(account);

    const card = node("article", null, "copy-pool-card");
    const avatar = node("span", String(name).slice(0, 1).toUpperCase(), "copy-pool-avatar");
    avatar.setAttribute("aria-hidden", "true");
    const body = node("div", null, "copy-pool-body");
    const title = node("div", null, "copy-pool-title");
    title.append(node("strong", name));
    const stateTone = account.enabled === false ? "" : isOnline ? "is-online" : "is-error";
    title.append(node("span", account.enabled === false ? "已停用" : isOnline ? "在线" : "掉线", `copy-pool-state ${stateTone}`));

    const metrics = node("div", null, "copy-pool-metrics");
    const rows = [
      ["本页组数", `${number(own.length, 0)} 组`],
      ["最近执行", times.length ? shortTime(times[times.length - 1]) : "—"],
      ["平均耗时", durations.length ? formatDuration(durations.reduce((total, value) => total + value, 0) / durations.length) : "—"],
    ];
    for (const [label, value] of rows) {
      const cell = node("div");
      cell.append(node("span", label), node("b", value));
      metrics.append(cell);
    }
    body.append(title, metrics);
    body.append(node(
      "span",
      isEligible ? "自动分配候选" : isOnline ? "会话校验未通过" : "不参与自动分配",
      `copy-pool-assignment${isEligible ? " is-eligible" : ""}`,
    ));
    card.append(avatar, body);
    fragment.append(card);
  }
  list.replaceChildren(fragment);
}

function metricCell(label, value, sub, className = "") {
  const cell = node("div", null, "copy-card-cell");
  cell.append(node("span", label, "copy-card-label"));
  cell.append(node("strong", value, `copy-card-value ${className}`.trim()));
  if (sub) cell.append(node("small", sub, "copy-card-sub"));
  return cell;
}

function groupCard(row) {
  const status = effectiveStatus(row);
  const tone = labels[status]?.[1] || "unknown";
  const success = status === "SUCCEEDED";
  const home = row.crown_home || row.home || "待匹配主队";
  const away = row.crown_away || row.away || "待匹配客队";

  const card = node("article", null, `copy-card copy-card-${tone}`);
  card.dataset.copyGroup = row.id;
  card.setAttribute("role", "listitem");

  const head = node("header", null, "copy-card-head");
  const ident = node("div", null, "copy-card-ident");
  ident.append(node("span", `订单 #${String(row.id).slice(0, 8)}`, "copy-card-id"));
  ident.append(node("span", `${number(row.order_count || 0, 0)} 单`, "copy-card-chip"));
  if (row.market_type) ident.append(node("span", marketLabels[row.market_type] || row.market_type, "copy-card-chip"));
  if (row.period) ident.append(node("span", periodLabels[row.period] || row.period, "copy-card-chip"));
  ident.append(node("time", date(row.updated_at || row.created_at), "copy-card-time"));
  if (row.league) ident.append(node("span", row.league, "copy-card-league"));
  const badge = statusBadge(row);
  badge.classList.add("copy-card-badge");
  head.append(ident, badge);
  card.append(head);

  const body = node("div", null, "copy-card-body");

  const match = node("div", null, "copy-card-match");
  const teams = node("div", null, "copy-card-teams");
  teams.append(node("strong", home), node("span", "VS", "copy-card-vs"), node("strong", away));
  match.append(teams, node("small", pickLabel(row), "copy-card-pick"));
  body.append(match);

  body.append(metricCell("投注单数", `${number(row.order_count || 0, 0)} 单`, `${number(row.member_count || 0, 0)} 位会员`));

  const hasAcceptedStake = success && row.accepted_stake !== null && row.accepted_stake !== undefined;
  const amount = hasAcceptedStake ? row.accepted_stake : row.target_stake;
  const amountNote = hasAcceptedStake
    ? (row.receipt_source === "manual" ? "人工核对金额" : "回执金额")
    : success
      ? (row.receipt_source === "manual" ? "计划金额 · 未登记实际金额" : "提交金额 · 回执未回显")
      : "计划金额";
  const amountText = amount === null || amount === undefined || amount === ""
    ? "—"
    : `${number(amount)} ${row.currency || ""}`.trim();
  body.append(metricCell("投注金额", amountText, amountNote, success ? "is-success" : ""));

  body.append(metricCell(
    "水位",
    number(row.accepted_odds ?? row.odds, 3),
    success && row.accepted_odds != null ? "回执水位 · 香港盘" : "提交水位 · 香港盘",
  ));

  const statusCell = node("div", null, "copy-card-cell copy-card-status");
  statusCell.append(node("span", "状态", "copy-card-label"));
  const statusTone = success ? "is-success" : tone === "failed" ? "is-danger" : tone === "unknown" ? "is-warning" : "";
  statusCell.append(node("strong", row.status === "GROUPING" ? "20 秒窗口内合组" : (row.message || "—"), `copy-card-value ${statusTone}`.trim()));
  if (row.status === "GROUPING" && row.window_ends_at) {
    const countdown = node("small", "合组剩余 —", "copy-card-sub");
    countdown.dataset.copyCountdown = row.window_ends_at;
    statusCell.append(countdown);
  } else if (row.waiting_reason) {
    statusCell.append(node("small", row.waiting_reason, "copy-card-sub"));
  }
  if (row.next_action_at) statusCell.append(node("small", `下次动作 ${date(row.next_action_at)}`, "copy-card-sub"));
  statusCell.append(node("small", `查盘 ${number(row.prepare_count ?? 0, 0)} · 重试 ${number(row.retry_count ?? 0, 0)} · 核对 ${number(row.reconcile_count ?? 0, 0)}`, "copy-card-sub copy-mono"));
  if (row.error_category) statusCell.append(node("small", `错误分类 ${row.error_category}`, "copy-card-sub"));
  if (row.receipt_id) statusCell.append(node("small", `回执 ${row.receipt_id}`, "copy-card-sub copy-mono"));
  body.append(statusCell);

  const detail = node("button", "明细", "button ghost copy-detail-button");
  detail.type = "button";
  detail.dataset.copyDetail = row.id;
  detail.setAttribute("aria-label", `查看 ${home} 对 ${away} 的跟单明细`);
  body.append(detail);

  card.append(body);
  return card;
}

function logMatches(row, filter) {
  const status = effectiveStatus(row);
  if (filter === "SUCCEEDED") return status === "SUCCEEDED";
  if (filter === "FAILED") return ["REJECTED", "FAILED", "SKIPPED", "EXPIRED"].includes(status);
  if (filter === "RETRY") return Number(row.retry_count || 0) > 0 || Number(row.prepare_count || 0) > 1;
  if (filter === "ERROR") return Boolean(row.error_category) || status === "UNKNOWN" || Boolean(row.waiting_reason);
  return true;
}

function renderTimeline(rows) {
  const body = $("copyLogBody");
  if (!body) return;
  const selected = rows
    .filter((row) => logMatches(row, state.logFilter))
    .sort((a, b) => new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at));
  const fragment = document.createDocumentFragment();
  for (const row of selected) {
    const status = effectiveStatus(row);
    const seconds = terminalStatuses.has(status) ? rowSeconds(row) : null;
    const badgeCell = node("td");
    badgeCell.append(statusBadge(row));
    const tr = node("tr");
    tr.dataset.copyGroup = row.id;
    tr.append(
      node("td", date(row.updated_at || row.created_at), "copy-log-time"),
      node("td", `#${String(row.id).slice(0, 8)}`, "copy-log-order"),
      node("td", stageLabels[status] || display(status)),
      node("td", `${number(row.member_count || 0, 0)} 位`),
      badgeCell,
      node("td", row.waiting_reason || row.message || "—", "copy-log-detail"),
      node("td", seconds === null ? "进行中" : formatDuration(seconds), "copy-number copy-mono"),
    );
    fragment.append(tr);
  }
  body.replaceChildren(fragment);
  const empty = $("copyLogEmpty");
  if (empty) empty.hidden = selected.length > 0;
  for (const button of document.querySelectorAll("[data-copy-log]")) {
    button.setAttribute("aria-pressed", String(button.dataset.copyLog === state.logFilter));
  }
}

function renderRows() {
  const rows = state.rows.filter((row) => state.filter !== "SUCCEEDED" || effectiveStatus(row) === "SUCCEEDED");
  const fragment = document.createDocumentFragment();
  for (const row of rows) fragment.append(groupCard(row));
  $("copyRows").replaceChildren(fragment);
  $("copyRows").hidden = rows.length === 0;
  updateCountdowns();
  renderTimeline(rows);
  renderBoard();
  $("copyEmpty").hidden = rows.length > 0;
  const searching = Boolean(state.search);
  $("copyEmptyTitle").textContent = searching ? "没有找到匹配的记录" : state.filter === "SUCCEEDED" ? "暂无成功跟单" : "暂无此类跟单记录";
  $("copyEmptyText").textContent = searching ? "试试其他球队、订单号或会员，或清空搜索条件。" : state.filter === "SUCCEEDED" ? "收到皇冠明确成功回执后，订单会显示在这里。处理中的任务可在其他分类查看。" : "新来单经过合组和匹配后，处理过程会显示在这里。";
  $("copyEmptyAll").hidden = state.filter === "ALL" && !searching;
  $("copyEmptyAll").textContent = searching ? "清空筛选" : "查看全部记录";
  $("copyMore").hidden = state.offset + state.limit >= state.total;
  $("copyMore").disabled = state.loading;
  $("copyPrevious").hidden = state.offset === 0;
  $("copyPrevious").disabled = state.loading;
  $("copyRecordCount").textContent = rows.length
    ? `第 ${number(state.offset + 1, 0)}–${number(state.offset + rows.length, 0)} 组 · 共 ${number(state.total, 0)} 组`
    : `共 ${number(state.total, 0)} 组`;
}

function updateCountdowns() {
  for (const element of document.querySelectorAll("[data-copy-countdown]")) {
    const deadline = new Date(element.dataset.copyCountdown).getTime();
    if (!Number.isFinite(deadline)) continue;
    const seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    element.textContent = seconds > 0 ? `合组剩余 ${seconds} 秒` : "窗口已结束，等待处理";
  }
}

function updateFilters() {
  for (const button of document.querySelectorAll("[data-copy-filter]")) {
    button.setAttribute("aria-pressed", String(button.dataset.copyFilter === state.filter));
  }
  const hints = {
    SUCCEEDED: "展示已收到成功回执的真实跟单订单。",
    PROCESSING: "查看合组、账号分配、盘口核验、重试等待和提交中的任务。",
    FAILED: "查看未完成的任务及具体原因。",
    UNKNOWN: "这些任务尚未取得确定结果，不会自动重复提交。",
    ALL: "按原单窗口时间展示全部跟单组及处理进度。",
  };
  $("copyRecordsHint").textContent = hints[state.filter];
}

export async function refreshCopyRecords({ force = false, forceForm = false } = {}) {
  if (state.mutating || (!force && (state.loading || !(visible() || ruleVisible())))) return;
  const request = ++state.request;
  const queryKey = JSON.stringify([state.filter, state.search, state.offset]);
  if (queryKey !== state.renderedQuery) {
    state.rows = [];
    state.total = 0;
    renderRows();
    $("copyEmpty").hidden = true;
    $("copyRecordCount").textContent = "正在加载…";
  }
  state.loading = true;
  $("copyRefresh").disabled = true;
  $("copyRows").setAttribute("aria-busy", "true");
  try {
    const payload = await readFollowRecords(state);
    if (request !== state.request) return;
    state.ready = true;
    state.renderedQuery = queryKey;
    state.rows = Array.isArray(payload.rows) ? payload.rows : [];
    state.total = Number(payload.total || 0);
    state.service = payload.service || {};
    renderService(payload, forceForm);
    renderSummary(payload.summary || {});
    if (!$("copyDetailDialog").open && !$("copyRows").contains(document.activeElement)) renderRows();
    $("copyLoadError").hidden = true;
    $("copyUpdatedAt").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · 每 3 秒刷新`;
  } catch (error) {
    if (request !== state.request) return;
    $("copyLoadError").textContent = `${error?.name === "AbortError" ? "读取记录超时" : error?.message || "读取记录失败"}。请刷新重试。`;
    $("copyLoadError").hidden = false;
    if (queryKey !== state.renderedQuery) $("copyRecordCount").textContent = "记录读取失败";
    if (!state.ready) $("copyRecordCount").textContent = "记录读取失败";
  } finally {
    if (request === state.request) {
      state.loading = false;
      state.lastRefresh = Date.now();
      $("copyRefresh").disabled = false;
      $("copyMore").disabled = false;
      $("copyPrevious").disabled = false;
      $("copyRows").setAttribute("aria-busy", "false");
      if ($("copyDetailDialog").open) refreshDetail({ quiet: true });
    }
  }
}

function selectFilter(filter) {
  state.filter = filter;
  state.offset = 0;
  updateFilters();
  refreshCopyRecords({ force: true });
}

function setBusy(busy) {
  state.mutating = busy;
  if (busy) {
    state.loading = false;
    $("copyRefresh").disabled = false;
    $("copyRows").setAttribute("aria-busy", "false");
  }
  for (const id of ["copySaveRule", "copyEnableRule", "copyPause"]) $(id).disabled = busy;
  for (const field of $("copyRuleForm").querySelectorAll("input, select")) {
    if (field.dataset.static !== undefined) continue;
    field.disabled = busy;
  }
  for (const control of $("copyRuleForm").querySelectorAll("[data-rule-mode], #cfgReverseSwitch")) {
    control.disabled = busy;
  }
  $("copyRuleForm").setAttribute("aria-busy", String(busy));
}

function formValue(enabled) {
  return {
    enabled, direction: $("copyDirection").value, amount_mode: $("copyAmountMode").value,
    amount_value: $("copyAmountValue").value,
    max_age_seconds: Number($("copyMaxAge").value),
    min_odds: $("copyMinOdds").value || null, max_odds: $("copyMaxOdds").value || null,
    member_codes: $("copyMembers").value.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean),
    scope: "live",
  };
}

async function saveRule(event) {
  event.preventDefault();
  if (state.mutating || !state.rule) return;
  const enable = event.submitter?.value === "enable";
  const value = formValue(enable || state.rule.enabled === true);
  const errorBox = $("copyFormError");
  errorBox.hidden = true;
  if (value.min_odds !== null && value.max_odds !== null && Number(value.min_odds) > Number(value.max_odds)) {
    errorBox.textContent = "最低水位不能高于最高水位。";
    errorBox.hidden = false;
    errorBox.focus();
    return;
  }
  state.request += 1;
  setBusy(true);
  $("copyRuleFeedback").textContent = "正在保存…";
  try {
    await saveFollowRule(value);
    setDirty(false);
    $("copyRuleFeedback").textContent = enable ? "规则已保存，跟单已启用。" : "规则已保存。";
    setBusy(false);
    await refreshCopyRecords({ force: true, forceForm: true });
  } catch (error) {
    errorBox.textContent = error?.name === "AbortError" ? "保存响应超时，状态尚未确认。请刷新核实后再操作。" : error?.message || "规则保存失败";
    errorBox.hidden = false;
    errorBox.focus();
    $("copyRuleFeedback").textContent = "";
  } finally {
    setBusy(false);
    $("copyPause").disabled = !state.rule?.enabled;
  }
}

async function pauseRule() {
  if (state.mutating) return;
  state.request += 1;
  setBusy(true);
  $("copyFormError").hidden = true;
  $("copyRuleFeedback").textContent = "正在暂停…";
  try {
    await pauseFollow();
    $("copyRuleFeedback").textContent = "跟单已暂停，现有记录已保留。";
    setBusy(false);
    await refreshCopyRecords({ force: true });
  } catch (error) {
    $("copyFormError").textContent = error?.message || "暂停失败，请刷新核实状态";
    $("copyFormError").hidden = false;
    $("copyRuleFeedback").textContent = "";
  } finally {
    setBusy(false);
    $("copyPause").disabled = !state.rule?.enabled;
  }
}

function detailFact(label, value) {
  const wrapper = node("div");
  wrapper.append(node("dt", label), node("dd", display(value)));
  return wrapper;
}

function detailTable(title, columns, rows) {
  const section = node("section", null, "copy-detail-section");
  section.append(node("h3", title));
  if (!rows.length) {
    section.append(node("p", "暂无记录", "copy-detail-muted"));
    return section;
  }
  const wrap = node("div", null, "copy-table-wrap");
  const table = node("table", null, "copy-table copy-detail-table");
  const head = node("thead");
  const headRow = node("tr");
  for (const column of columns) {
    const th = node("th", column);
    th.scope = "col";
    headRow.append(th);
  }
  head.append(headRow);
  const body = node("tbody");
  for (const values of rows) {
    const tr = node("tr");
    for (const value of values) tr.append(node("td", display(value)));
    body.append(tr);
  }
  table.append(head, body);
  wrap.append(table);
  section.append(wrap);
  return section;
}

function statusText(status) {
  return labels[status]?.[0] || display(status);
}

function resolutionDraft() {
  const form = $("copyResolutionForm");
  if (!form) return null;
  return Object.fromEntries(["outcome", "receipt_id", "note", "accepted_stake", "accepted_odds"]
    .map((name) => [name, form.elements.namedItem(name).value]));
}

function detailFeedback(message, failed = false) {
  state[failed ? "detailError" : "detailFeedback"] = message;
  const box = $(failed ? "copyDetailActionError" : "copyDetailActionFeedback");
  if (!box) return;
  box.textContent = message;
  box.hidden = !message;
}

function setDetailBusy(busy) {
  state.detailBusy = busy;
  const content = $("copyDetailContent");
  content.setAttribute("aria-busy", String(busy));
  for (const element of content.querySelectorAll("button, input, select, textarea")) element.disabled = busy;
  if (!busy) updateResolutionFields();
}

function updateResolutionFields() {
  const form = $("copyResolutionForm");
  if (!form) return;
  const successful = form.elements.namedItem("outcome").value === "SUCCEEDED";
  for (const name of ["receipt_id", "accepted_stake", "accepted_odds"]) {
    form.elements.namedItem(name).disabled = state.detailBusy || !successful;
  }
  form.elements.namedItem("receipt_id").required = successful;
}

function resolutionField(label, name, type = "text") {
  const wrapper = node("label", label);
  const field = node(type === "textarea" ? "textarea" : "input");
  field.name = name;
  field.id = `copyResolution-${name}`;
  if (type !== "textarea") field.type = type;
  wrapper.append(field);
  return [wrapper, field];
}

function unknownActions(row, draft) {
  const section = node("section", null, "copy-detail-section copy-resolution");
  section.append(node("h3", "待核对处理"), node("p", "只读核对查询原账号的平台注单。仍无法确认时，请先到平台核实，再登记结果。", "copy-detail-muted"));
  const reconcile = node("button", "只读核对", "button ghost");
  reconcile.id = "copyReconcileRequest";
  reconcile.type = "button";
  reconcile.addEventListener("click", () => performDetailAction("reconcile"));
  section.append(reconcile);
  const form = node("form", null, "copy-resolution-form");
  form.id = "copyResolutionForm";
  form.noValidate = true;
  form.append(node("h4", "人工登记已核实结果"));
  const fields = node("div", null, "copy-fields");
  const outcomeLabel = node("label", "平台核实结果");
  const outcome = node("select");
  outcome.name = "outcome";
  outcome.id = "copyResolution-outcome";
  outcome.required = true;
  for (const [value, label] of [["", "请选择核实结果"], ["SUCCEEDED", "已成功"], ["REJECTED", "已确认未成交"]]) {
    const option = node("option", label);
    option.value = value;
    outcome.append(option);
  }
  outcomeLabel.append(outcome);
  const [receiptLabel, receipt] = resolutionField("平台票号（成功必填）", "receipt_id");
  receipt.maxLength = 128;
  receipt.pattern = "[A-Za-z0-9][A-Za-z0-9_-]{0,127}";
  receipt.autocomplete = "off";
  const [stakeLabel, stake] = resolutionField("实际成交金额（元，可选）", "accepted_stake", "number");
  const [oddsLabel, odds] = resolutionField("实际成交水位（香港盘，可选）", "accepted_odds", "number");
  for (const input of [stake, odds]) { input.min = "0"; input.step = "any"; }
  const [noteLabel, note] = resolutionField("核对依据（3–500 字，必填）", "note", "textarea");
  noteLabel.className = "copy-field-wide";
  note.required = true;
  note.minLength = 3;
  note.maxLength = 500;
  note.rows = 3;
  fields.append(outcomeLabel, receiptLabel, stakeLabel, oddsLabel, noteLabel);
  const help = node("p", "金额和水位仅填写平台实际成交值；留空则保持未知，不用计划金额代替。登记未成交会结束此组。", "copy-detail-muted");
  const submit = node("button", "登记核对结果", "button primary");
  submit.type = "submit";
  submit.id = "copyResolutionSubmit";
  form.append(fields, help, submit);
  if (draft) for (const [name, value] of Object.entries(draft)) form.elements.namedItem(name).value = value;
  form.addEventListener("input", () => { state.detailDirty = true; });
  form.addEventListener("change", () => { state.detailDirty = true; updateResolutionFields(); });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.detailBusy) return;
    const value = resolutionDraft();
    const fail = (message) => detailFeedback(message, true);
    if (!value || !["SUCCEEDED", "REJECTED"].includes(value.outcome)) return fail("请选择已核实的结果。");
    value.note = value.note.trim();
    value.receipt_id = value.receipt_id.trim();
    if (value.note.length < 3 || value.note.length > 500) return fail("请填写 3 至 500 字的核对依据。");
    if (value.outcome === "SUCCEEDED" && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value.receipt_id)) return fail("确认成功必须填写有效的平台票号（字母、数字、下划线或短横线）。");
    for (const name of ["accepted_stake", "accepted_odds"]) {
      const raw = value[name].trim();
      if (value.outcome === "REJECTED") { delete value[name]; continue; }
      if (form.elements.namedItem(name).validity.badInput) return fail("实际金额和水位必须填写有效数字。");
      if (!raw) delete value[name];
      else if (!Number.isFinite(Number(raw)) || Number(raw) <= 0) return fail("填写的实际金额和水位必须大于零。");
      else value[name] = raw;
    }
    if (value.outcome === "REJECTED") value.receipt_id = "";
    performDetailAction("resolution", value);
  });
  section.append(form);
  return section;
}

function renderDetail(payload, draft = null) {
  const row = payload.group || {};
  $("copyDetailTitle").textContent = `${row.crown_home || row.home || "待匹配主队"} vs ${row.crown_away || row.away || "待匹配客队"}`;
  const content = $("copyDetailContent");
  const status = node("div", null, "copy-detail-status");
  status.append(statusBadge(row), node("span", row.message || ""));
  const refresh = node("button", "刷新明细", "button ghost copy-detail-button");
  refresh.type = "button";
  refresh.id = "copyDetailRefresh";
  refresh.addEventListener("click", () => refreshDetail());
  status.append(refresh);
  const feedback = node("p", state.detailFeedback, "copy-detail-feedback");
  feedback.id = "copyDetailActionFeedback";
  feedback.setAttribute("role", "status");
  feedback.hidden = !state.detailFeedback;
  const error = node("p", state.detailError, "copy-alert copy-alert-error");
  error.id = "copyDetailActionError";
  error.setAttribute("role", "alert");
  error.hidden = !state.detailError;
  const manual = row.receipt_source === "manual";
  const facts = node("dl", null, "copy-detail-facts");
  facts.append(
    detailFact("皇冠回执号", row.receipt_id), detailFact("皇冠账号", accountName(row.account_id)),
    detailFact("结果来源", manual ? "人工核对登记" : row.receipt_source === "platform_query" ? "平台注单只读核对" : row.receipt_id ? "平台回执" : "尚待确认"),
    detailFact("等待原因", row.waiting_reason),
    detailFact("平博原单盘口", pickLabel(row, true)), detailFact("皇冠目标盘口", pickLabel(row)),
    detailFact("原单合计", `${number(row.source_stake)} ${row.currency || ""}`),
    detailFact("计划金额", `${number(row.target_stake)} ${row.currency || ""}`),
    detailFact(manual ? "人工核对金额" : "回执金额", effectiveStatus(row) === "SUCCEEDED" ? row.accepted_stake != null ? `${number(row.accepted_stake)} ${row.currency || ""}` : manual ? "未登记实际金额" : "回执未回显金额" : "尚无明确成交结果"),
    detailFact(manual ? "人工核对水位（香港盘）" : "回执水位（香港盘）", effectiveStatus(row) === "SUCCEEDED" ? row.accepted_odds != null ? number(row.accepted_odds, 3) : manual ? "未登记实际水位" : "回执未回显水位" : "—"),
    detailFact("查盘 / 重试 / 核对次数", `${number(row.prepare_count ?? 0, 0)} / ${number(row.retry_count ?? 0, 0)} / ${number(row.reconcile_count ?? 0, 0)}`),
    detailFact("下次动作时间", date(row.next_action_at)), detailFact("错误分类", row.error_category),
    detailFact("创建时间", date(row.created_at)), detailFact("更新时间", date(row.updated_at)),
  );
  const members = (payload.members || []).map((item) => [item.order_id, item.member_user_code || item.member_code || item.member_identity, `${number(item.stake)} ${item.currency || row.currency || ""}`, item.reported_currency || item.currency || row.currency || "—", item.selection || sideLabels[item.side], date(item.created_at || item.created_ms)]);
  const attempts = (payload.attempts || []).map((item) => [date(item.created_at || item.started_at), accountName(item.account_id || row.account_id), item.resolution === "manual" ? `人工核对 · ${statusText(item.status)}` : statusText(item.status), item.receipt_id || "—", item.category || item.error_category || "—", item.message || "—"]);
  const history = (payload.history || []).map((item) => [date(item.at), `${statusText(item.from_status)} → ${statusText(item.to_status)}`, item.kind, item.message]);
  const methods = { automatic: "自动核对", requested: "手动触发只读核对", manual: "人工登记" };
  const reconciliations = (payload.reconciliations || []).map((item) => [date(item.at), methods[item.method] || display(item.method), statusText(item.status), item.receipt_id, item.note || item.message]);
  content.replaceChildren(status, feedback, error, facts);
  if (effectiveStatus(row) === "UNKNOWN") content.append(unknownActions(row, draft));
  content.append(
    detailTable(`来源订单 · ${members.length} 笔`, ["原单号", "会员", "计入金额", "后台币种标签", "方向", "源单时间"], members),
    detailTable("提交尝试", ["时间", "账号", "状态", "回执号", "错误分类", "说明"], attempts),
    detailTable("状态历史", ["时间", "状态变化", "分类", "说明"], history),
    detailTable("核对审计", ["时间", "方式", "结果", "票号", "依据 / 说明"], reconciliations));
  setDetailBusy(state.detailBusy);
}

async function refreshDetail({ quiet = false } = {}) {
  const id = state.detailId;
  if (!id || state.detailLoading || (quiet && (state.detailBusy || state.detailDirty || $("copyResolutionForm")?.contains(document.activeElement)))) return;
  const request = ++state.detailRequest;
  const draft = resolutionDraft();
  state.detailLoading = true;
  try {
    const payload = await readFollowDetail(id);
    if (request !== state.detailRequest || !$("copyDetailDialog").open || (quiet && state.detailDirty)) return;
    renderDetail(payload, resolutionDraft() || draft);
  } catch (error) {
    if (request !== state.detailRequest || quiet) return;
    if ($("copyDetailActionError")) detailFeedback(error?.message || "明细读取失败，请刷新重试。", true);
    else {
      const box = node("p", error?.message || "明细读取失败，请关闭后重试。", "copy-alert copy-alert-error");
      box.setAttribute("role", "alert");
      $("copyDetailContent").replaceChildren(box);
    }
  } finally {
    if (request === state.detailRequest) state.detailLoading = false;
  }
}

async function performDetailAction(action, value) {
  if (!state.detailId || state.pendingDetails.has(state.detailId)) return;
  const id = state.detailId;
  const view = state.detailView;
  const sameView = () => id === state.detailId && view === state.detailView && $("copyDetailDialog").open;
  state.pendingDetails.add(id);
  state.detailRequest += 1;
  state.detailLoading = false;
  const draft = resolutionDraft();
  setDetailBusy(true);
  detailFeedback("", true);
  detailFeedback(action === "reconcile" ? "正在安排只读核对…" : "正在登记核对结果…");
  try {
    const result = action === "reconcile" ? await requestFollowReconcile(id) : await resolveFollowResult(id, value);
    if (!sameView()) return;
    if (action === "resolution") state.detailDirty = false;
    try {
      const payload = await readFollowDetail(id);
      if (sameView()) renderDetail(payload, action === "reconcile" ? draft : null);
    } catch (error) {
      if (sameView()) detailFeedback(`操作已完成，但明细读取失败：${error?.message || "请刷新核实最新状态"}`, true);
    }
    if (sameView()) detailFeedback(result.message || (action === "reconcile" ? "已安排只读核对，可刷新明细查看结果。" : "人工核对结果已登记。"));
  } catch (error) {
    if (sameView()) {
      detailFeedback("");
      detailFeedback(error?.name === "AbortError" ? "操作响应超时，请刷新明细核实状态后再操作。已填写内容保留。" : error?.message || "操作失败，已填写内容保留。", true);
    }
  } finally {
    state.pendingDetails.delete(id);
    setDetailBusy(state.pendingDetails.has(state.detailId));
    refreshCopyRecords({ force: true });
  }
}

async function openDetail(id) {
  state.detailRequest += 1;
  state.detailView += 1;
  state.detailId = id;
  state.detailBusy = state.pendingDetails.has(id);
  state.detailFeedback = "";
  state.detailError = "";
  state.detailDirty = false;
  state.detailLoading = false;
  $("copyDetailTitle").textContent = "订单详情";
  $("copyDetailContent").replaceChildren(node("p", "正在读取明细…", "copy-detail-muted"));
  if (!$("copyDetailDialog").open) $("copyDetailDialog").showModal();
  await refreshDetail();
}

function updateClock() {
  const clock = $("copyBoardClock");
  if (clock) clock.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

export function bindCopyEvents() {
  if (state.bound || !$("copyPage")) return;
  state.bound = true;
  $("copyRefresh").addEventListener("click", () => refreshCopyRecords({ force: true }));
  $("copyRulesToggle").addEventListener("click", () => {
    document.querySelector('[data-page-link="settings"]')?.click();
    window.setTimeout(() => $("copyRuleForm")?.scrollIntoView({ block: "start", behavior: "smooth" }), 120);
  });
  document.querySelectorAll("[data-rule-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      $("copyAmountMode").value = button.dataset.ruleMode;
      syncRuleControls();
      amountHelp();
      setDirty(true);
    });
  });
  $("cfgReverseSwitch")?.addEventListener("change", (event) => {
    $("copyDirection").value = event.target.checked ? "reverse" : "forward";
    syncRuleControls();
    setDirty(true);
  });
  $("copyRuleForm").addEventListener("input", () => setDirty(true));
  $("copyRuleForm").addEventListener("change", () => setDirty(true));
  $("copyAmountMode").addEventListener("change", amountHelp);
  $("copyRuleForm").addEventListener("submit", saveRule);
  $("copyPause").addEventListener("click", pauseRule);
  document.querySelectorAll("[data-copy-filter]").forEach((button) => button.addEventListener("click", () => selectFilter(button.dataset.copyFilter)));
  document.querySelectorAll("[data-copy-log]").forEach((button) => button.addEventListener("click", () => {
    state.logFilter = button.dataset.copyLog;
    renderTimeline(state.rows.filter((row) => state.filter !== "SUCCEEDED" || effectiveStatus(row) === "SUCCEEDED"));
  }));
  let searchTimer;
  $("copySearch").addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.search = $("copySearch").value.trim();
      state.offset = 0;
      refreshCopyRecords({ force: true });
    }, 250);
  });
  $("copyEmptyAll").addEventListener("click", () => {
    $("copySearch").value = "";
    state.search = "";
    selectFilter("ALL");
  });
  $("copyMore").addEventListener("click", () => {
    state.offset += state.limit;
    refreshCopyRecords({ force: true });
  });
  $("copyPrevious").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    refreshCopyRecords({ force: true });
  });
  $("copyRows").addEventListener("click", (event) => {
    const button = event.target.closest("[data-copy-detail]");
    if (button) openDetail(button.dataset.copyDetail);
  });
  $("copyDetailClose").addEventListener("click", () => $("copyDetailDialog").close());
  $("copyDetailDialog").addEventListener("close", () => {
    state.detailRequest += 1;
    state.detailView += 1;
    state.detailId = null;
    state.detailLoading = false;
    state.detailBusy = false;
    renderRows();
  });
  window.addEventListener("ps38:pagenav", (event) => {
    const page = event.detail?.page;
    if (page === "copy" || page === "settings") refreshCopyRecords({ force: true });
    if (page !== "copy" && $("copyDetailDialog").open) $("copyDetailDialog").close();
  });
  document.addEventListener("visibilitychange", () => { if (visible()) refreshCopyRecords(); });
  window.setInterval(() => {
    updateClock();
    if (visible() || ruleVisible()) {
      if (visible()) updateCountdowns();
      if (Date.now() - state.lastRefresh >= 3000) refreshCopyRecords();
    }
  }, 1000);
  amountHelp();
  updateClock();
  if (visible() || ruleVisible()) refreshCopyRecords();
}
