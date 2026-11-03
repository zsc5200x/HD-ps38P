import { $, el } from "../dom.js";
import { fetchJson } from "../http.js";
import { state } from "../state.js";

const PAGE_SIZE = 50;
const FIXTURE_PAGE_SIZE = 50;
const BANNER_LIMIT = 5;
const BANNER_TTL_MS = 30000;

const RESULT_LABELS = {
  WIN: "胜",
  LOSE: "负",
  WIN_HALF: "赢半",
  LOSE_HALF: "输半",
  DRAW: "平",
  REFUNDED: "退还",
  OPEN: "未结算",
  "": "—",
};

function myEvents() {
  return state.myEvents;
}

function setStatus(text) {
  const node = $("myEventsStatus");
  if (node) node.textContent = text;
}

function formatNumber(value, digits = 0) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("zh-CN", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function formatMoney(value) {
  return formatNumber(value, 0);
}

function formatOdds(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "—";
}

function formatSigned(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const sign = number > 0 ? "+" : number < 0 ? "−" : "";
  return `${sign}${formatNumber(Math.abs(number), 2)}`;
}

function formatMember(row) {
  const code = row.member_user_code || "—";
  return row.member_name ? `${code} · ${row.member_name}` : code;
}

/** Render 0.25 / 0.75 as the Chinese quarter-line pair, e.g. 3.25 -> 3/3.5. */
function lineText(value) {
  const number = Math.abs(Number(value));
  if (!Number.isFinite(number)) return "";
  const whole = Math.floor(number);
  const fraction = Math.round((number - whole) * 100) / 100;
  if (fraction === 0) return String(whole);
  if (fraction === 0.5) return `${whole}.5`;
  if (fraction === 0.25) return `${whole}/${whole}.5`;
  if (fraction === 0.75) return `${whole}.5/${whole + 1}`;
  return String(number);
}

function signedLine(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  const text = lineText(number);
  if (number > 0) return `+${text}`;
  if (number < 0) return `-${text}`;
  return "0";
}

/** PS3838 stores fixtures as "home-vs-away"; display them with spaces. */
function formatTeams(value) {
  return String(value || "").replace(/-vs-/gi, " vs ");
}

function splitTeams(value) {
  const text = String(value || "");
  const parts = text.split(/\s*-\s*vs\s*-\s*/i);
  if (parts.length === 2) return [parts[0].trim(), parts[1].trim()];
  return [text.trim(), ""];
}

/** Convert a raw market into the Chinese betting notation the user reads. */
function formatPick(row) {
  const market = String(row.market || "").trim();
  const lower = market.toLowerCase();
  const [home, away] = splitTeams(row.teams);

  if (lower.startsWith("ou") || market.startsWith("大小")) {
    const prefix = ["小", "UNDER"].includes(row.selection) ? "小" : "大";
    return `${prefix}${lineText(row.handicap)}`;
  }
  if (lower.startsWith("hdp") || market.startsWith("让球")) {
    const isHome = ["主", "HOME"].includes(row.selection);
    const team = isHome ? home : away;
    return `${team || (isHome ? "主队" : "客队")}${signedLine(row.handicap)}`;
  }
  if (lower.startsWith("1x2") || market.startsWith("独赢")) {
    return row.selection || "独赢";
  }
  if (lower.startsWith("or")) {
    return row.selection || market;
  }
  const label = market.replace(/\s*0(?:\.0+)?$/, "").trim();
  return [label, row.selection].filter(Boolean).join(" ") || "—";
}

function formatPeriod(row) {
  const rawType = row.period_type ?? row.periodType;
  const type = rawType === null || rawType === undefined || rawType === ""
    ? null
    : Number(rawType);
  if (type === 0) return "全场";
  if (type === 1) return "上半场";
  if (type === 2) return "下半场";
  const name = String(row.period_name ?? row.periodName ?? "").replace(/\s+/g, "").trim();
  return ["上半场", "下半场", "全场", "中场", "加时", "点球"].includes(name) ? name : "";
}

function isCorner(row) {
  if (row.is_corner === true || row.is_corner === 1) return true;
  return /角球|\bcorners?\b/i.test(
    [row.units, row.market, row.league, row.teams].filter(Boolean).join(" "),
  );
}

function formatPickWithPeriod(row) {
  const pick = formatPick(row);
  return [isCorner(row) ? "角球" : "", formatPeriod(row), pick].filter(Boolean).join(" · ");
}

function timeZoneName(tzMinutes) {
  return Number(tzMinutes) === -240 ? "Etc/GMT+4" : "Asia/Shanghai";
}

function formatTime(row, basis = "created", tzMinutes = -240) {
  const value = basis === "settled" ? row.settled_at : (row.created_ms ?? row.created_at);
  if (value == null || value === "") return "—";
  const date = new Date(typeof value === "number" ? value : Date.parse(value));
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { hour12: false, timeZone: timeZoneName(tzMinutes) });
}

