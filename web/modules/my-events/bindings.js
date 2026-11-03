import { $, showToast } from "../dom.js";
import { state } from "../state.js";
import { refreshCollectorStatus, requestBackfill, requestLogin, requestOpenLogin, requestRefresh } from "../status.js";
import {
  clearMyEventsBanner,
  loadMoreMyEvents,
  refreshMyEventsAll,
  setMyEventsView,
} from "./page.js";

function setActiveTab(active) {
  document.querySelectorAll("[data-my-events-status]").forEach((button) => {
    const current = button === active;
    button.classList.toggle("active", current);
    button.setAttribute("aria-pressed", String(current));
  });
}

function markQuick(activeValue) {
  document.querySelectorAll("[data-my-events-range]").forEach((button) => {
    button.classList.toggle("active", button.dataset.myEventsRange === activeValue);
  });
}

function ymdInTz(offsetMinutes, date = new Date()) {
  const shifted = new Date(date.getTime() + Number(offsetMinutes) * 60000);
  return shifted.toISOString().slice(0, 10);
}

function addDaysYmd(text, days) {
  const date = new Date(`${text}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function applyRange(value) {
  const current = state.myEvents;
  const today = ymdInTz(current.tz);
  let from = "";
  let to = "";
  if (value === "today") {
    from = today;
    to = today;
  } else if (value === "24") {
    from = addDaysYmd(today, -1);
    to = today;
  } else if (value === "7" || value === "30") {
    from = addDaysYmd(today, -(Number(value) - 1));
    to = today;
  }
  current.from = from;
  current.to = to;
  current.hours = value === "24" ? 24 : 0;
  const fromInput = $("myEventsFrom");
  const toInput = $("myEventsTo");
  if (fromInput) fromInput.value = from;
  if (toInput) toInput.value = to;
  markQuick(value);
  clearMyEventsBanner();
  refreshMyEventsAll({ reset: true });
}

function bindCollectorActions() {
  $("collectorRefresh")?.addEventListener("click", async () => {
    try {
      showToast(await requestRefresh());
    } catch (error) {
      showToast(`刷新失败：${error.message || error}`);
    }
    // Give the collector a moment to finish the upstream round.
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
    await refreshMyEventsAll({ reset: true });
    await refreshCollectorStatus();
  });

  $("collectorBackfill")?.addEventListener("click", async () => {
    if (!window.confirm("补采 history_from 至今的已结算订单，可能需要几分钟；期间新单仍会照常拉取。继续？")) {
      return;
    }
    try {
      showToast(await requestBackfill());
    } catch (error) {
      showToast(`补采失败：${error.message || error}`);
    }
    await refreshCollectorStatus();
  });

  $("collectorLogin")?.addEventListener("click", async () => {
    if (!window.confirm("将清除当前浏览器会话并重新打开登录窗口；已登录状态会退出。继续？")) {
      return;
    }
    try {
      showToast(await requestLogin());
    } catch (error) {
      showToast(`登录失败：${error.message || error}`);
    }
    await refreshCollectorStatus();
  });
}

/** 列表滚到接近底部时自动取下一页，不再显示「加载更多」按钮。 */
function bindLoadMoreOnScroll() {
  const views = [
    ["myEventsOrdersWrap", "orders"],
    ["myEventsCopyWrap", "copy"],
    ["myEventsFixtureWrap", "fixtures"],
  ];
  for (const [id, view] of views) {
    const wrap = $(id);
    if (!wrap) continue;
    wrap.addEventListener(
      "scroll",
      () => {
        if (state.myEvents.view !== view) return;
        if (wrap.scrollTop + wrap.clientHeight < wrap.scrollHeight - 160) return;
        loadMoreMyEvents();
      },
      { passive: true },
    );
  }
}

export function bindMyEventsEvents() {
  const initialFrom = $("myEventsFrom");
  const initialTo = $("myEventsTo");
  if (initialFrom) initialFrom.value = state.myEvents.from;
  if (initialTo) initialTo.value = state.myEvents.to;
  const initialTz = $("myEventsTz");
  if (initialTz) initialTz.value = String(state.myEvents.tz);
  markQuick("24");
  setActiveTab(document.querySelector('[data-my-events-status="ALL"]'));
  for (const item of document.querySelectorAll("[data-my-events-basis]")) {
    const active = item.dataset.myEventsBasis === state.myEvents.basis;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  }
  setMyEventsView(state.myEvents.view);

  bindCollectorActions();

  document.querySelectorAll("[data-my-events-status]").forEach((button) => {
    button.addEventListener("click", () => {
      state.myEvents.status = button.dataset.myEventsStatus;
      state.myEvents.offset = 0;
      state.myEvents.fixtureOffset = 0;
      setActiveTab(button);
      clearMyEventsBanner();
      refreshMyEventsAll({ reset: true });
    });
  });

  const search = $("myEventsSearch");
  let timer = null;
  search?.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      state.myEvents.search = search.value.trim();
      refreshMyEventsAll({ reset: true });
    }, 250);
  });

  const agent = $("myEventsAgent");
  agent?.addEventListener("change", () => {
    state.myEvents.agent = agent.value;
    clearMyEventsBanner();
    refreshMyEventsAll({ reset: true });
  });

  $("myEventsRefresh")?.addEventListener("click", () => {
    refreshMyEventsAll({ reset: true });
    refreshCollectorStatus();
  });

  $("myEventsLogin")?.addEventListener("click", async () => {
    try {
      showToast(await requestOpenLogin());
    } catch (error) {
      showToast(`打开后台失败：${error.message || error}`);
    }
    // The poller brings the window up on its next tick; refresh status so the
    // bar reflects it right away.
    window.setTimeout(() => refreshCollectorStatus(), 800);
  });

  bindLoadMoreOnScroll();

  document.querySelector('[data-my-events-view="fixtures"]')?.addEventListener("click", () => {
    if (state.myEvents.view === "fixtures") return;
    setMyEventsView("fixtures");
    state.myEvents.fixtureOffset = 0;
    clearMyEventsBanner();
    refreshMyEventsAll({ reset: true });
  });

  document.querySelector('[data-my-events-view="copy"]')?.addEventListener("click", () => {
    if (state.myEvents.view === "copy") return;
    setMyEventsView("copy");
    state.myEvents.copyOffset = 0;
    clearMyEventsBanner();
    refreshMyEventsAll({ reset: true });
  });

  $("myEventsFrom")?.addEventListener("change", (event) => {
    state.myEvents.from = event.target.value;
    state.myEvents.hours = 0;
    markQuick("");
    clearMyEventsBanner();
    refreshMyEventsAll({ reset: true });
  });

  $("myEventsTo")?.addEventListener("change", (event) => {
    state.myEvents.to = event.target.value;
    state.myEvents.hours = 0;
    markQuick("");
    clearMyEventsBanner();
    refreshMyEventsAll({ reset: true });
  });

  document.querySelectorAll("[data-my-events-range]").forEach((button) => {
    button.addEventListener("click", () => applyRange(button.dataset.myEventsRange));
  });

  document.querySelectorAll("[data-my-events-basis]").forEach((button) => {
    button.addEventListener("click", () => {
      const basis = button.dataset.myEventsBasis;
      const backToOrders = state.myEvents.view !== "orders";
      setMyEventsView("orders");
      if (basis === state.myEvents.basis && !backToOrders) return;
      state.myEvents.basis = basis;
      for (const item of document.querySelectorAll("[data-my-events-basis]")) {
        const active = item.dataset.myEventsBasis === basis;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      }
      if (basis === "settled" && state.myEvents.status === "OPEN") {
        state.myEvents.status = "SETTLED";
        setActiveTab(document.querySelector('[data-my-events-status="SETTLED"]'));
      }
      state.myEvents.offset = 0;
      state.myEvents.fixtureOffset = 0;
      clearMyEventsBanner();
      refreshMyEventsAll({ reset: true });
    });
  });

  $("myEventsTz")?.addEventListener("change", (event) => {
    state.myEvents.tz = Number(event.target.value) || 480;
    state.myEvents.offset = 0;
    state.myEvents.fixtureOffset = 0;
    clearMyEventsBanner();
    refreshMyEventsAll({ reset: true });
  });

  $("myEventsAlignPartner")?.addEventListener("click", () => {
    const current = state.myEvents;
    const target = addDaysYmd(ymdInTz(-240), -1);
    setMyEventsView("orders");
    current.basis = "settled";
    current.tz = -240;
    current.from = target;
    current.to = target;
    current.status = "SETTLED";
    current.hours = 0;
    for (const item of document.querySelectorAll("[data-my-events-basis]")) {
      const active = item.dataset.myEventsBasis === "settled";
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", String(active));
    }
    const tzSelect = $("myEventsTz");
    if (tzSelect) tzSelect.value = "-240";
    const fromInput = $("myEventsFrom");
    const toInput = $("myEventsTo");
    if (fromInput) fromInput.value = target;
    if (toInput) toInput.value = target;
    setActiveTab(document.querySelector('[data-my-events-status="SETTLED"]'));
    current.offset = 0;
    markQuick("");
    clearMyEventsBanner();
    refreshMyEventsAll({ reset: true });
  });

  refreshMyEventsAll({ reset: true });
  refreshCollectorStatus();
}
