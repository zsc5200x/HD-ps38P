import { pauseFollow, readFollowDetail, readFollowRecords, saveFollowRule } from "./api.js";

const $ = (id) => document.getElementById(id);
const state = {
  filter: "ALL", search: "", limit: 50, offset: 0, total: 0, rows: [], accounts: [],
  rule: null, summary: {}, service: {}, logFilter: "ALL", events: [],
  dirty: false, ready: false, loading: false, mutating: false,
  request: 0, detailRequest: 0, lastRefresh: 0, bound: false,
  renderedQuery: null,
  detailId: null, detailDirty: false, detailBusy: false, detailLoading: false,
  detailFeedback: "", detailError: "", detailSourcesOpen: false,
  // 请求占用按组保留；视图代数防止关闭、重开后的迟到响应覆盖新输入。
  detailView: 0, pendingDetails: new Set(),
};
const labels = {
  SUCCEEDED: ["跟单成功", "success"],
  GROUPING: ["旧版任务", "muted"], WAIT_MAPPING: ["等待赛事匹配", "processing"],
  READY: ["等待处理", "processing"], WAIT_ACCOUNT: ["等待可用会员", "processing"],
  WAIT_MARKET: ["等待盘口", "processing"],
  PREPARING: ["核验盘口中", "processing"], RETRY_WAIT: ["等待重试", "processing"],
  PREPARED: ["已匹配盘口", "processing"], SUBMITTING: ["提交中", "processing"],
  REJECTED: ["已拒绝", "failed"], FAILED: ["失败", "failed"], PARTIAL: ["部分成功", "unknown"],
  SKIPPED: ["已跳过", "muted"], EXPIRED: ["已过期", "muted"],
};
const sideLabels = { HOME: "主队", AWAY: "客队", OVER: "大", UNDER: "小" };
const periodLabels = { FT: "全场", HT: "上半场" };
const marketLabels = { HANDICAP: "让球", TOTAL: "大小" };
const terminalStatuses = new Set(["SUCCEEDED", "REJECTED", "FAILED", "SKIPPED", "EXPIRED"]);
const stageLabels = {
  GROUPING: "旧版任务", WAIT_MAPPING: "赛事匹配", READY: "排队", WAIT_ACCOUNT: "分配会员",
  WAIT_MARKET: "等待盘口",
  PREPARING: "查盘", RETRY_WAIT: "重试等待", PREPARED: "盘口就绪", SUBMITTING: "提交",
  SUCCEEDED: "完成", REJECTED: "结束", FAILED: "结束", SKIPPED: "跳过", EXPIRED: "过期",
  PARTIAL: "部分成功",
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
  return row.status === "SUCCEEDED" && !row.receipt_id ? "FAILED" : row.status;
}