function resultClass(result) {
  const key = String(result || "open").toLowerCase().replace(/_/g, "-");
  return `result-badge result-${key}`;
}

function cell(text, className) {
  return el("td", className, text);
}

function resultCellFor(row) {
  const td = el("td", "result");
  td.append(el("span", resultClass(row.result), RESULT_LABELS[row.result] ?? "—"));
  if (row.inplay) td.append(el("span", "inplay", " · 滚球"));
  return td;
}

// ── orders table ─────────────────────────────────────────────────────────

function renderRows() {
  const body = $("myEventsBody");
  if (!body) return;
  const rows = myEvents().rows;
  const basis = myEvents().basis || "created";
  const tzLabel = Number(myEvents().tz) === -240 ? "GMT-4" : "GMT+8";
  const timeHeader = $("myEventsTimeHeader");
  if (timeHeader) {
    timeHeader.textContent = `${basis === "settled" ? "结算时间" : "下单时间"} (${tzLabel})`;
  }
  body.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.className = `my-events-row status-${String(row.status || "").toLowerCase()}`;

    const winLoss = row.win_loss == null ? null : Number(row.win_loss);
    const winLossCell = cell(
      row.status === "SETTLED" && winLoss != null ? formatSigned(winLoss) : "—",
      "winloss",
    );
    if (winLoss != null && winLoss > 0) winLossCell.classList.add("positive");
    else if (winLoss != null && winLoss < 0) winLossCell.classList.add("negative");

    tr.append(
      cell(row.league || "—", "league"),
      cell(formatTeams(row.teams) || "—", "teams"),
      cell(formatPickWithPeriod(row), "market"),
      cell(formatOdds(row.odds), "odds"),
      cell(formatMoney(row.stake), "stake"),
      winLossCell,
      cell(row.order_id || "—", "order"),
      cell(row.agent_code || "—", "agent"),
      cell(formatMember(row), "member"),
      resultCellFor(row),
      cell(formatTime(row, basis, myEvents().tz), "time"),
    );
    body.append(tr);
  }
  const empty = $("myEventsEmpty");
  if (empty) empty.classList.toggle("hidden", rows.length > 0);
}

// ── fixtures view ────────────────────────────────────────────────────────

function fixtureKey(row) {
  return `${row.teams}|${row.event_ms}`;
}

