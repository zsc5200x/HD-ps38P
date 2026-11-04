/** 赛事合并工作台：联赛绑定、赛事候选、人工确认与自动合并。 */
import { $, el, showToast } from "../dom.js";
import { fetchJson } from "../http.js";

const MAX_CANDIDATES = 3;
const MERGE_COLD_POLL_INTERVAL_MS = 500;
const MERGE_REFRESH_POLL_INTERVAL_MS = 1500;
const MERGE_RETRY_INTERVAL_MS = 2500;

let data = null;
let loading = false;
let busy = false;
let mergePageActive = false;
let mergePollTimer = null;
let mergeBuildPending = false;
let confirmingFixtureId = null;
let dialogMode = null;
let matchView = "pending";
let libraryQuery = "";
let initialLeagueExpanded = false;
const expandedLeagueGroups = new Set();
const expandedMergedLeagueGroups = new Set();
const expandedFixtures = new Set();

function groupBy(rows, key) {
  const map = new Map();
  for (const row of rows || []) {
    const value = row?.[key];
    if (!map.has(value)) map.set(value, []);
    map.get(value).push(row);
  }
  return map;
}

function shortTime(value) {
  const text = String(value || "");
  return text.length >= 16 ? text.slice(5, 16) : text;
}

function stamp(value) {
  const text = String(value || "").replace("T", " ");
  return text.length >= 16 ? text.slice(5, 16) : text;
}

function fullStamp(value) {
  const text = String(value || "").replace("T", " ");
  return text.length >= 19 ? text.slice(0, 19) : text;
}

function money(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = text;
  return element;
}

function button(label, className, dataset = {}) {
  const element = node("button", className, label);
  element.type = "button";
  for (const [key, value] of Object.entries(dataset)) element.dataset[key] = value;
  return element;
}

function option(value, label, disabled = false) {
  const element = node("option", "", label);
  element.value = value;
  element.disabled = Boolean(disabled);
  return element;
}

