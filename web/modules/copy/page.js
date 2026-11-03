import { pauseFollow, readFollowDetail, readFollowRecords, requestFollowReconcile, resolveFollowResult, saveFollowRule } from "./api.js";

const $ = (id) => document.getElementById(id);
const state = {
  filter: "SUCCEEDED", search: "", limit: 50, offset: 0, total: 0, rows: [], accounts: [],
  rule: null, dirty: false, ready: false, loading: false, mutating: false,
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
  READY: ["等待处理", "processing"], WAIT_MARKET: ["等待盘口", "processing"],
  PREPARING: ["核验盘口中", "processing"], RETRY_WAIT: ["等待重试", "processing"],
  PREPARED: ["已匹配盘口", "processing"], SUBMITTING: ["提交中", "processing"],
  REJECTED: ["已拒绝", "failed"], FAILED: ["失败", "failed"],
  SKIPPED: ["已跳过", "muted"], EXPIRED: ["已过期", "muted"],
  LATE_ORDER: ["迟到待核对", "unknown"],
};
const sideLabels = { HOME: "主队", AWAY: "客队", OVER: "大", UNDER: "小" };
const periodLabels = { FT: "全场", HT: "上半场" };
const marketLabels = { HANDICAP: "让球", TOTAL: "大小" };

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

function lineLabel(value, side, kind) {
  const raw = display(value);
  const signed = kind === "HANDICAP" && Number(value) > 0 ? `+${raw}` : raw;
  return `${sideLabels[side] || display(side)} ${signed}`;
}

function pickLabel(row, source = false) {
  const side = source ? row.source_side : row.target_side;
  const line = source ? row.source_line : row.target_line;
  return `${periodLabels[row.period] || display(row.period)} · ${marketLabels[row.market_type] || display(row.market_type)} · ${lineLabel(line, side, row.market_type)}`;
}

function visible() {
  return !$('copyPage')?.hidden && !document.hidden;
}

function setDirty(value) {
  state.dirty = value;
  $("copyRuleDirty").hidden = !value;
  if (value) $("copyRuleFeedback").textContent = "";
}

function amountHelp() {
  const ratio = $("copyAmountMode").value === "ratio";
  $("copyAmountLabel").textContent = ratio ? "跟单比例（%）" : "每组金额（元）";
  $("copyAmountValue").step = ratio ? "0.01" : "1";
  $("copyAmountValue").min = ratio ? "0.01" : "1";
  $("copyAmountHelp").textContent = ratio
    ? "例如 10 表示原单合计的 10%，向下取整；不足 1 不处理。"
    : "每组使用一次固定金额，按人民币整元计算。";
}

function populateAccounts(accounts, selected) {
  const options = [node("option", "请选择皇冠账号")];
  options[0].value = "";
  for (const account of accounts) {
    if (account.platform && account.platform !== "crown_member") continue;
    const online = account.enabled !== false && account.status === "online";
    const option = node("option", `${account.label || account.name || account.username || account.id}${online ? " · 在线" : " · 不可用"}`);
    option.value = account.id;
    option.disabled = !online;
    options.push(option);
  }
  if (selected && !options.some((option) => option.value === selected)) {
    const option = node("option", `${selected} · 账号不可用`);
    option.value = selected;
    option.disabled = true;
    options.push(option);
  }
  $("copyAccount").replaceChildren(...options);
  $("copyAccount").value = selected || "";
}

function populateRule(rule) {
  $("copyDirection").value = rule.direction || "forward";
  $("copyAmountMode").value = rule.amount_mode || "fixed";
  $("copyAmountValue").value = rule.amount_value ?? "";
  $("copyMaxAge").value = rule.max_age_seconds ?? 120;
  $("copyMinOdds").value = rule.min_odds ?? "";
  $("copyMaxOdds").value = rule.max_odds ?? "";
  $("copyMembers").value = Array.isArray(rule.member_codes) ? rule.member_codes.join(", ") : "";
  populateAccounts(state.accounts, rule.account_id);
  amountHelp();
}