function formatKickoff(value, tzMinutes) {
  const date = new Date(Number(value));
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    hour12: false,
    timeZone: timeZoneName(tzMinutes),
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderFixtures() {
  const body = $("myEventsFixtureBody");
  if (!body) return;
  const current = myEvents();
  body.replaceChildren();
  for (const row of current.fixtures) {
    const key = fixtureKey(row);
    const tr = document.createElement("tr");
    tr.className = "my-events-fixture-row";
    if (key === current.fixtureOpenKey) tr.classList.add("open");

    const teamsCell = cell(formatTeams(row.teams) || "—", "teams");
    const lastMs = Number(row.last_ms) || 0;
    if (lastMs && Date.now() - lastMs <= 15000) {
      const fresh = el("span", "fixture-new", "新");
      fresh.title = "最近 15 秒有新单";
      teamsCell.append(fresh);
    }

    tr.append(
      cell(row.league || "—", "league"),
      teamsCell,
      cell(formatKickoff(row.event_ms, current.tz), "time"),
      cell(formatNumber(row.orders), "num"),
      cell(formatNumber(row.members), "num"),
      cell(formatNumber(row.lines), "num center"),
      cell(formatMoney(row.stake), "stake center"),
    );
    tr.addEventListener("click", () => toggleFixture(row));
    body.append(tr);
    if (key === current.fixtureOpenKey) body.append(renderFixtureDetail(row));
  }
  const empty = $("myEventsFixtureEmpty");
  if (empty) empty.classList.toggle("hidden", current.fixtures.length > 0);
  const emptyText = $("myEventsFixtureEmptyText");
  if (emptyText) emptyText.textContent = "当前范围内没有比赛";
  const more = $("myEventsFixtureMore");
  if (more) more.classList.toggle("hidden", current.fixtures.length >= current.fixtureTotal);
}

function renderFixtureDetail(row) {
  const current = myEvents();
  const key = fixtureKey(row);
  const orders = current.fixtureOrders.get(key);
  const tr = document.createElement("tr");
  tr.className = "my-events-fixture-detail";
  const td = document.createElement("td");
  td.colSpan = 7;

  const box = el("div", "fixture-detail");
  const head = el("div", "fixture-detail-head");
  const range = `${formatKickoff(row.first_ms, current.tz)} – ${formatKickoff(row.last_ms, current.tz)}`;
  if (orders === undefined) {
    head.textContent = "读取订单明细…";
  } else {
    const parts = [
      `${formatNumber(row.orders)} 单`,
      `${formatNumber(row.members)} 名会员`,
      `${formatNumber(row.lines)} 个盘口`,
      `金额 ${formatMoney(row.stake)}`,
    ];
    if (current.basis !== "settled") parts.push(`下单时间 ${range}`);
    head.textContent = parts.join(" · ");
  }
  box.append(head);

  if (orders && orders.length) {
    const groups = groupOrdersByPick(orders, current.basis);
    const grouped = document.createElement("table");
    grouped.className = "fixture-order-table fixture-line-table";
    const groupedHead = document.createElement("thead");
    const groupedHeadRow = document.createElement("tr");
    for (const label of ["盘口", "数量", "水位", "比分", "金额", "会员", "时间"]) {
      const th = el("th", "", label);
      if (label === "水位") th.title = "该盘口组的水位，多个不同水位显示为区间";
      if (label === "比分") th.title = "下注时的比分（滚球）";
      groupedHeadRow.append(th);
    }
    groupedHead.append(groupedHeadRow);
    const groupedBody = document.createElement("tbody");
    for (const group of groups) {
      const groupRow = document.createElement("tr");
      const timeText = group.first == null
        ? "—"
        : `${formatKickoff(group.first, current.tz)} – ${formatKickoff(group.last, current.tz)}`;
      groupRow.append(
        cell(group.pick, "market"),
        cell(formatNumber(group.count), "num"),
        cell(formatOddsRange(group), "odds"),
        cell(formatScoreRange(group), "score"),
        cell(formatMoney(group.stake), "stake"),
        cell(formatNumber(group.members.size), "num"),
        cell(timeText, "time"),
      );
      groupedBody.append(groupRow);
    }
    grouped.append(groupedHead, groupedBody);
    box.append(grouped);

    const details = document.createElement("details");
    details.className = "fixture-orders";
    const clusters = clusterOrdersByBurst(orders);
    const summary = document.createElement("summary");
    summary.textContent = `逐单明细（${formatNumber(orders.length)} 单 · 合并 ${formatNumber(clusters.length)} 组）`;
    details.append(summary);
    details.append(renderClusterTable(clusters, current));
    box.append(details);

    if (Number(row.orders) > orders.length) {
      box.append(el("div", "fixture-detail-note", `仅显示前 ${orders.length} 单`));
    }
  } else if (orders) {
    box.append(el("div", "fixture-detail-note", "该筛选条件下没有订单"));
  }

  td.append(box);
  tr.append(td);
  tr.addEventListener("click", (event) => event.stopPropagation());
  return tr;
}

function groupOrdersByPick(orders, basis = "created") {
  const map = new Map();
  for (const order of orders) {
    const pick = formatPickWithPeriod(order);
    let group = map.get(pick);
    if (!group) {
      group = {
        pick, count: 0, stake: 0, members: new Set(), first: null, last: null,
        oddsMin: null, oddsMax: null, scoreFirst: "", scoreLast: "", sample: order,
      };
      map.set(pick, group);
    }
    group.count += 1;
    group.stake += Number(order.stake) || 0;
    group.members.add(order.member_user_code || order.member_name || order.order_id || "");
    const odds = Number(order.odds);
    if (Number.isFinite(odds)) {
      if (group.oddsMin == null || odds < group.oddsMin) group.oddsMin = odds;
      if (group.oddsMax == null || odds > group.oddsMax) group.oddsMax = odds;
    }
    const score = String(order.score || "").trim();
    if (score) {
      if (!group.scoreFirst) group.scoreFirst = score;
      group.scoreLast = score;
    }
    const value = basis === "settled" ? Date.parse(order.settled_at) : Number(order.created_ms);
    if (Number.isFinite(value)) {
      if (group.first == null || value < group.first) group.first = value;
      if (group.last == null || value > group.last) group.last = value;
    }
  }
  return [...map.values()].sort((a, b) => b.count - a.count || a.pick.localeCompare(b.pick));
}

function formatOddsRange(group) {
  if (group.oddsMin == null) return "—";
  const min = formatOdds(group.oddsMin);
  const max = formatOdds(group.oddsMax);
  return min === max ? min : `${min}–${max}`;
}

function formatScoreRange(group) {
  if (!group.scoreFirst) return "—";
  return group.scoreFirst === group.scoreLast
    ? group.scoreFirst
    : `${group.scoreFirst} → ${group.scoreLast}`;
}

/** Merge orders on the same pick + same score that arrive within 20 seconds. */
function clusterOrdersByBurst(orders) {
  const sorted = [...orders].sort(
    (a, b) => (Number(a.created_ms) || 0) - (Number(b.created_ms) || 0),
  );
  const clusters = [];
  for (const order of sorted) {
    const pick = formatPickWithPeriod(order);
    const score = String(order.score || "").trim();
    const ms = Number(order.created_ms) || 0;
    const last = clusters[clusters.length - 1];
    if (last && last.pick === pick && last.score === score && ms - last.startMs <= 20000) {
      last.orders.push(order);
      last.endMs = ms;
    } else {
      clusters.push({ pick, score, startMs: ms, endMs: ms, orders: [order] });
    }
  }
  return clusters;
}

function formatClock(ms, tzMinutes) {
  const date = new Date(Number(ms));
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("zh-CN", { hour12: false, timeZone: timeZoneName(tzMinutes) });
}

function oddsBounds(orders) {
  let min = null;
  let max = null;
  for (const order of orders) {
    const value = Number(order.odds);
    if (!Number.isFinite(value)) continue;
    if (min == null || value < min) min = value;
    if (max == null || value > max) max = value;
  }
  return { min, max };
}

function oddsRangeForOrders(orders) {
  const { min, max } = oddsBounds(orders);
  if (min == null) return "—";
  const low = formatOdds(min);
  const high = formatOdds(max);
  return low === high ? low : `${low}–${high}`;
}

function renderClusterTable(clusters, current) {
  const table = document.createElement("table");
  table.className = "fixture-order-table fixture-cluster-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["下单时间", "盘口", "比分", "水位", "金额", "单数", "会员"]) {
    const th = el("th", "", label);
    if (label === "比分") th.title = "下注时的比分（滚球）";
    if (label === "金额") th.title = "20 秒内同盘口、同比分订单的金额合计";
    headRow.append(th);
  }
  thead.append(headRow);

  const tbody = document.createElement("tbody");
  for (const cluster of clusters) {
    const row = document.createElement("tr");
    row.className = "cluster-row";
    const first = cluster.orders[0];
    const startText = formatTime(first, "created", current.tz);
    const timeText = cluster.endMs > cluster.startMs
      ? `${startText}–${formatClock(cluster.endMs, current.tz)}`
      : startText;
    const stake = cluster.orders.reduce((sum, order) => sum + (Number(order.stake) || 0), 0);
    const members = new Set(
      cluster.orders.map((order) => order.member_user_code || order.member_name || order.order_id || ""),
    );
    row.append(
      cell(timeText, "time"),
      cell(cluster.pick, "market"),
      cell(cluster.score || "—", "score"),
      cell(oddsRangeForOrders(cluster.orders), "odds"),
      cell(formatMoney(stake), "stake"),
      cell(formatNumber(cluster.orders.length), "num"),
      cell(formatNumber(members.size), "num"),
    );

    const detailRow = document.createElement("tr");
    detailRow.className = "cluster-orders hidden";
    const detailCell = document.createElement("td");
    detailCell.colSpan = 7;
    detailCell.append(renderClusterOrders(cluster.orders, current));
    detailRow.append(detailCell);

    row.addEventListener("click", () => {
      const open = detailRow.classList.toggle("hidden");
      row.classList.toggle("open", !open);
    });
    tbody.append(row, detailRow);
  }
  table.append(thead, tbody);
  return table;
}