async function postAction(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function setSourceState(element, state) {
  if (element) element.dataset.state = state;
}

function updateControls() {
  const auto = $("mergeAuto");
  const refresh = $("mergeRefresh");
  if (auto) {
    auto.disabled = Boolean(loading || busy || mergeBuildPending || !data || data.crownError || !data.sources?.ps38?.ok || !data.sources?.crown?.count);
  }
  if (refresh) refresh.disabled = Boolean(loading || busy);
  for (const id of ["mergeFetchCrown", "mergeFetchPs38"]) {
    const button = $(id);
    if (button) button.disabled = Boolean(loading || busy);
  }
  for (const confirm of document.querySelectorAll("[data-quick-confirm]")) {
    confirm.disabled = Boolean(loading || busy);
    confirm.textContent = confirmingFixtureId === confirm.dataset.crownId ? "确认中…" : "确认比赛";
  }
}

function showNotice(message) {
  const notice = $("mergeNotice");
  if (!notice) return;
  if (message) {
    notice.textContent = message;
    notice.classList.remove("hidden");
  } else {
    notice.classList.add("hidden");
  }
}

async function loadMerge({ silent = false } = {}) {
  if (loading) return null;
  loading = true;
  updateControls();
  if (!data && !silent) renderLoading();
  try {
    const { response, payload } = await fetchJson(
      "/api/merge",
      { cache: "no-store" },
      { timeoutMs: 30000, allowInvalidJson: true },
    );
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    if (payload.ready === false) {
      mergeBuildPending = payload.refreshing === true;
      const hasSavedMappings = (payload.mappings || []).length > 0
        || (payload.leagueMappings || []).length > 0;
      if (hasSavedMappings) {
        data = payload;
        if (payload.sources?.crown?.ok === false) matchView = "merged";
        renderAll();
      } else if (!data) {
        renderLoading(payload.error || "正在准备赛事匹配数据…");
      }
      if (payload.error) showNotice(payload.error);
      else renderStatus();
      scheduleMergePoll(payload);
      return payload;
    }
    data = payload;
    mergeBuildPending = payload.refreshing === true;
    if ((payload.mappings || []).length && payload.sources?.crown?.ok === false) {
      matchView = "merged";
    }
    renderAll();
    scheduleMergePoll(payload);
    return payload;
  } catch (error) {
    const message = String(error?.message || error);
    showNotice(`合并数据读取失败：${message}`);
    if (!silent) showToast(`合并数据读取失败：${message}`);
    if (mergePageActive) scheduleMergePoll({ ready: false, refreshing: false });
    return null;
  } finally {
    loading = false;
    updateControls();
  }
}

function scheduleMergePoll(payload) {
  if (mergePollTimer !== null) {
    window.clearTimeout(mergePollTimer);
    mergePollTimer = null;
  }
  const pending = payload?.ready === false || payload?.refreshing === true;
  if (!mergePageActive || !pending) return;
  const delay = payload?.ready === true
    ? MERGE_REFRESH_POLL_INTERVAL_MS
    : payload?.refreshing === false
      ? MERGE_RETRY_INTERVAL_MS
      : MERGE_COLD_POLL_INTERVAL_MS;
  mergePollTimer = window.setTimeout(() => {
    mergePollTimer = null;
    loadMerge({ silent: true });
  }, delay);
}

function setMergePageActive(active) {
  mergePageActive = Boolean(active);
  if (!mergePageActive && mergePollTimer !== null) {
    window.clearTimeout(mergePollTimer);
    mergePollTimer = null;
  } else if (mergePageActive) {
    loadMerge({ silent: true });
  }
}

function renderLoading(message = "正在读取皇冠与平博赛事…") {
  const fixtures = $("mergeFixtures");
  if (fixtures) fixtures.replaceChildren(node("div", "merge-empty", message));
  const leagues = $("mergeLeagues");
  if (leagues) leagues.replaceChildren(node("div", "merge-empty", message));
}

function renderStatus() {
  const ps38 = data?.sources?.ps38;
  const crown = data?.sources?.crown;
  const counts = data?.counts || {};
  setSourceState($("mergePs38Source"), ps38 ? (ps38.ok ? "ok" : "off") : "wait");
  setSourceState($("mergeCrownSource"), crown ? (crown.ok ? "ok" : "off") : "wait");
  if ($("mergePs38Count")) $("mergePs38Count").textContent = ps38 ? String(ps38.count) : "—";
  if ($("mergeCrownCount")) $("mergeCrownCount").textContent = crown ? String(crown.count) : "—";
  if ($("mergeMappedCount")) {
    $("mergeMappedCount").textContent = String(counts.fixtureMappings ?? "—");
  }
  const updated = $("mergeUpdated");
  if (updated) {
    updated.textContent = mergeBuildPending
      ? "后台正在准备赛事数据…"
      : data
      ? `更新 ${String(data.generatedAt || "").slice(11, 19)}`
      : "读取中…";
  }
  for (const [id, source, events] of [
    ["mergePs38Source", ps38, data?.ps38Events],
    ["mergeCrownSource", crown, data?.crownEvents],
  ]) {
    const target = $(id);
    if (!target) continue;
    const live = (events || []).filter((event) => event.isLive).length;
    target.title = source?.error || source?.warning || (source
      ? `${source.count} 场 / ${source.leagueCount} 个联赛 · 滚球 ${live} 场 + 今日未开赛 ${source.count - live} 场（按各平台系统口径）`
      : "尚未读取");
  }

  if (data?.crownError) {
    showNotice(`${data.crownError} 皇冠数据恢复后点「刷新」重试。`);
  } else if (data && !mergeBuildPending && crown && crown.count === 0) {
    showNotice("皇冠服务已连接，但当前没有赛事。");
  } else if (ps38?.warning) {
    showNotice(`${ps38.warning} 待匹配列表只使用当前赛事，请恢复采集后重新拉取。`);
  } else {
    showNotice("");
  }
}

function renderLeagues() {
  const host = $("mergeLeagues");
  if (!host || !data) return;
  host.replaceChildren();
  const mappings = data.leagueMappings || [];
  const mappedCrown = new Map(mappings.map((item) => [item.crownId, item]));
  const mappedPs = new Set(mappings.map((item) => item.ps38Id));
  const psLeagues = [...(data.ps38Leagues || [])].sort((left, right) =>
    String(left.name).localeCompare(String(right.name), "zh-CN"),
  );
  const psById = new Map(psLeagues.map((item) => [item.id, item]));
  const suggestions = groupBy(data.leagueSuggestions || [], "crownLeagueId");
  const allCrownLeagues = [...(data.crownLeagues || [])].sort((left, right) =>
    String(left.name).localeCompare(String(right.name), "zh-CN"),
  );
  const unboundLeagues = allCrownLeagues.filter((league) => !mappedCrown.has(league.id));
  if ($("mergeLeagueMeta")) {
    $("mergeLeagueMeta").textContent = `${unboundLeagues.length} 个待绑定`;
  }

  if (!allCrownLeagues.length) {
    host.append(node("div", "merge-empty", data.crownError
      ? "皇冠当前联赛暂不可用，请点击「拉取皇冠赛事」重试。"
      : "当前暂无皇冠联赛，请点击「拉取皇冠赛事」。"));
    return;
  }
  if (!unboundLeagues.length) {
    host.append(node("div", "merge-empty", "联赛已全部绑定"));
    return;
  }
  for (const league of unboundLeagues) {
    const row = node("div", "merge-league-row");
    row.dataset.crownLeagueId = league.id;
    const identity = node("div", "merge-league-identity");
    identity.append(
      node("strong", "", league.name),
      node("span", "", `${league.eventCount} 场`),
    );
    row.append(identity);

    const target = node("div", "merge-league-target");
    const select = node("select", "merge-select");
    select.dataset.crownSelect = league.id;
    select.setAttribute("aria-label", `为皇冠联赛 ${league.name} 选择平博联赛`);
    select.append(option("", "选择对应平博联赛"));

    const ranked = (suggestions.get(league.id) || [])
      .filter((item) => psById.has(item.ps38LeagueId))
      .slice(0, MAX_CANDIDATES);
    const rankedIds = new Set(ranked.map((item) => item.ps38LeagueId));
    if (ranked.length) {
      const group = node("optgroup");
      group.label = "推荐";
      for (const item of ranked) {
        const taken = mappedPs.has(item.ps38LeagueId);
        group.append(
          option(
            item.ps38LeagueId,
            `${item.ps38LeagueName} · ${Number(item.score).toFixed(1)}${taken ? "（已绑定）" : ""}`,
            taken,
          ),
        );
      }
      select.append(group);
    }
    const group = node("optgroup");
    group.label = "全部平博联赛";
    for (const psLeague of psLeagues) {
      if (rankedIds.has(psLeague.id)) continue;
      const taken = mappedPs.has(psLeague.id);
      group.append(
        option(psLeague.id, `${psLeague.name}${taken ? "（已绑定）" : ""}`, taken),
      );
    }
    if (group.children.length) select.append(group);

    const firstAvailable = ranked.find((item) => !mappedPs.has(item.ps38LeagueId))
      || psLeagues.find((item) => !mappedPs.has(item.id));
    if (!firstAvailable) {
      target.append(
        node(
          "span",
          "merge-league-wait",
          ranked.length
            ? `建议 ${ranked[0].ps38LeagueName}（已绑定）`
            : "暂无可用平博联赛，请拉取平博赛事后绑定",
        ),
      );
      row.append(target);
      host.append(row);
      continue;
    }
    const recommended = ranked.find((item) => !mappedPs.has(item.ps38LeagueId) && item.baseScore >= 65);
    select.value = recommended?.ps38LeagueId || "";
    const bind = button("绑定", "merge-mini-btn", { bindCrown: league.id });
    bind.title = `绑定 ${league.name}`;
    bind.setAttribute("aria-label", `绑定 ${league.name}`);
    target.append(select, bind);
    row.append(target);
    host.append(row);
  }
}

function psEventsByLeague() {
  const map = new Map();
  for (const event of data?.ps38Events || []) {
    if (!map.has(event.leagueId)) map.set(event.leagueId, []);
    map.get(event.leagueId).push(event);
  }
  for (const list of map.values()) {
    list.sort((left, right) => String(left.kickoffUtc).localeCompare(String(right.kickoffUtc)));
  }
  return map;
}

function matchingCrownEvents() {
  const bound = new Set((data?.leagueMappings || []).map((item) => item.crownId));
  return (data?.crownEvents || []).filter((event) => bound.has(event.leagueId));
}

function updateViewTabs() {
  const mappings = data?.mappings || [];
  const mappedCrown = new Set(mappings.map((item) => item.crownId));
  const pendingCount = data
    ? matchingCrownEvents().filter((event) => !mappedCrown.has(event.id)).length
    : null;
  const pending = document.querySelector('[data-merge-view="pending"]');
  const merged = document.querySelector('[data-merge-view="merged"]');
  if (pending) pending.textContent = pendingCount === null ? "待合并" : `待合并 ${pendingCount}`;
  if (merged) merged.textContent = `已合并 ${mappings.length}`;
  for (const tab of document.querySelectorAll("[data-merge-view]")) {
    const active = tab.dataset.mergeView === matchView;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-pressed", String(active));
  }
}

function renderFixtures() {
  const host = $("mergeFixtures");
  if (!host) return;
  updateViewTabs();
  if (!data) return;
  host.replaceChildren();
  const mappings = data.mappings || [];
  if (matchView === "merged") {
    if ($("mergeFixtureMeta")) {
      $("mergeFixtureMeta").textContent = `${mappings.length} 场已合并`;
    }
    renderMergedFixtures(host, mappings);
    return;
  }
  const mappedPs = new Set(mappings.map((item) => item.ps38Id));
  const mappedCrown = new Set(mappings.map((item) => item.crownId));
  const boundByCrownLeague = new Map((data.leagueMappings || []).map((item) => [item.crownId, item]));
  const candidatesByCrown = groupBy(data.suggestions || [], "crownId");
  const psById = new Map((data.ps38Events || []).map((item) => [item.id, item]));
  const psByLeague = psEventsByLeague();
  const events = matchingCrownEvents().sort((left, right) =>
    String(left.kickoffUtc).localeCompare(String(right.kickoffUtc)),
  );

  const pending = events.filter((event) => !mappedCrown.has(event.id));
  if ($("mergeFixtureMeta")) {
    $("mergeFixtureMeta").textContent = `皇冠 ${pending.length} 场待合并 · ${mappings.length} 已合并`;
  }
  if (!events.length) {
    host.append(node("div", "merge-empty", data.crownError
      ? "皇冠当前赛事暂不可用，请点击「拉取皇冠赛事」重试；已保存的对照可在「已合并」查看。"
      : (data.crownEvents || []).length
        ? "请先在左侧绑定皇冠联赛，绑定后该联赛的比赛会显示在这里。"
        : "当前暂无皇冠比赛，请点击「拉取皇冠赛事」。"));
    return;
  }
  if (!pending.length) {
    host.append(node("div", "merge-empty", "窗口内的赛事都已合并。"));
    return;
  }

  const grouped = new Map();
  for (const event of pending) {
    const mapping = boundByCrownLeague.get(event.leagueId);
    const key = event.leagueId || event.league;
    const name = event.league || mapping?.crownSnapshot?.name || "未命名皇冠联赛";
    if (!grouped.has(key)) grouped.set(key, { id: key, name, mapping, events: [] });
    grouped.get(key).events.push(event);
  }
  const orderedGroups = [...grouped.values()].sort((left, right) =>
    String(left.name).localeCompare(String(right.name), "zh-CN"),
  );
  if (!initialLeagueExpanded && orderedGroups.length) {
    const first = orderedGroups.find((group) => group.mapping) || orderedGroups[0];
    expandedLeagueGroups.add(first.id);
    initialLeagueExpanded = true;
  }
  for (const group of orderedGroups) {
    const wrapper = node("div", "merge-league-group");
    if (expandedLeagueGroups.has(group.id)) wrapper.classList.add("open");
    const groupHead = document.createElement("button");
    groupHead.type = "button";
    groupHead.className = "merge-league-group-head";
    groupHead.dataset.leagueToggle = group.id;
    groupHead.setAttribute("aria-expanded", wrapper.classList.contains("open") ? "true" : "false");
    const mapping = group.mapping;
    const crownName = group.name;
    const rawCrownId = group.events[0]?.providerLeagueId || mapping?.crownSnapshot?.providerId || group.id;
    const crownId = rawCrownId && rawCrownId !== crownName ? rawCrownId : "—";
    const psName = mapping ? (mapping.ps38Snapshot?.name || mapping.ps38Id) : "待绑定平博联赛";
    const psId = mapping
      ? (mapping.ps38Snapshot?.providerId || "—")
      : "—";
    const title = node("span", "merge-league-group-title");
    title.append(
      node("span", "merge-league-side-label crown", "皇冠"),
      node("strong", "merge-league-side-name", crownName),
      Object.assign(node("code", "merge-league-id", crownId), {
        title: crownId === "—" ? "参考库未保存皇冠数字 ID" : "皇冠联赛 ID",
      }),
      node("span", "merge-league-arrow", "→"),
      node("span", "merge-league-side-label ps", "平博"),
      node("span", "merge-league-side-name ps", psName),
      node("code", "merge-league-id", psId),
    );
    groupHead.setAttribute("aria-label", `${crownName} ${crownId}，平博 ${psName} ${psId}`);
    groupHead.append(
      node("span", "merge-chevron", "›"),
      title,
      node("span", "merge-league-group-count", `${group.events.length} 场`),
    );
    const body = node("div", "merge-league-group-body");
    wrapper.append(groupHead, body);

    for (const event of group.events) {
      const card = node("div", "merge-fixture");
      card.dataset.crownFixtureId = event.id;
      const isOpen = expandedFixtures.has(event.id);
      if (isOpen) card.classList.add("open");

      const boundLeague = boundByCrownLeague.get(event.leagueId);
      const candidates = (candidatesByCrown.get(event.id) || [])
        .filter((item) => psById.has(item.ps38Id) && !mappedPs.has(item.ps38Id))
        .sort((a, b) => Number(b.score) - Number(a.score))
        .slice(0, MAX_CANDIDATES);
      const top = candidates[0];
      const topPs = top ? psById.get(top.ps38Id) : null;

      const head = document.createElement("button");
      head.type = "button";
      head.className = "merge-fixture-head";
      head.dataset.fixtureToggle = event.id;
      head.setAttribute("aria-expanded", isOpen ? "true" : "false");
      head.setAttribute("aria-controls", `fixture-body-${event.id}`);
      head.setAttribute("aria-label",
        `${isOpen ? "收起" : "展开"}皇冠 ${event.name} 的平博候选`);

      const crown = node("span", "merge-fixture-crown");
      crown.append(node("span", "merge-source-tag crown", "皇冠"));
      const title = node("span", "merge-fixture-title");
      title.append(event.home, node("span", "vs", "vs"), event.away);
      crown.append(title);
      head.append(crown);

      if (top && topPs) {
        const topBox = node("span", "merge-fixture-top");
        topBox.append(node("span", "merge-fixture-top-label", "平博"));
        topBox.append(node("span", "merge-fixture-top-name", `${topPs.home} vs ${topPs.away}`));
        topBox.append(node("span", "merge-fixture-top-score", Number(top.score).toFixed(1)));
        topBox.append(node("span", "merge-fixture-top-conf",
          top.confidence === "high" ? "高" : top.confidence === "medium" ? "中" : "低"));
        if (candidates.length > 1) {
          topBox.append(node("span", "merge-fixture-top-more", `+${candidates.length - 1}`));
        }
        head.append(topBox);
      } else {
        head.append(node("span", "merge-fixture-top is-empty", "暂无可选平博比赛"));
      }

      head.append(node("span", "merge-fixture-kickoff", shortTime(event.kickoffLocal) || "时间未知"));
      head.append(node("span", "merge-fixture-chevron", "›"));
      const summary = node("div", "merge-fixture-summary");
      summary.append(head);
      if (top && topPs) {
        const confirm = button("确认比赛", "merge-mini-btn merge-fixture-confirm", {
          quickConfirm: "true",
          crownId: event.id,
          ps38Id: top.ps38Id,
        });
        confirm.disabled = Boolean(loading || busy);
        if (top.timeConflict) confirm.classList.add("warn");
        confirm.title = `确认比赛：皇冠 ${event.home} vs ${event.away} → 平博 ${topPs.home} vs ${topPs.away}${top.timeConflict ? "（开赛时间不一致）" : ""}`;
        confirm.setAttribute("aria-label", confirm.title);
        summary.append(confirm);
      }
      card.append(summary);

      const meta = node("div", "merge-fixture-meta");
      const scoreText = String(event.score || "").trim();
      const minuteText = String(event.minute || "").trim();
      if (event.isLive) meta.append(node("span", "live", "进行中"));
      if (scoreText && ![":", "-", "—"].includes(scoreText)) {
        meta.append(node("span", "", scoreText));
      }
      if (minuteText) meta.append(node("span", "", minuteText));
      if (meta.childElementCount) card.append(meta);

      const fixtureBody = node("div", "merge-fixture-body");
      fixtureBody.id = `fixture-body-${event.id}`;

      if (candidates.length) {
        const list = node("div", "merge-candidate-list");
        for (const candidate of candidates) {
          const ps = psById.get(candidate.ps38Id);
          const element = node("button", "merge-candidate");
          element.type = "button";
          element.dataset.ps38Id = candidate.ps38Id;
          element.dataset.crownId = event.id;
          if (candidate.timeConflict) element.classList.add("warn");
          element.title = `将皇冠 ${event.name} 对应到平博 ${ps.name}`;
          const main = node("span", "merge-candidate-main");
          main.append(
            node("strong", "", `平博 · ${ps.home} vs ${ps.away}`),
            node(
              "span",
              "",
              `${ps.league} · ${shortTime(ps.kickoffLocal) || "时间未知"}${ps.isLive ? " · 进行中" : ""} · ${candidate.reasons.join(" · ")}`,
            ),
          );
          const score = node("span", "merge-candidate-score");
          score.append(
            node("b", "", Number(candidate.score).toFixed(1)),
            node("span", "", candidate.confidence === "high" ? "高" : candidate.confidence === "medium" ? "中" : "低"),
          );
          element.append(main, score);
          list.append(element);
        }
        fixtureBody.append(list);
      } else {
        const sameLeague = psByLeague.get(boundLeague.ps38Id) || [];
        const available = sameLeague.filter((item) => !mappedPs.has(item.id));
        const message = !data.sources?.ps38?.ok
          ? "平博赛事暂不可用；皇冠比赛已保留显示，请拉取平博赛事后继续对应。"
          : !sameLeague.length ? "对应平博联赛暂时没有当前比赛，等待平博更新。"
          : !available.length ? "对应平博联赛的当前比赛已全部合并，暂无可选比赛。"
          : "暂无相似度足够的平博候选，可在下方手动选择同联赛比赛。";
        fixtureBody.append(node("div", "merge-empty-hint", message));
      }

      const manual = node("div", "merge-manual");
      const select = node("select", "merge-select");
      select.dataset.manualSelect = event.id;
      select.setAttribute("aria-label", `为皇冠 ${event.name} 手动选择平博赛事`);
      select.append(option("", "手动选择对应平博赛事"));
      const psEvents = (psByLeague.get(boundLeague.ps38Id) || []).filter(
        (item) => !mappedPs.has(item.id),
      );
      for (const ps of psEvents) {
        select.append(
          option(
            ps.id,
            `${ps.home} vs ${ps.away} · ${shortTime(ps.kickoffLocal) || "时间未知"}`,
          ),
        );
      }
      const bind = button("合并选中", "merge-mini-btn", { manualBind: event.id });
      bind.disabled = !psEvents.length;
      select.disabled = !psEvents.length;
      bind.title = `合并 ${event.name}`;
      bind.setAttribute("aria-label", `合并 ${event.name}`);
      manual.append(select, bind);
      fixtureBody.append(manual);
      card.append(fixtureBody);
      body.append(card);
    }
    host.append(wrapper);
  }
}

function renderMergedFixtures(host, mappings) {
  host.replaceChildren();
  if (!mappings.length) {
    host.append(node("div", "merge-empty", "还没有确认的合并关系。先绑定联赛，或点「自动合并」。"));
    return;
  }
  const crownEventsById = new Map(
    (data?.crownEvents || [])
      .filter((event) => event?.id !== undefined && event?.id !== null)
      .map((event) => [String(event.id), event]),
  );
  const groups = new Map();
  for (const mapping of mappings) {
    const crown = mapping.crownSnapshot || {};
    const ps = mapping.ps38Snapshot || {};
    const key = String(crown.providerLeagueId || crown.leagueId || crown.league || mapping.crownId);
    if (!groups.has(key)) {
      groups.set(key, {
        id: key,
        crownName: crown.league || "未命名皇冠联赛",
        crownId: crown.providerLeagueId || (/^\d+$/.test(key) ? key : "—"),
        psName: ps.league || "未命名平博联赛",
        psId: ps.providerLeagueId || "—",
        fixtures: [],
      });
    }
    groups.get(key).fixtures.push(mapping);
  }
  if ($("mergeFixtureMeta")) {
    $("mergeFixtureMeta").textContent = `${groups.size} 个联赛 · ${mappings.length} 场已合并`;
  }
  const orderedGroups = [...groups.values()].sort((left, right) =>
    left.crownName.localeCompare(right.crownName, "zh-CN"),
  );
  for (const group of orderedGroups) {
    const wrapper = node("div", "merge-league-group");
    const open = expandedMergedLeagueGroups.has(group.id);
    wrapper.classList.toggle("open", open);
    const head = button("", "merge-league-group-head", { mergedLeagueToggle: group.id });
    head.setAttribute("aria-expanded", String(open));
    head.setAttribute("aria-label", `${group.crownName}，平博 ${group.psName}，${group.fixtures.length} 场已合并`);
    const title = node("span", "merge-league-group-title");
    title.append(
      node("span", "merge-league-side-label crown", "皇冠"),
      node("strong", "merge-league-side-name", group.crownName),
      node("code", "merge-league-id", group.crownId),
      node("span", "merge-league-arrow", "→"),
      node("span", "merge-league-side-label ps", "平博"),
      node("span", "merge-league-side-name ps", group.psName),
      node("code", "merge-league-id", group.psId),
    );
    head.append(
      node("span", "merge-chevron", "›"),
      title,
      node("span", "merge-league-group-count", `${group.fixtures.length} 场`),
    );
    const body = node("div", "merge-league-group-body");
    group.fixtures.sort((left, right) =>
      String(left.crownSnapshot?.kickoffUtc || left.crownSnapshot?.kickoffLocal || "")
        .localeCompare(String(right.crownSnapshot?.kickoffUtc || right.crownSnapshot?.kickoffLocal || "")),
    );
    for (const mapping of group.fixtures) {
      const crownEvent = crownEventsById.get(String(mapping.crownId ?? ""));
      body.append(renderMergedFixtureRow(mapping, crownEvent));
    }
    wrapper.append(head, body);
    host.append(wrapper);
  }
}

function renderMergedFixtureRow(mapping, crownEvent) {
  const row = node("div", "merge-mapped-row");
  const left = node("div", "merge-mapped-side");
  const mergedPsId = mapping.ps38Snapshot?.providerLeagueId || "—";
  const mergedCrownLeague = mapping.crownSnapshot?.league || "未命名联赛";
  const mergedCrownIdRaw = mapping.crownSnapshot?.providerLeagueId || mapping.crownSnapshot?.leagueId || "—";
  const mergedCrownId = mergedCrownIdRaw === mergedCrownLeague ? "—" : mergedCrownIdRaw;
  left.append(
    node("span", "", `平博 · ${mapping.ps38Snapshot?.league || "未命名联赛"} · ${mergedPsId}`),
    node("strong", "", mapping.ps38Snapshot?.name || mapping.ps38Id),
    node("small", "", `${shortTime(mapping.ps38Snapshot?.kickoffLocal)} · ${mapping.ps38Snapshot?.orders ?? "—"} 单`),
  );
  const right = node("div", "merge-mapped-side");
  const crownHomeId = String(mapping.crownSnapshot?.homeId ?? "").trim()
    || String(crownEvent?.homeId ?? "").trim()
    || "—";
  const crownAwayId = String(mapping.crownSnapshot?.awayId ?? "").trim()
    || String(crownEvent?.awayId ?? "").trim()
    || "—";
  right.append(
    node("span", "", `皇冠 · ${mergedCrownLeague} · ${mergedCrownId}`),
    node("strong", "", mapping.crownSnapshot?.name || mapping.crownId),
    node("small", "", shortTime(mapping.crownSnapshot?.kickoffLocal) || "时间未知"),
    node("small", "merge-crown-team-ids", `主队 ID ${crownHomeId} · 客队 ID ${crownAwayId}`),
  );
  row.append(
    right,
    node("span", "merge-mapped-arrow", "→"),
    left,
    button("解绑", "merge-mini-btn danger", { unmapFixture: mapping.ps38Id }),
  );
  return row;
}

function renderAll() {
  renderStatus();
  renderLeagues();
  renderFixtures();
  updateControls();
  if ($("mergeDialog")?.open) renderDialog();
}

function librarySearchText(mapping) {
  return [
    mapping.crownSnapshot?.name,
    mapping.crownSnapshot?.providerId,
    mapping.crownId,
    mapping.ps38Snapshot?.name,
    mapping.ps38Snapshot?.providerId,
    mapping.ps38Id,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function libraryRow(mapping) {
  const row = document.createElement("tr");
  const crownName = node("td", "merge-library-cell-name", mapping.crownSnapshot?.name || mapping.crownId);
  const crownId = node(
    "td",
    "merge-library-id",
    mapping.crownSnapshot?.providerId || mapping.crownId,
  );
  const psName = node("td", "merge-library-cell-name", mapping.ps38Snapshot?.name || mapping.ps38Id);
  const psId = node(
    "td",
    "merge-library-id",
    mapping.ps38Snapshot?.providerId || "—",
  );
  const updated = node("td", "merge-library-id", fullStamp(mapping.updatedAt));
  const op = node("td", "merge-library-op");
  const unbind = button("解绑联赛", "merge-mini-btn danger", { unbindLeague: mapping.crownId });
  unbind.setAttribute("aria-label", `解绑联赛：${mapping.crownSnapshot?.name || mapping.crownId}`);
  op.append(unbind);
  row.append(crownName, crownId, psName, psId, updated, op);
  return row;
}

function renderLeagueLibrary(body) {
  const mappings = [...(data?.leagueMappings || [])].sort((left, right) =>
    String(left.crownSnapshot?.name || left.crownId).localeCompare(
      String(right.crownSnapshot?.name || right.crownId),
      "zh-CN",
    ),
  );
  if (!mappings.length) {
    body.append(node("div", "merge-dialog-empty", "尚未保存联赛绑定。在左侧绑定皇冠和平博联赛后，会自动保存到这里。"));
    return;
  }

  body.append(node("p", "merge-dialog-summary", "皇冠联赛 → 平博联赛 · 绑定长期保存，刷新或重启后仍生效；可在此解绑，已确认的比赛对照保留。"));
  const toolbar = node("div", "merge-library-toolbar");
  const search = node("label", "merge-library-search");
  const input = document.createElement("input");
  input.type = "search";
  input.placeholder = "搜索联赛名称、皇冠 ID、平博 ID";
  input.value = libraryQuery;
  input.setAttribute("aria-label", "搜索联赛库");
  search.append(input);
  const refresh = button("刷新", "merge-mini-btn ghost");
  const count = node("span", "merge-library-count");
  toolbar.append(search, refresh, count);

  const wrap = node("div", "merge-library-table-wrap");
  const table = node("table", "merge-library-table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["皇冠联赛", "皇冠 ID", "平博联赛", "平博 ID", "更新时间", "操作"]) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = label;
    headRow.append(th);
  }
  head.append(headRow);
  const tbody = document.createElement("tbody");
  table.append(head, tbody);
  wrap.append(table);
  body.append(toolbar, wrap);

  const apply = () => {
    const query = libraryQuery.trim().toLowerCase();
    const filtered = query ? mappings.filter((item) => librarySearchText(item).includes(query)) : mappings;
    tbody.replaceChildren();
    if (!filtered.length) {
      const row = document.createElement("tr");
      const cell = node("td", "merge-library-empty-cell", "没有匹配的联赛");
      cell.colSpan = 6;
      row.append(cell);
      tbody.append(row);
    } else {
      for (const mapping of filtered) tbody.append(libraryRow(mapping));
    }
    count.textContent = query
      ? `${filtered.length} / ${mappings.length} 条`
      : `共 ${mappings.length} 条`;
  };

  input.addEventListener("input", () => {
    libraryQuery = input.value;
    apply();
  });
  refresh.addEventListener("click", () => loadMerge({ silent: true }));
  apply();
}

function renderMergedLibrary(body) {
  const mappings = data?.mappings || [];
  if (!mappings.length) {
    body.append(node("div", "merge-dialog-empty", "还没有已合并的赛事。先绑定联赛，再确认赛事或点「自动合并」。"));
    return;
  }
  const groups = new Map();
  for (const mapping of mappings) {
    const ps = mapping.ps38Snapshot || {};
    const crown = mapping.crownSnapshot || {};
    const key = `${ps.leagueId || ps.league}::${crown.leagueId || crown.league}`;
    if (!groups.has(key)) {
      groups.set(key, {
        psLeague: ps.league || "平博",
        crownLeague: crown.league || "皇冠",
        fixtures: [],
        updatedAt: mapping.updatedAt || "",
      });
    }
    const group = groups.get(key);
    group.fixtures.push(mapping);
    if (String(mapping.updatedAt || "") > String(group.updatedAt || "")) {
      group.updatedAt = mapping.updatedAt;
    }
  }
  body.append(node("p", "merge-dialog-summary", `${groups.size} 个联赛 · ${mappings.length} 场赛事`));
  const ordered = [...groups.values()].sort(
    (left, right) => right.fixtures.length - left.fixtures.length,
  );
  for (const group of ordered) {
    const section = node("section", "merge-library-group");
    const header = node("header");
    const pair = node("div", "merge-library-pair");
    pair.append(
      node("span", "", "皇冠"),
      node("strong", "", group.crownLeague),
      node("span", "arrow", "→"),
      node("span", "", "平博"),
      node("strong", "", group.psLeague),
    );
    header.append(pair, node("span", "merge-library-meta", `${group.fixtures.length} 场`));
    const list = node("ul");
    for (const mapping of group.fixtures) {
      const ps = mapping.ps38Snapshot || {};
      const crown = mapping.crownSnapshot || {};
      const item = node("li");
      item.append(
        node("span", "", crown.name || crown.id),
        node("span", "arrow", "→"),
        node("span", "", ps.name || ps.id),
        node("small", "", `${shortTime(crown.kickoffLocal)} · ${ps.orders ?? "—"} 单`),
      );
      list.append(item);
    }
    section.append(header, list);
    body.append(section);
  }
}

function renderDialog() {
  const dialog = $("mergeDialog");
  const body = $("mergeDialogBody");
  if (!dialog || !body) return;
  dialog.dataset.mode = dialogMode || "leagues";
  const title = $("mergeDialogTitle");
  const kicker = $("mergeDialogKicker");
  body.replaceChildren();
  if (!data) {
    body.append(node("div", "merge-dialog-empty", "正在读取合并数据…"));
    return;
  }
  if (dialogMode === "leagues") {
    if (kicker) kicker.textContent = "LEAGUE LIBRARY";
    if (title) title.textContent = "联赛库";
    renderLeagueLibrary(body);
  } else {
    if (kicker) kicker.textContent = "MERGED LEAGUES";
    if (title) title.textContent = "已合并联赛";
    renderMergedLibrary(body);
  }
}

function openDialog(mode) {
  const dialog = $("mergeDialog");
  if (!dialog) return;
  dialogMode = mode;
  renderDialog();
  if (!dialog.open) {
    try {
      dialog.showModal();
    } catch {
      dialog.setAttribute("open", "");
    }
  }
}

function closeDialog() {
  const dialog = $("mergeDialog");
  if (dialog?.open) dialog.close();
}

async function refreshCrown() {
  if (busy || loading) return;
  busy = true;
  updateControls();
  try {
    const result = await postAction("/api/merge/refresh", { source: "crown" });
    showToast((result.message || "皇冠赛事已拉取") + (result.autoBound ? `，自动匹配 ${result.autoBound} 场` : ""));
    await loadMerge({ silent: true });
    if (result.autoError) showNotice(`赛事已拉取，自动匹配暂未执行：${result.autoError}`);
  } catch (error) {
    showToast(`拉取皇冠赛事失败：${error?.message || error}`);
    await loadMerge({ silent: true });
  } finally {
    busy = false;
    updateControls();
  }
}

async function refreshPs38() {
  if (busy || loading) return;
  busy = true;
  updateControls();
  try {
    const result = await postAction("/api/merge/refresh", { source: "pinnacle" });
    showToast((result.message || "平博赛事已拉取") + (result.autoBound ? `，自动匹配 ${result.autoBound} 场` : ""));
    await loadMerge({ silent: true });
    if (result.autoError) showNotice(`赛事已拉取，自动匹配暂未执行：${result.autoError}`);
  } catch (error) {
    showToast(`拉取平博赛事失败：${error?.message || error}`);
    await loadMerge({ silent: true });
  } finally {
    busy = false;
    updateControls();
  }
}

async function runAction(action, successMessage, fixtureId = null, applyResult = null) {
  if (busy) return;
  busy = true;
  confirmingFixtureId = fixtureId;
  updateControls();
  try {
    const result = await action();
    showToast(successMessage + (result?.autoBound ? `，自动匹配 ${result.autoBound} 场，已放入「已合并」` : ""));
    if (!applyResult || !applyResult(result)) await loadMerge({ silent: true });
    if (result?.autoError) showNotice(`${successMessage}，自动匹配暂未执行：${result.autoError}`);
  } catch (error) {
    showToast(String(error?.message || error));
  } finally {
    busy = false;
    confirmingFixtureId = null;
    updateControls();
  }
}

function confirmFixture(ps38Id, crownId) {
  if (loading || busy) return;
  runAction(
    () => postAction("/api/merge/mapping", { action: "map", ps38Id, crownId }),
    "赛事已合并",
    crownId,
    (result) => {
      const saved = result?.mapping;
      if (!data || saved?.ps38Id !== ps38Id || saved?.crownId !== crownId) return false;
      data.mappings = (data.mappings || []).filter(
        (item) => item.ps38Id !== ps38Id && item.crownId !== crownId,
      );
      data.mappings.push(saved);
      data.counts = { ...data.counts, fixtureMappings: data.mappings.length };
      expandedFixtures.delete(crownId);
      const host = $("mergeFixtures");
      const scrollTop = host?.scrollTop || 0;
      renderStatus();
      renderFixtures();
      if (host) host.scrollTop = scrollTop;
      if ($("mergeDialog")?.open) renderDialog();
      return true;
    },
  );
}

function onLeagueClick(event) {
  const bind = event.target.closest("[data-bind-crown]");
  if (!bind) return;
  const crownId = bind.dataset.bindCrown;
  const select = [...document.querySelectorAll("[data-crown-select]")].find(
    (item) => item.dataset.crownSelect === crownId,
  );
  const psId = select?.value;
  if (!psId) {
    showToast("先选择一个对应平博联赛");
    return;
  }
  runAction(
    async () => {
      const result = await postAction("/api/merge/league-mapping", {
        action: "map",
        ps38LeagueId: psId,
        crownLeagueId: crownId,
      });
      expandedLeagueGroups.add(crownId);
      matchView = "pending";
      return result;
    },
    "联赛已绑定",
  );
}

function onFixtureClick(event) {
  const mergedLeagueToggle = event.target.closest("[data-merged-league-toggle]");
  if (mergedLeagueToggle) {
    const group = mergedLeagueToggle.closest(".merge-league-group");
    const open = group.classList.toggle("open");
    mergedLeagueToggle.setAttribute("aria-expanded", String(open));
    const leagueId = mergedLeagueToggle.dataset.mergedLeagueToggle;
    if (open) expandedMergedLeagueGroups.add(leagueId);
    else expandedMergedLeagueGroups.delete(leagueId);
    return;
  }
  const fixtureToggle = event.target.closest("[data-fixture-toggle]");
  if (fixtureToggle) {
    const fixture = fixtureToggle.closest(".merge-fixture");
    const open = fixture.classList.toggle("open");
    fixtureToggle.setAttribute("aria-expanded", open ? "true" : "false");
    const fixtureId = fixtureToggle.dataset.fixtureToggle;
    if (open) expandedFixtures.add(fixtureId);
    else expandedFixtures.delete(fixtureId);
    fixtureToggle.setAttribute("aria-label",
      `${open ? "收起" : "展开"}皇冠 ${fixture.querySelector(".merge-fixture-title")?.textContent || "比赛"} 的平博候选`);
    return;
  }
  const leagueToggle = event.target.closest("[data-league-toggle]");
  if (leagueToggle) {
    const group = leagueToggle.closest(".merge-league-group");
    const open = group.classList.toggle("open");
    leagueToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) expandedLeagueGroups.add(leagueToggle.dataset.leagueToggle);
    else expandedLeagueGroups.delete(leagueToggle.dataset.leagueToggle);
    return;
  }
  const unmap = event.target.closest("[data-unmap-fixture]");
  if (unmap) {
    if (!window.confirm("解除这条赛事合并？")) return;
    runAction(
      () =>
        postAction("/api/merge/mapping", {
          action: "unmap",
          ps38Id: unmap.dataset.unmapFixture,
        }),
      "赛事合并已解除",
    );
    return;
  }
  const candidate = event.target.closest("[data-crown-id]");
  if (candidate) {
    if (candidate.disabled || loading || busy) return;
    if (candidate.classList.contains("warn") && !window.confirm("这场平博候选的开赛时间与皇冠不一致，仍要合并？")) {
      return;
    }
    confirmFixture(candidate.dataset.ps38Id, candidate.dataset.crownId);
    return;
  }
  const manual = event.target.closest("[data-manual-bind]");
  if (manual) {
    const crownId = manual.dataset.manualBind;
    const select = [...document.querySelectorAll("[data-manual-select]")].find(
      (item) => item.dataset.manualSelect === crownId,
    );
    const ps38Id = select?.value;
    if (!ps38Id) {
      showToast("先选择一场对应平博赛事");
      return;
    }
    confirmFixture(ps38Id, crownId);
  }
}

async function autoMerge() {
  if (!data || busy) return;
  const message = [
    "自动合并只匹配已绑定联赛下的当前赛事：",
    "· 双方队名和主客顺序吻合，开赛时间已知且相差不超过 15 分钟",
    "· 两边候选都唯一，近似候选有歧义时留给人工确认",
    "未满足条件的候选会保留在页面上，可继续人工确认。继续？",
  ].join("\n");
  if (!window.confirm(message)) return;
  busy = true;
  updateControls();
  try {
    const result = await postAction("/api/merge/auto", {});
    const leagues = result.mappedLeagues?.length || 0;
    const fixtures = result.mappedFixtures?.length || 0;
    showToast(
      leagues || fixtures
        ? `自动合并完成：联赛 ${leagues} 个，赛事 ${fixtures} 场`
        : "没有满足自动合并条件的候选，请人工确认",
    );
    await loadMerge({ silent: true });
  } catch (error) {
    showToast(String(error?.message || error));
  } finally {
    busy = false;
    updateControls();
  }
}

export function bindMergeEvents() {
  $("mergeRefresh")?.addEventListener("click", () => loadMerge());
  $("mergeAuto")?.addEventListener("click", autoMerge);
  $("mergeLeagues")?.addEventListener("click", onLeagueClick);
  $("mergeFixtures")?.addEventListener("click", onFixtureClick);
  for (const tab of document.querySelectorAll("[data-merge-view]")) {
    tab.addEventListener("click", () => {
      const view = tab.dataset.mergeView;
      if (view === matchView) return;
      matchView = view;
      renderFixtures();
      updateControls();
    });
  }
  $("mergeFetchCrown")?.addEventListener("click", refreshCrown);
  $("mergeFetchPs38")?.addEventListener("click", refreshPs38);
  $("mergeLeagueLibrary")?.addEventListener("click", () => openDialog("leagues"));
  $("mergeMergedLeagues")?.addEventListener("click", () => openDialog("merged"));
  $("mergeDialogClose")?.addEventListener("click", closeDialog);
  $("mergeDialogBody")?.addEventListener("click", (event) => {
    const unbind = event.target.closest("[data-unbind-league]");
    if (!unbind) return;
    if (busy || loading) return;
    const mapping = data?.leagueMappings?.find((item) => item.crownId === unbind.dataset.unbindLeague);
    const leagueName = mapping?.crownSnapshot?.name || unbind.dataset.unbindLeague;
    if (!window.confirm(`解除「${leagueName}」的联赛绑定？\n后续比赛需重新绑定联赛才能匹配。已确认的比赛对照会保留，可在「已合并」中单独解绑。`)) return;
    runAction(
      () =>
        postAction("/api/merge/league-mapping", {
          action: "unmap",
          crownLeagueId: unbind.dataset.unbindLeague,
        }),
      "联赛绑定已解除，已确认的比赛对照保留",
    );
  });
  $("mergeDialog")?.addEventListener("close", () => {
    dialogMode = null;
  });
  $("mergeDialog")?.addEventListener("click", (event) => {
    if (event.target === $("mergeDialog")) closeDialog();
  });

  window.addEventListener("ps38:pagenav", (event) => {
    if (event.detail?.page === "merge") setMergePageActive(true);
    else {
      setMergePageActive(false);
      closeDialog();
    }
  });

  const initial = String(window.location.hash || "").replace(/^#\/?/, "");
  if (initial === "merge") setMergePageActive(true);
}