function renderService(payload, forceForm) {
  const service = payload.service || {};
  const rule = payload.rule || {};
  const enabled = service.enabled === true;
  const badge = $("copyServiceBadge");
  badge.textContent = enabled ? "跟单已启用" : "跟单已暂停";
  badge.className = `copy-badge ${enabled ? "copy-badge-success" : "copy-badge-muted"}`;
  $("copyServiceMessage").textContent = service.message || (enabled ? "接收新来单，按规则合组处理。" : "保存规则并启用后开始接收新来单。已生成记录会继续保留。");
  let queue = $("copyQueueStatus");
  if (!queue) {
    queue = node("span", null, "copy-queue-status copy-mono");
    queue.id = "copyQueueStatus";
    $("copyServiceMessage").after(queue);
  }
  queue.textContent = `执行中 ${number(service.active_tasks ?? 0, 0)} · 排队 ${number(service.queued_tasks ?? 0, 0)}`;
  state.rule = rule;
  state.accounts = Array.isArray(payload.accounts) ? payload.accounts : state.accounts;
  const direction = rule.direction === "reverse" ? "反向" : "正向";
  const amount = rule.amount_mode === "ratio" ? `原单合计 × ${display(rule.amount_value)}%` : `每组 ${display(rule.amount_value)} 元`;
  $("copyRuleSummary").textContent = `${direction} · ${amount} · 20 秒合组`;
  if (!state.dirty || forceForm) populateRule(rule);
  $("copyPause").disabled = !enabled || state.mutating;
  $("copySaveRule").disabled = state.mutating;
  $("copyEnableRule").disabled = state.mutating;
}

function renderSummary(summary) {
  for (const [id, key] of [["copySuccessCount", "succeeded"], ["copyProcessingCount", "processing"], ["copyFailedCount", "failed"], ["copyUnknownCount", "unknown"]]) {
    $(id).textContent = number(summary[key] ?? 0, 0);
  }
  for (const count of document.querySelectorAll("[data-copy-count]")) {
    count.textContent = number(summary[count.dataset.copyCount] ?? 0, 0);
  }
}

function stackedCell(primary, secondary, className = "") {
  const cell = node("td", null, className);
  cell.append(node("span", primary, "copy-cell-main"));
  if (secondary) cell.append(node("small", secondary, "copy-cell-sub"));
  return cell;
}

function renderRows() {
  const rows = state.rows.filter((row) => state.filter !== "SUCCEEDED" || effectiveStatus(row) === "SUCCEEDED");
  const fragment = document.createDocumentFragment();
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.dataset.copyGroup = row.id;
    const status = node("td");
    status.append(statusBadge(row), node("small", row.receipt_id || "尚无成功回执", "copy-cell-sub copy-mono"));
    const home = row.crown_home || row.home || "待匹配主队";
    const away = row.crown_away || row.away || "待匹配客队";
    const match = stackedCell(`${home} vs ${away}`, row.league, "copy-match-cell");
    const pick = stackedCell(pickLabel(row), `${row.direction === "reverse" ? "反向" : "正向"} · 原单 ${lineLabel(row.source_line, row.source_side, row.market_type)}`);
    const success = effectiveStatus(row) === "SUCCEEDED";
    const hasAcceptedStake = success && row.accepted_stake !== null && row.accepted_stake !== undefined;
    const amount = hasAcceptedStake ? row.accepted_stake : row.target_stake;
    const money = stackedCell(`${number(amount)} ${row.currency || ""}`, hasAcceptedStake ? row.receipt_source === "manual" ? "人工核对金额" : "回执金额" : success ? row.receipt_source === "manual" ? "计划金额 · 未登记实际金额" : "提交金额 · 回执未回显" : "计划金额", "copy-number copy-mono");
    const account = stackedCell(accountName(row.account_id), `${number(row.order_count || 0, 0)} 笔原单 · ${number(row.member_count || 0, 0)} 位会员`);
    const time = stackedCell(date(row.updated_at || row.created_at), row.status === "GROUPING" ? "20 秒窗口内合组" : row.message, "copy-time-cell");
    if (row.status === "GROUPING" && row.window_ends_at) {
      time.querySelector("small").dataset.copyCountdown = row.window_ends_at;
    }
    if (row.status !== "GROUPING" && row.waiting_reason) time.querySelector("small")?.remove();
    if (row.status !== "GROUPING" && row.waiting_reason) time.append(node("small", row.waiting_reason, "copy-cell-sub"));
    if (row.next_action_at) time.append(node("small", `下次动作 ${date(row.next_action_at)}`, "copy-cell-sub"));
    time.append(node("small", `查盘 ${number(row.prepare_count ?? 0, 0)} · 重试 ${number(row.retry_count ?? 0, 0)} · 核对 ${number(row.reconcile_count ?? 0, 0)}`, "copy-cell-sub copy-mono"));
    if (row.error_category) time.append(node("small", `错误分类 ${row.error_category}`, "copy-cell-sub"));
    const actions = node("td");
    const detail = node("button", "明细", "button ghost copy-detail-button");
    detail.type = "button";
    detail.dataset.copyDetail = row.id;
    detail.setAttribute("aria-label", `查看 ${home} 对 ${away} 的跟单明细`);
    actions.append(detail);
    const oddsLabel = success && row.accepted_odds != null ? "回执水位 · 香港盘" : "提交水位 · 香港盘";
    tr.append(status, match, pick, stackedCell(number(row.accepted_odds ?? row.odds, 3), oddsLabel, "copy-number copy-mono"), money, account, time, actions);
    fragment.append(tr);
  }
  $("copyRows").replaceChildren(fragment);
  updateCountdowns();
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
    PROCESSING: "查看合组、排队、盘口核验、重试等待和提交中的任务。",
    FAILED: "查看未完成的任务及具体原因。",
    UNKNOWN: "这些任务尚未取得确定结果，不会自动重复提交。",
    ALL: "按原单窗口时间展示全部跟单组及处理进度。",
  };
  $("copyRecordsHint").textContent = hints[state.filter];
}