function renderClusterOrders(orders, current) {
  const table = document.createElement("table");
  table.className = "fixture-order-table cluster-orders-table";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["下单时间", "水位", "金额", "会员", "结果", "订单号"]) {
    headRow.append(el("th", "", label));
  }
  thead.append(headRow);
  const tbody = document.createElement("tbody");
  for (const order of orders) {
    const row = document.createElement("tr");
    row.append(
      cell(formatTime(order, current.basis, current.tz), "time"),
      cell(formatOdds(order.odds), "odds"),
      cell(formatMoney(order.stake), "stake"),
      cell(formatMember(order), "member"),
      resultCellFor(order),
      cell(order.order_id || "—", "order"),
    );
    tbody.append(row);
  }
  table.append(thead, tbody);
  return table;
}

function toggleFixture(row) {
  const current = myEvents();
  const key = fixtureKey(row);
  if (current.fixtureOpenKey === key) {
    current.fixtureOpenKey = "";
    renderFixtures();
    return;
  }
  current.fixtureOpenKey = key;
  renderFixtures();
  if (!current.fixtureOrders.has(key)) loadFixtureOrders(row);
}

async function loadFixtureOrders(row) {
  const current = myEvents();
  const key = fixtureKey(row);
  try {
    const { response, payload } = await fetchJson(
      `/api/my-events/fixture?${fixtureOrderQuery(row)}`,
      { cache: "no-store" },
      { timeoutMs: 10000, allowInvalidJson: true },
    );
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    current.fixtureOrders.set(key, Array.isArray(payload.rows) ? payload.rows : []);
  } catch (_error) {
    current.fixtureOrders.set(key, []);
  }
  if (current.fixtureOpenKey === key) renderFixtures();
}