function statusBadge(row) {
  let [label, tone] = labels[effectiveStatus(row)] || ["未知", "muted"];
  if (row.error_category === "RULE_DISABLED") label = "跟单未开启";
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

function directionLabel(row) {
  const side = row.target_side;
  if (!side) return "待匹配盘口";
  const market = marketLabels[row.market_type] || display(row.market_type);
  return `${market} · ${lineLabel(row.target_line, side, row.market_type)}`;
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
  $("copySplitCount").value = rule.split_count ?? 1;
  $("copySplitDelay").value = rule.split_delay_seconds ?? 0;
  $("copyMatchLimit").value = rule.match_limit_amount ?? 0;
  $("copyMarketLimit").value = rule.market_limit_amount ?? 0;
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
  const amount = rule.amount_mode === "ratio" ? `批次金额 × ${display(rule.amount_value)}%` : `每批次 ${display(rule.amount_value)} 元`;
  $("copyRuleSummary").textContent = `${enabled ? "已启用" : "跟单未开启"} · ${direction} · ${amount} · 主页批次单号防重`;
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
    ? `主页批次金额 × ${display(rule.amount_value)}%`
    : `每批次 ${display(rule.amount_value)} 元`;
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
  // 等待任务包含已冻结待执行，以及仍在 10 秒待跟单窗口里的批次。
  const queued = Number(service.queued_tasks ?? 0) + Number(service.pending_tasks ?? 0);
  setText("copyQueuedTasks", number(queued, 0));
  setText("copyActiveTasks", number(service.active_tasks ?? 0, 0));

  const summary = state.summary || {};
  const succeeded = Number(summary.succeeded || 0);
  const processed = Number(summary.total || 0) || ["succeeded", "processing", "failed", "unknown"]
    .reduce((total, key) => total + Number(summary[key] || 0), 0);
  const rate = processed ? Math.round((succeeded / processed) * 1000) / 10 : null;
  setText("copySuccessRate", rate === null ? "—" : `${rate}%`);
  const rateNode = $("copySuccessRate");
  if (rateNode) rateNode.className = rate === null ? "copy-tone-muted" : succeeded ? "copy-tone-green" : "copy-tone-danger";
  const ring = $("copySuccessRing");
  if (ring) {
    const circumference = 2 * Math.PI * 15.5;
    ring.style.strokeDasharray = `${circumference}`;
    ring.style.strokeDashoffset = `${circumference * (1 - (rate ?? 0) / 100)}`;
    ring.style.opacity = rate === null ? "0.15" : "1";
    ring.style.stroke = rate === null ? "#7f93b5" : succeeded ? "#5ef0b0" : "#ff8f9f";
  }

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
    ["批次标识", "主页 8 位单号"],
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
    const times = own.map((row) => row.updated_at || row.created_at).filter(Boolean).sort();
    const isOnline = account.enabled !== false && account.status === "online";

    const card = node("article", null, "copy-pool-card");
    const avatar = node("span", String(name).slice(0, 1).toUpperCase(), "copy-pool-avatar");
    avatar.setAttribute("aria-hidden", "true");
    const body = node("div", null, "copy-pool-body");
    const title = node("div", null, "copy-pool-title");
    title.append(node("strong", name));
    const stateTone = account.enabled === false ? "" : isOnline ? "is-online" : "is-error";
    title.append(node("span", account.enabled === false ? "已停用" : isOnline ? "在线" : "掉线", `copy-pool-state ${stateTone}`));

    const metrics = node("div", null, "copy-pool-metrics");
    const latency = account.latency_ms;
    const balance = account.balance;
    const currency = account.balance_currency || "";
    const balanceText = balance === null || balance === undefined || balance === ""
      ? "—"
      : `${number(balance)} ${currency}`.trim();
    const rows = [
      ["延迟", latency == null || latency === "" ? "—" : `${number(latency, 0)} ms`],
      ["余额", balanceText],
      ["最近执行", times.length ? shortTime(times[times.length - 1]) : "—"],
    ];
    for (const [label, value] of rows) {
      const cell = node("div");
      cell.append(node("span", label), node("b", value));
      metrics.append(cell);
    }
    body.append(title, metrics);
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

function followStakeText(row) {
  const value = row.accepted_stake ?? row.target_stake;
  if (value === null || value === undefined || value === "") return "";
  return `${number(value)} ${row.currency || ""}`.trim();
}

function groupCard(row) {
  const status = effectiveStatus(row);
  const tone = labels[status]?.[1] || "unknown";
  const success = status === "SUCCEEDED";
  const home = row.crown_home || row.home || "待匹配主队";
  const away = row.crown_away || row.away || "待匹配客队";

  const card = node("article", null, `copy-card copy-card-${tone}`);
  card.dataset.copyGroup = row.id;
  card.dataset.copyDetail = row.id;
  card.setAttribute("role", "listitem");
  card.setAttribute("tabindex", "0");
  card.setAttribute("aria-label", `${home} 对 ${away}，查看跟单明细`);

  const head = node("header", null, "copy-card-head");
  const ident = node("div", null, "copy-card-ident");
  ident.append(node(
    "span",
    row.source_batch_no ? `订单 #${row.source_batch_no}` : `记录 #${String(row.id).slice(0, 8)}`,
    "copy-card-id",
  ));
  ident.append(node("span", `${number(row.order_count || 0, 0)} 单`, "copy-card-chip"));
  if (row.market_type) ident.append(node("span", marketLabels[row.market_type] || row.market_type, "copy-card-chip"));
  // 只有明确记录了赛前/滚球时才显示标识；历史跳过单没有 intent，不猜。
  const liveFlag = typeof row.is_live === "boolean" ? row.is_live : row.intent?.is_live;
  if (typeof liveFlag === "boolean") {
    ident.append(node(
      "span",
      liveFlag ? "滚球" : "赛前",
      `copy-card-chip copy-card-chip-${liveFlag ? "live" : "prematch"}`,
    ));
  }
  if (row.league) ident.append(node("span", row.league, "copy-card-chip copy-card-league-chip"));
  ident.append(node("time", date(row.updated_at || row.created_at), "copy-card-time"));

  const headRight = node("div", null, "copy-card-head-right");
  const badge = statusBadge(row);
  badge.classList.add("copy-card-badge");
  headRight.append(badge);
  head.append(ident, headRight);
  card.append(head);

  const match = node("div", null, "copy-card-match");
  const teams = node("div", null, "copy-card-teams");
  teams.append(node("strong", home), node("span", "VS", "copy-card-vs"), node("strong", away));
  teams.append(node("small", pickLabel(row), "copy-card-pick"));
  match.append(teams);
  card.append(match);

  const body = node("div", null, "copy-card-body");
  body.append(metricCell("投注笔数", `${number(row.order_count || 0, 0)} 单`));

  // 卡面只显示来单本身的数据：原单合计金额、原单水位。
  const amount = row.source_stake;
  const amountText = amount === null || amount === undefined || amount === ""
    ? "—"
    : `${number(amount)} ${row.currency || ""}`.trim();
  body.append(metricCell("投注金额", amountText));

  body.append(metricCell("投注方向", directionLabel(row)));
  body.append(metricCell("时段", periodLabels[row.period] || "—"));

  // 只有成功的单才有“我们跟单的水位”，失败的不显示查盘报价。
  const followOdds = success ? row.accepted_odds ?? row.odds ?? null : null;
  body.append(metricCell("比分", row.score || "—"));
  body.append(metricCell("水位", followOdds == null || followOdds === "" ? "—" : number(followOdds, 2)));

  const statusCell = node("div", null, "copy-card-cell copy-card-status");
  statusCell.append(node("span", "状态", "copy-card-label"));
  const statusTone = success ? "is-success" : tone === "failed" ? "is-danger" : "";
  // 成功直接写跟进金额；失败写原因。
  const followAmount = followStakeText(row);
  const statusText = [];
  if (success) {
    statusText.push(followAmount ? `跟单成功 · ${followAmount}` : "跟单成功");
  } else {
    statusText.push(row.message || "—");
    if (row.waiting_reason) statusText.push(`（${row.waiting_reason}）`);
  }
  statusCell.append(node("strong", statusText.join(""), `copy-card-value ${statusTone}`.trim()));
  body.append(statusCell);

  card.append(body);
  return card;
}

function logMatches(row, filter) {
  const status = effectiveStatus(row);
  if (filter === "SUCCEEDED") return status === "SUCCEEDED";
  if (filter === "FAILED") return ["REJECTED", "FAILED", "SKIPPED", "EXPIRED"].includes(status);
  if (filter === "RETRY") return Number(row.retry_count || 0) > 0 || Number(row.prepare_count || 0) > 1;
  if (filter === "ERROR") return Boolean(row.error_category) || Boolean(row.waiting_reason);
  return true;
}

const eventKindLabels = {
  CROWN: "皇冠回执",
  SUCCESS: "皇冠回执",
  STATE: "状态变化",
  MARKET_REJECTED: "盘口拒绝",
  ACCOUNT_CREDIT: "额度不足",
  ACCOUNT_SESSION: "会话异常",
  ACCOUNT_BUSY: "会员占用",
  MARKET_UNAVAILABLE: "盘口缺失",
  AMOUNT_REJECTED: "金额受限",
  PLATFORM_REJECTED: "平台拒绝",
  BATCH_SKIPPED: "跳过",
};

const eventFilters = {
  SUCCEEDED: (event) => event.to_status === "SUCCEEDED" || event.kind === "CROWN" && /成功/.test(event.message || ""),
  FAILED: (event) => ["REJECTED", "SKIPPED", "EXPIRED"].includes(String(event.to_status || "")),
  RETRY: (event) => ["RETRY_WAIT", "WAIT_MARKET", "WAIT_ACCOUNT", "WAIT_MAPPING"].includes(String(event.to_status || "")),
  ERROR: (event) => Boolean(event.error_code) || ["MARKET_REJECTED", "ACCOUNT_CREDIT", "PLATFORM_REJECTED", "AMOUNT_REJECTED"].includes(String(event.kind || "")),
};

function renderTimeline(rows, events) {
  const body = $("copyLogBody");
  if (!body) return;
  const feed = Array.isArray(events) && events.length
    ? events.filter((event) => !state.logFilter || state.logFilter === "ALL" || (eventFilters[state.logFilter] || (() => true))(event))
    : rows
        .filter((row) => logMatches(row, state.logFilter))
        .map((row) => ({
          group_id: row.id,
          at: row.updated_at || row.created_at,
          kind: row.error_category || "STATE",
          to_status: effectiveStatus(row),
          message: row.waiting_reason || row.message || "—",
          member_count: row.member_count,
          error_code: "",
        }));
  const fragment = document.createDocumentFragment();
  // 每步耗时 = 与同一订单上一步的时间差（毫秒）。
  const durations = new Map();
  const seen = new Map();
  for (const event of [...feed].reverse()) {
    const at = new Date(event.at || 0).getTime();
    const last = seen.get(event.group_id);
    durations.set(event, Number.isFinite(last) ? Math.max(0, at - last) : null);
    seen.set(event.group_id, at);
  }
  for (const event of feed) {
    const elapsed = durations.get(event);
    const stage = eventKindLabels[event.kind] || display(event.kind || "STATE");
    const statusTextValue = stageLabels[event.to_status] || display(event.to_status || "");
    const badge = node("td");
    const chip = node("span", stage, "copy-log-kind");
    chip.dataset.kind = String(event.kind || "").toLowerCase();
    badge.append(chip);
    const statusCell = node("td");
    statusCell.append(node("span", statusTextValue, "copy-log-stage"));
    const tr = node("tr");
    tr.dataset.copyGroup = event.group_id;
    tr.append(
      node("td", date(event.at), "copy-log-time"),
      node("td", `#${String(event.group_id || "").slice(0, 8)}`, "copy-log-order"),
      statusCell,
      badge,
      node("td", event.error_code || "—", "copy-number copy-mono"),
      node("td", event.message || "—", "copy-log-detail"),
      node(
        "td",
        elapsed === null ? "—" : `${elapsed} ms`,
        "copy-number copy-mono copy-log-cost",
      ),
    );
    fragment.append(tr);
  }
  body.replaceChildren(fragment);
  const empty = $("copyLogEmpty");
  if (empty) empty.hidden = feed.length > 0;
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
  renderTimeline(rows, state.events);
  renderBoard();
  $("copyEmpty").hidden = rows.length > 0;
  const searching = Boolean(state.search);
  $("copyEmptyTitle").textContent = searching ? "没有找到匹配的记录" : state.filter === "SUCCEEDED" ? "暂无成功跟单" : "暂无此类跟单记录";
  $("copyEmptyText").textContent = searching ? "试试其他球队、单号或会员，或清空搜索条件。" : state.filter === "SUCCEEDED" ? "收到皇冠明确成功回执后，订单会显示在这里。处理中的任务可在其他分类查看。" : "主页批次首次进入后会按 8 位单号显示处理状态。";
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

function updateFilters() {
  for (const button of document.querySelectorAll("[data-copy-filter]")) {
    button.setAttribute("aria-pressed", String(button.dataset.copyFilter === state.filter));
  }
  const hints = {
    SUCCEEDED: "展示已收到成功回执的真实跟单订单。",
    PROCESSING: "查看账号分配、盘口核验、重试等待和提交中的任务。",
    FAILED: "查看未完成的任务及具体原因。",
    ALL: "按主页 8 位批次单号展示一次处理记录。",
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
    state.events = Array.isArray(payload.events) ? payload.events : [];
    state.total = Number(payload.total || 0);
    state.service = payload.service || {};
    renderService(payload, forceForm);
    renderSummary(payload.summary || {});
    if (!isDetailOpen() && !$("copyRows").contains(document.activeElement)) renderRows();
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
      if (isDetailOpen()) refreshDetail({ quiet: true });
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
    split_count: Number($("copySplitCount").value || 1),
    split_delay_seconds: Number($("copySplitDelay").value || 0),
    match_limit_amount: Number($("copyMatchLimit").value || 0),
    market_limit_amount: Number($("copyMarketLimit").value || 0),
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

function detailTableBody(columns, rows) {
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
  return wrap;
}

function detailTable(title, columns, rows, openSections) {
  const section = node("details", null, "copy-detail-section");
  if (openSections && openSections.has(title)) section.open = true;
  const summary = node("summary", null, "copy-detail-summary");
  const heading = node("span", title);
  heading.setAttribute("role", "heading");
  heading.setAttribute("aria-level", "3");
  summary.append(heading);
  if (rows.length) summary.append(node("span", `${rows.length} 条`, "copy-detail-count"));
  section.append(summary);
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

function detailFeedback(message, failed = false) {
  state[failed ? "detailError" : "detailFeedback"] = message;
  const box = $(failed ? "copyDetailActionError" : "copyDetailActionFeedback");
  if (!box) return;
  box.textContent = message;
  box.hidden = !message;
}

function detailSlot() {
  return $("copyDetailContent");
}

function isDetailOpen() {
  return Boolean($("copyDetailDialog")?.open);
}

function closeDetail() {
  state.detailRequest += 1;
  state.detailView += 1;
  const dialog = $("copyDetailDialog");
  if (dialog?.open) dialog.close();
  const content = detailSlot();
  if (content) content.replaceChildren();
  state.detailId = null;
  state.detailLoading = false;
  state.detailBusy = false;
}

function setDetailBusy(busy) {
  state.detailBusy = busy;
  const content = detailSlot();
  if (!content) return;
  content.setAttribute("aria-busy", String(busy));
  for (const element of content.querySelectorAll("button, input, select, textarea")) {
    if (element.dataset.copyKeepEnabled !== undefined) continue;
    element.disabled = busy;
  }
}

function renderDetail(payload) {
  const row = payload.group || {};
  const content = detailSlot();
  if (!content) return;
  $("copyDetailTitle").textContent = row.league || "跟单明细";

  // 顶部赛事信息：联赛 + 主客队 + 玩法，一眼看清是哪场。
  const match = node("div", null, "copy-detail-match");
  match.append(node("div", row.league || "联赛待匹配", "copy-detail-league"));
  const teams = node("div", null, "copy-detail-teams");
  const home = node("div", null, "copy-detail-team");
  home.append(node("strong", row.crown_home || row.home || "待匹配主队"));
  home.append(node("span", "主队", "copy-detail-team-role"));
  const away = node("div", null, "copy-detail-team");
  away.append(node("strong", row.crown_away || row.away || "待匹配客队"));
  away.append(node("span", "客队", "copy-detail-team-role"));
  teams.append(home, node("span", "VS", "copy-detail-vs"), away);
  match.append(teams);
  const liveFlag = typeof row.is_live === "boolean" ? row.is_live : row.intent?.is_live;
  const modeLabel = typeof liveFlag === "boolean" ? (liveFlag ? "滚球 · " : "赛前 · ") : "";
  match.append(node("div", `${modeLabel}${pickLabel(row)}`, "copy-detail-pick-line"));

  // 结果摘要：状态徽章 + 四个关键数字，做成卡片里的指标条。
  const success = effectiveStatus(row) === "SUCCEEDED";
  const outcome = node("div", null, "copy-detail-outcome");
  const outcomeHead = node("div", null, "copy-detail-outcome-head");
  outcomeHead.append(statusBadge(row));
  outcomeHead.append(node("span", success ? "本次跟单结果" : "处理结果", "copy-detail-outcome-caption"));
  outcome.append(outcomeHead);

  const stats = node("div", null, "copy-detail-outcome-stats");
  const stat = (label, value, tone = "") => {
    const cell = node("div", null, "copy-detail-outcome-stat");
    cell.append(node("span", label, "copy-detail-outcome-label"));
    cell.append(node("strong", value, `copy-detail-outcome-value${tone ? ` ${tone}` : ""}`.trim()));
    return cell;
  };
  const stake = row.accepted_stake ?? row.target_stake;
  const odds = row.accepted_odds;
  stats.append(stat(success ? "金额" : "计划金额", stake == null || stake === "" ? "—" : `${number(stake)} ${row.currency || "CNY"}`, success ? "is-accent" : ""));
  stats.append(stat("水位", odds == null || odds === "" ? "—" : `@${number(odds, 3)}`, success ? "is-accent" : ""));
  stats.append(stat("回执号", row.receipt_id || "—"));
  stats.append(stat("皇冠账号", row.account_id ? accountName(row.account_id) : "—"));
  outcome.append(stats);
  match.append(outcome);

  const sources = payload.members || [];
  const status = node("div", null, "copy-detail-status");
  if (row.source_batch_no) {
    status.append(node("span", `批次 #${row.source_batch_no}`, "copy-detail-batch"));
  }
  status.append(node("span", row.message || row.waiting_reason || ""));
  const sourceButton = node(
    "button",
    `查看来源订单${sources.length ? ` · ${sources.length} 笔` : ""}`,
    "button ghost copy-detail-button copy-detail-sources-button",
  );
  sourceButton.type = "button";
  sourceButton.id = "copyDetailSources";
  sourceButton.setAttribute("aria-expanded", String(state.detailSourcesOpen));
  sourceButton.setAttribute("aria-controls", "copyDetailSourcesTable");
  sourceButton.addEventListener("click", () => {
    state.detailSourcesOpen = !state.detailSourcesOpen;
    renderDetail(payload);
  });
  status.append(sourceButton);
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
  const members = (payload.members || []).map((item) => [item.order_id, item.member_user_code || item.member_code || item.member_identity, `${number(item.stake)} ${item.currency || row.currency || ""}`, item.reported_currency || item.currency || row.currency || "—", item.selection || sideLabels[item.side], date(item.created_at || item.created_ms)]);
  // 明细会被自动刷新重绘；先记住哪些分区是展开的，避免点开就被收起。
  const openSections = new Set(
    [...content.querySelectorAll("details[open]")]
      .map((item) => item.querySelector("summary span[role='heading']")?.textContent || "")
      .filter(Boolean),
  );
  const history = (payload.history || []).map((item) => [date(item.at), `${statusText(item.from_status)} → ${statusText(item.to_status)}`, item.kind, item.message]);
  content.replaceChildren(match, status, feedback, error);
  if (state.detailSourcesOpen) {
    const sourceSection = node("section", null, "copy-detail-section copy-detail-sources");
    sourceSection.id = "copyDetailSourcesTable";
    sourceSection.append(
      node("h3", `来源订单 · ${members.length} 笔`),
      members.length
        ? detailTableBody(["原单号", "会员", "计入金额", "后台币种标签", "方向", "源单时间"], members)
        : node("p", "暂无记录", "copy-detail-muted"),
    );
    content.append(sourceSection);
  }
  const splitParts = Array.isArray(payload.parts) ? payload.parts : [];
  if (splitParts.length) {
    const done = splitParts.filter((part) => part.status === "SUCCEEDED").length;
    content.append(
      detailTable(
        `分单结果 · ${done}/${splitParts.length}`,
        ["分单", "金额", "状态", "票号", "账号", "时间"],
        splitParts.map((part) => [
          `${Number(part.part_index) + 1}/${splitParts.length}`,
          number(part.stake),
          statusText(part.status),
          part.ticket_id || "—",
          part.account_id ? accountName(part.account_id) : "—",
          date(part.updated_at),
        ]),
        openSections,
      ),
    );
  }
  content.append(
    detailTable("状态历史", ["时间", "状态变化", "分类", "说明"], history, openSections));
  setDetailBusy(state.detailBusy);
}

async function refreshDetail({ quiet = false } = {}) {
  const id = state.detailId;
  if (!id || state.detailLoading || (quiet && (state.detailBusy || state.detailDirty))) return;
  const request = ++state.detailRequest;
  state.detailLoading = true;
  try {
    const payload = await readFollowDetail(id);
    if (request !== state.detailRequest || !isDetailOpen() || (quiet && state.detailDirty)) return;
    renderDetail(payload);
  } catch (error) {
    if (request !== state.detailRequest || quiet) return;
    if ($("copyDetailActionError")) detailFeedback(error?.message || "明细读取失败，请刷新重试。", true);
    else {
      const box = node("p", error?.message || "明细读取失败，请关闭后重试。", "copy-alert copy-alert-error");
      box.setAttribute("role", "alert");
      content.replaceChildren(box);
    }
  } finally {
    if (request === state.detailRequest) state.detailLoading = false;
  }
}

async function openDetail(id) {
  if (state.detailId === id && isDetailOpen()) return;
  state.detailRequest += 1;
  state.detailView += 1;
  state.detailId = id;
  state.detailBusy = state.pendingDetails.has(id);
  state.detailFeedback = "";
  state.detailError = "";
  state.detailDirty = false;
  state.detailLoading = false;
  $("copyDetailTitle").textContent = "订单详情";
  detailSlot()?.replaceChildren(node("p", "正在读取明细…", "copy-detail-muted"));
  const dialog = $("copyDetailDialog");
  if (dialog && !dialog.open) dialog.showModal();
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
    renderTimeline(
      state.rows.filter((row) => state.filter !== "SUCCEEDED" || effectiveStatus(row) === "SUCCEEDED"),
      state.events,
    );
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
    if (button) {
      openDetail(button.dataset.copyDetail);
      return;
    }
    const card = event.target.closest("[data-copy-group]");
    if (!card) return;
    // 卡片里的按钮、输入框自己处理点击，不再切换展开状态。
    if (event.target.closest("a, button, input, select, textarea")) return;
    openDetail(card.dataset.copyGroup);
  });
  $("copyDetailClose").addEventListener("click", () => closeDetail());
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
    if (page !== "copy" && isDetailOpen()) {
      closeDetail();
      renderRows();
    }
  });
  document.addEventListener("visibilitychange", () => { if (visible()) refreshCopyRecords(); });
  window.setInterval(() => {
    updateClock();
    if (visible() || ruleVisible()) {
      if (Date.now() - state.lastRefresh >= 3000) refreshCopyRecords();
    }
  }, 1000);
  amountHelp();
  updateClock();
  if (visible() || ruleVisible()) refreshCopyRecords();
}