export async function refreshCopyRecords({ force = false, forceForm = false } = {}) {
  if (state.mutating || (!force && (state.loading || !visible()))) return;
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
  $("copyRows").closest("table").setAttribute("aria-busy", "true");
  try {
    const payload = await readFollowRecords(state);
    if (request !== state.request) return;
    state.ready = true;
    state.renderedQuery = queryKey;
    state.rows = Array.isArray(payload.rows) ? payload.rows : [];
    state.total = Number(payload.total || 0);
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
    if (!state.ready) {
      $("copyRecordCount").textContent = "记录读取失败";
      $("copyServiceBadge").textContent = "暂不可用";
      $("copyServiceMessage").textContent = "跟单服务暂不可用，请稍后刷新。";
    }
  } finally {
    if (request === state.request) {
      state.loading = false;
      state.lastRefresh = Date.now();
      $("copyRefresh").disabled = false;
      $("copyMore").disabled = false;
      $("copyPrevious").disabled = false;
      $("copyRows").closest("table").setAttribute("aria-busy", "false");
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
    $("copyRows").closest("table").setAttribute("aria-busy", "false");
  }
  for (const id of ["copySaveRule", "copyEnableRule", "copyPause"]) $(id).disabled = busy;
  for (const field of $("copyRuleForm").querySelectorAll("input, select")) field.disabled = busy;
  $("copyRuleForm").setAttribute("aria-busy", String(busy));
}

function formValue(enabled) {
  return {
    enabled, direction: $("copyDirection").value, amount_mode: $("copyAmountMode").value,
    amount_value: $("copyAmountValue").value, account_id: $("copyAccount").value,
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

export function bindCopyEvents() {
  if (state.bound || !$("copyPage")) return;
  state.bound = true;
  $("copyRefresh").addEventListener("click", () => refreshCopyRecords({ force: true }));
  $("copyRulesToggle").addEventListener("click", () => {
    $("copyRuleSettings").open = !$("copyRuleSettings").open;
    if ($("copyRuleSettings").open) $("copyRuleSettings").scrollIntoView({ block: "nearest" });
  });
  $("copyRuleSettings").addEventListener("toggle", () => $("copyRulesToggle").setAttribute("aria-expanded", String($("copyRuleSettings").open)));
  $("copyRuleForm").addEventListener("input", () => setDirty(true));
  $("copyRuleForm").addEventListener("change", () => setDirty(true));
  $("copyAmountMode").addEventListener("change", amountHelp);
  $("copyRuleForm").addEventListener("submit", saveRule);
  $("copyPause").addEventListener("click", pauseRule);
  document.querySelectorAll("[data-copy-filter]").forEach((button) => button.addEventListener("click", () => selectFilter(button.dataset.copyFilter)));
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
    if (event.detail?.page === "copy") refreshCopyRecords({ force: true });
    else if ($("copyDetailDialog").open) $("copyDetailDialog").close();
  });
  document.addEventListener("visibilitychange", () => { if (visible()) refreshCopyRecords(); });
  window.setInterval(() => {
    if (visible()) {
      updateCountdowns();
      if (Date.now() - state.lastRefresh >= 3000) refreshCopyRecords();
    }
  }, 1000);
  amountHelp();
  if (visible()) refreshCopyRecords();
}