// ── summary / agent cards ────────────────────────────────────────────────

function renderAgentOptions() {
  const select = $("myEventsAgent");
  if (!select) return;
  const agents = myEvents().agents || [];
  const signature = agents.map((agent) => agent.code).join(",");
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;
  const current = select.value;
  select.replaceChildren(new Option("全部代理", ""));
  for (const agent of agents) {
    select.append(new Option(agent.code, agent.code));
  }
  select.value = current;
}

function setStat(id, text, tone = "") {
  const node = $(id);
  if (!node) return;
  node.textContent = text;
  node.classList.remove("positive", "negative", "neutral");
  if (tone) node.classList.add(tone);
}

function renderAgents(agents) {
  const box = $("myEventsAgents");
  if (!box) return;
  box.replaceChildren();
  for (const agent of agents) {
    const card = el("article", "my-events-agent");
    card.append(el("span", "my-events-agent-code", agent.agent_code || "—"));
    card.append(el("strong", "", formatMoney(agent.stake)));
    const net = Number(agent.net);
    const foot = el(
      "span",
      "my-events-agent-foot",
      `${formatNumber(agent.orders)} 单 · 输赢 ${formatSigned(net)}`,
    );
    if (net > 0) foot.classList.add("positive");
    else if (net < 0) foot.classList.add("negative");
    card.append(foot);
    box.append(card);
  }
}

function renderSummary(payload) {
  setStat("myEventsTotalStake", formatMoney(payload.total_stake));
  setStat(
    "myEventsTotalOrders",
    `${formatNumber(payload.orders)} 单 · 已结算 ${formatNumber(payload.settled)} · 未结算 ${formatNumber(payload.open)}`,
  );
  const net = Number(payload.net);
  setStat(
    "myEventsNet",
    formatSigned(net),
    net > 0 ? "positive" : net < 0 ? "negative" : "neutral",
  );
  setStat(
    "myEventsWinLose",
    `赢 ${formatNumber(payload.win_count)} · 输 ${formatNumber(payload.lose_count)} · 赢半 ${formatNumber(payload.win_half_count)} · 输半 ${formatNumber(payload.lose_half_count)}`,
  );
  setStat("myEventsOpenCount", formatNumber(payload.open));
  setStat("myEventsOpenStake", formatMoney(payload.open_stake));
  setStat("myEventsSettledCount", formatNumber(payload.settled));
  setStat("myEventsSettledStake", formatMoney(payload.settled_stake));
  renderAgents(payload.agents || []);
}

function summaryQuery() {
  const current = myEvents();
  const params = new URLSearchParams();
  if (current.agent) params.set("agent", current.agent);
  if (current.from) params.set("from", current.from);
  if (current.to) params.set("to", current.to);
  if (current.hours) params.set("hours", String(current.hours));
  params.set("basis", current.basis || "created");
  params.set("tz", String(current.tz ?? 480));
  return params.toString();
}

// ── new order banner ─────────────────────────────────────────────────────

export function clearMyEventsBanner() {
  const banner = $("myEventsBanner");
  myEvents().banner = [];
  window.clearTimeout(myEvents().bannerTimer);
  if (banner) {
    banner.replaceChildren();
    banner.classList.add("hidden");
  }
}

export function showMyEventsBanner(events) {
  const banner = $("myEventsBanner");
  if (!banner || !Array.isArray(events) || !events.length) return;
  const current = myEvents();
  current.banner = [...events, ...current.banner].slice(0, BANNER_LIMIT);
  banner.replaceChildren();
  for (const event of current.banner) {
    const chip = el("span", "my-events-banner-chip");
    chip.append(el("b", "", "新单 "));
    chip.append(document.createTextNode(
      `${event.member_user_code || "会员"} · ${event.agent_code || "代理"} · `
      + `${event.league || "联赛"} · ${formatTeams(event.teams) || "比赛"} · `
      + `${[event.market, event.selection].filter(Boolean).join(" ") || "盘口"} · `
      + `水位 ${formatOdds(event.odds)} · 金额 ${formatMoney(event.stake)} · `
      + `#${event.order_id || "—"}`,
    ));
    banner.append(chip);
  }
  const close = el("button", "my-events-banner-close", "关闭");
  close.type = "button";
  close.addEventListener("click", clearMyEventsBanner);
  banner.append(close);
  banner.classList.remove("hidden");
  window.clearTimeout(current.bannerTimer);
  current.bannerTimer = window.setTimeout(clearMyEventsBanner, BANNER_TTL_MS);
}

// ── queries ──────────────────────────────────────────────────────────────

function scopeParams() {
  const current = myEvents();
  const params = new URLSearchParams();
  params.set("status", current.status);
  if (current.agent) params.set("agent", current.agent);
  if (current.search) params.set("search", current.search);
  if (current.from) params.set("from", current.from);
  if (current.to) params.set("to", current.to);
  if (current.hours) params.set("hours", String(current.hours));
  params.set("basis", current.basis || "created");
  params.set("tz", String(current.tz ?? 480));
  return params;
}

export function myEventsQuery() {
  const params = scopeParams();
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(myEvents().offset));
  return params.toString();
}

function fixturesQuery() {
  const params = scopeParams();
  params.set("limit", String(FIXTURE_PAGE_SIZE));
  params.set("offset", String(myEvents().fixtureOffset));
  return params.toString();
}

function copyQuery() {
  const params = scopeParams();
  params.set("limit", String(FIXTURE_PAGE_SIZE));
  params.set("offset", String(myEvents().copyOffset));
  return params.toString();
}

function fixtureOrderQuery(row) {
  const params = scopeParams();
  params.set("teams", row.teams);
  params.set("event", String(row.event_ms));
  return params.toString();
}

// ── refresh loops ────────────────────────────────────────────────────────

let listRequestId = 0;

export async function refreshMyEvents({ reset = false, silent = false } = {}) {
  const current = myEvents();
  if (current.loading && !reset) return;
  const requestId = ++listRequestId;
  if (reset) current.offset = 0;
  current.loading = true;
  if (!silent) setStatus("读取中…");
  try {
    const { response, payload } = await fetchJson(
      `/api/my-events?${myEventsQuery()}`,
      { cache: "no-store" },
      { timeoutMs: 10000, allowInvalidJson: true },
    );
    if (requestId !== listRequestId) return;
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    current.rows = reset ? rows : current.rows.concat(rows);
    current.total = Number(payload.total || 0);
    current.ready = Boolean(payload.ready);
    if (Array.isArray(payload.agents)) current.agents = payload.agents;
    renderAgentOptions();
    renderRows();
    if (reset) {
      const wrap = $("myEventsOrdersWrap");
      if (wrap) wrap.scrollTop = 0;
    }
    const count = $("myEventsCount");
    if (count) count.textContent = `${current.total} 单`;
    setStatus(current.ready ? `共 ${current.total} 单` : "采集器未就绪");
  } catch (error) {
    if (!silent) setStatus(`读取失败：${error.message || "请求失败"}`);
  } finally {
    if (requestId === listRequestId) current.loading = false;
  }
}

let fixtureRequestId = 0;

export async function refreshFixtures({ reset = false, silent = false, keepOpen = false } = {}) {
  const current = myEvents();
  if (current.fixtureLoading && !reset) return;
  const requestId = ++fixtureRequestId;
  if (reset) {
    current.fixtureOffset = 0;
    current.fixtureLastRefreshAt = Date.now();
    if (!keepOpen) {
      current.fixtureOrders = new Map();
      current.fixtureOpenKey = "";
    }
  }
  current.fixtureLoading = true;
  if (!silent) setStatus("读取中…");
  const scrollY = keepOpen ? window.scrollY : 0;
  const wrap = keepOpen ? $("myEventsFixtureWrap") : null;
  const wrapTop = wrap ? wrap.scrollTop : 0;
  try {
    const { response, payload } = await fetchJson(
      `/api/my-events/fixtures?${fixturesQuery()}`,
      { cache: "no-store" },
      { timeoutMs: 20000, allowInvalidJson: true },
    );
    if (requestId !== fixtureRequestId) return;
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    current.fixtures = reset ? rows : current.fixtures.concat(rows);
    current.fixtureTotal = Number(payload.total || 0);
    current.ready = Boolean(payload.ready);
    if (Array.isArray(payload.agents)) current.agents = payload.agents;
    renderAgentOptions();
    renderFixtures();
    if (keepOpen && current.fixtureOpenKey) {
      const openRow = current.fixtures.find((item) => fixtureKey(item) === current.fixtureOpenKey);
      if (openRow) {
        loadFixtureOrders(openRow);
      } else {
        current.fixtureOpenKey = "";
        renderFixtures();
      }
    }
    if (keepOpen) {
      if (wrap) wrap.scrollTop = wrapTop;
      window.scrollTo(0, scrollY);
    }
    const count = $("myEventsCount");
    if (count) count.textContent = `${formatNumber(current.fixtureTotal)} 场`;
    setStatus(current.ready ? `共 ${current.fixtureTotal} 场` : "采集器未就绪");
  } catch (error) {
    if (!silent) setStatus(`读取失败：${error.message || "请求失败"}`);
  } finally {
    if (requestId === fixtureRequestId) current.fixtureLoading = false;
  }
}

// ── copy (跟单) view: 20s merged groups ──────────────────────────────────

function copyScoreText(group) {
  const first = String(group.score_first || "").trim();
  const last = String(group.score_last || "").trim();
  if (!first) return "—";
  return first === last ? first : `${first} → ${last}`;
}

function copyOddsText(group) {
  const min = group.odds_min == null ? null : Number(group.odds_min);
  const max = group.odds_max == null ? null : Number(group.odds_max);
  if (min == null) return "—";
  if (max == null || Math.abs(max - min) < 1e-9) return formatOdds(min);
  return `${formatOdds(min)}–${formatOdds(max)}`;
}

function renderCopyGroups() {
  const body = $("myEventsCopyBody");
  if (!body) return;
  const current = myEvents();
  body.replaceChildren();
  for (const group of current.copy) {
    const tr = document.createElement("tr");
    tr.className = "my-events-copy-row";
    const teamsCell = cell(formatTeams(group.teams) || "—", "teams");
    if (Number(group.open_orders) > 0) teamsCell.append(el("span", "inplay", " · 未结算"));
    tr.append(
      cell(formatClock(group.start_ms, current.tz), "time"),
      cell(group.league || "—", "league"),
      teamsCell,
      cell(formatPickWithPeriod({
        market: group.market,
        selection: group.selection,
        handicap: group.handicap,
        period_type: group.period_type,
        units: group.units,
        corner_event_unit: group.corner_event_unit,
        teams: group.teams,
      }) || "—", "market"),
      cell(copyScoreText(group), "score"),
      cell(copyOddsText(group), "odds"),
      cell(formatMoney(group.stake), "stake"),
      cell(String(group.orders), "num center"),
      cell(String(group.members), "num center"),
      cell(`${Math.round((Number(group.duration_ms) || 0) / 1000)}s`, "num center"),
    );
    body.append(tr);
  }
  $("myEventsCopyEmpty")?.classList.toggle("hidden", current.copy.length > 0);
}

let copyRequestId = 0;

export async function refreshCopyGroups({ reset = false, silent = false } = {}) {
  const current = myEvents();
  if (current.copyLoading && !reset) return;
  const requestId = ++copyRequestId;
  if (reset) {
    current.copyOffset = 0;
    current.copyLastRefreshAt = Date.now();
  }
  current.copyLoading = true;
  if (!silent) setStatus("读取中…");
  try {
    const { response, payload } = await fetchJson(
      `/api/my-events/copy-groups?${copyQuery()}`,
      { cache: "no-store" },
      { timeoutMs: 20000, allowInvalidJson: true },
    );
    if (requestId !== copyRequestId) return;
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    const rows = Array.isArray(payload.rows) ? payload.rows : [];
    current.copy = reset ? rows : current.copy.concat(rows);
    current.copyTotal = Number(payload.total || 0);
    current.ready = Boolean(payload.ready);
    if (Array.isArray(payload.agents)) current.agents = payload.agents;
    renderAgentOptions();
    renderCopyGroups();
    if (reset) {
      const wrap = $("myEventsCopyWrap");
      if (wrap) wrap.scrollTop = 0;
    }
    const count = $("myEventsCount");
    if (count) count.textContent = `${formatNumber(current.copyTotal)} 组`;
    setStatus(current.ready ? `共 ${current.copyTotal} 组` : "采集器未就绪");
  } catch (error) {
    if (!silent) setStatus(`读取失败：${error.message || "请求失败"}`);
  } finally {
    if (requestId === copyRequestId) current.copyLoading = false;
  }
}

export function setMyEventsView(view) {
  const current = myEvents();
  const next = view === "copy" ? "copy" : view === "fixtures" ? "fixtures" : "orders";
  current.view = next;

  for (const item of document.querySelectorAll("[data-my-events-view]")) {
    const active = item.dataset.myEventsView === next;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  }
  if (next !== "orders") {
    for (const item of document.querySelectorAll("[data-my-events-basis]")) {
      item.classList.remove("active");
      item.setAttribute("aria-pressed", "false");
    }
  }

  const fixtures = next === "fixtures";
  const copy = next === "copy";
  $("myEventsOrdersWrap")?.classList.toggle("hidden", fixtures || copy);
  $("myEventsFixtureWrap")?.classList.toggle("hidden", !fixtures);
  $("myEventsCopyWrap")?.classList.toggle("hidden", !copy);
}

let summaryRequestId = 0;

export async function refreshMyEventsSummary() {
  const requestId = ++summaryRequestId;
  try {
    const { response, payload } = await fetchJson(
      `/api/my-events/summary?${summaryQuery()}`,
      { cache: "no-store" },
      { timeoutMs: 10000, allowInvalidJson: true },
    );
    if (requestId !== summaryRequestId) return;
    if (!response.ok || payload.ok === false) return;
    if (payload.ready === false) return;
    renderSummary(payload);
  } catch (_error) {
    // Keep the previous cards when the local summary query is unavailable.
  }
}

export async function refreshMyEventsAll(options = {}) {
  if (myEvents().view === "fixtures") {
    await Promise.all([refreshFixtures(options), refreshMyEventsSummary()]);
    return;
  }
  if (myEvents().view === "copy") {
    await Promise.all([refreshCopyGroups(options), refreshMyEventsSummary()]);
    return;
  }
  await Promise.all([refreshMyEvents(options), refreshMyEventsSummary()]);
}

export async function loadMoreMyEvents() {
  const current = myEvents();
  if (current.view === "fixtures") {
    if (current.fixtureLoading) return;
    current.fixtureOffset = current.fixtures.length;
    await refreshFixtures();
    return;
  }
  if (current.view === "copy") {
    if (current.copyLoading) return;
    current.copyOffset = current.copy.length;
    await refreshCopyGroups();
    return;
  }
  if (current.loading) return;
  current.offset = current.rows.length;
  await refreshMyEvents();
}

export async function pollMyEventsEvents() {
  const current = myEvents();
  const url = current.lastCursor == null
    ? "/api/my-events/events"
    : `/api/my-events/events?cursor=${current.lastCursor}`;
  try {
    const { response, payload } = await fetchJson(
      url,
      { cache: "no-store" },
      { timeoutMs: 8000, allowInvalidJson: true },
    );
    if (!response.ok || payload.ok === false) return;
    if (payload.ready === false) return;
    current.lastCursor = payload.cursor ?? current.lastCursor;
    if (Array.isArray(payload.events) && payload.events.length) {
      showMyEventsBanner(payload.events);
      if (current.view === "fixtures") {
        await refreshMyEventsSummary();
        const now = Date.now();
        if (!current.fixtureLoading && now - (current.fixtureLastRefreshAt || 0) >= 3000) {
          await refreshFixtures({ reset: true, silent: true, keepOpen: true });
        }
      } else if (current.view === "copy") {
        await refreshMyEventsSummary();
        const now = Date.now();
        if (!current.copyLoading && now - (current.copyLastRefreshAt || 0) >= 3000) {
          await refreshCopyGroups({ reset: true, silent: true });
        }
      } else {
        await refreshMyEvents({ reset: true, silent: true });
        await refreshMyEventsSummary();
      }
    }
  } catch (_error) {
    // A transient local read error must never stop the event cursor loop.
  }
}
