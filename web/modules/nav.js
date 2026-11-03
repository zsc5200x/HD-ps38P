/** Top capsule navigation: deep-linkable page switching + sliding active pill. */
import { showToast } from "./dom.js";
import { refreshCollectorStatus, requestBackfill, requestLogin, requestRefresh } from "./status.js";

const ACTION_RUNNERS = {
  refresh: requestRefresh,
  backfill: requestBackfill,
  relogin: requestLogin,
};

const PAGES = {
  home: { hash: "#/home", title: "HD PS38 Sports", label: "主页" },
  copy: { hash: "#/copy", title: "PS38 跟单 · HD PS38 Sports", label: "PS38 跟单" },
  merge: { hash: "#/merge", title: "赛事合并 · HD PS38 Sports", label: "赛事合并" },
  pool: { hash: "#/pool", title: "号池管理 · HD PS38 Sports", label: "号池管理" },
  settings: { hash: "#/settings", title: "设置 · HD PS38 Sports", label: "设置" },
};

const DEFAULT_PAGE = "home";

let currentPage = null;

function pageFromHash() {
  const raw = String(window.location.hash || "").replace(/^#\/?/, "").toLowerCase();
  return Object.prototype.hasOwnProperty.call(PAGES, raw) ? raw : DEFAULT_PAGE;
}

function linkFor(pageId) {
  return document.querySelector(`[data-page-link="${pageId}"]`);
}

function panelFor(pageId) {
  return document.querySelector(`[data-page-panel="${pageId}"]`);
}

function moveGlider(link, animate = true) {
  const host = link?.closest(".nav-links");
  const glider = host?.querySelector(".nav-glider");
  if (!host || !glider || !link.offsetWidth) return;
  if (!animate) glider.style.transition = "none";
  glider.style.width = `${link.offsetWidth}px`;
  glider.style.transform = `translateX(${link.offsetLeft}px)`;
  glider.classList.add("ready");
  if (!animate) {
    // Flush the layout write before handing transitions back to CSS.
    void glider.offsetWidth;
    glider.style.transition = "";
  }
}

function applyPage(pageId, { focus = false, animate = true } = {}) {
  currentPage = pageId;
  for (const panel of document.querySelectorAll("[data-page-panel]")) {
    panel.hidden = panel.dataset.pagePanel !== pageId;
  }
  for (const link of document.querySelectorAll("[data-page-link]")) {
    if (link.dataset.pageLink === pageId) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  moveGlider(linkFor(pageId), animate);

  const page = PAGES[pageId];
  const title = document.getElementById("appPageTitle");
  if (title) title.textContent = page.label;
  for (const group of document.querySelectorAll("[data-page-actions]")) {
    group.hidden = group.dataset.pageActions !== pageId;
  }

  document.title = page.title;
  window.dispatchEvent(new CustomEvent("ps38:pagenav", { detail: { page: pageId } }));
  if (focus) {
    (title || panelFor(pageId))?.focus({ preventScroll: true });
  }
}

function navigate(pageId) {
  const target = Object.prototype.hasOwnProperty.call(PAGES, pageId) ? pageId : DEFAULT_PAGE;
  const changed = target !== currentPage;
  if (changed) applyPage(target, { focus: true });
  const { hash } = PAGES[target];
  if (window.location.hash !== hash) window.history.pushState({ page: target }, "", hash);
}

function bindLinks() {
  for (const link of document.querySelectorAll("[data-page-link]")) {
    link.addEventListener("click", (event) => {
      // Keep modifier clicks (new tab, download, ...) behaving like real links.
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      navigate(link.dataset.pageLink);
    });
  }
}

function bindActions() {
  for (const trigger of document.querySelectorAll("[data-nav-action]")) {
    trigger.addEventListener("click", (event) => {
      const action = trigger.dataset.navAction;
      if (trigger.tagName === "A") event.preventDefault();
      if (action === "fixtures") {
        navigate("home");
        document.querySelector('[data-my-events-view="fixtures"]')?.click();
        return;
      }
      const targetId = {
        home: null,
        "open-login": "myEventsLogin",
      }[action];
      if (action === "home") {
        navigate("home");
        return;
      }
      const runner = ACTION_RUNNERS[action];
      if (runner) {
        const confirmText = {
          backfill: "补采 history_from 至今的已结算订单，可能需要几分钟；期间新单仍会照常拉取。继续？",
          relogin: "将清除当前浏览器会话并重新打开登录窗口，已登录状态会退出。继续？",
        }[action];
        if (confirmText && !window.confirm(confirmText)) return;
        runner()
          .then((message) => showToast(message))
          .catch((error) => showToast(`操作失败：${error?.message || error}`))
          .finally(() => refreshCollectorStatus());
        return;
      }
      if (targetId) document.getElementById(targetId)?.click();
    });
  }
}

function bindRouteEvents() {
  const sync = () => {
    const pageId = pageFromHash();
    if (pageId !== currentPage) applyPage(pageId, { focus: true });
  };
  window.addEventListener("hashchange", sync);
  window.addEventListener("popstate", sync);
}

export function bindTopNav() {
  const initial = pageFromHash();
  applyPage(initial, { focus: false, animate: false });
  const { hash } = PAGES[initial];
  if (window.location.hash !== hash) window.history.replaceState({ page: initial }, "", hash);

  bindLinks();
  bindActions();
  bindRouteEvents();

  const reposition = () => moveGlider(linkFor(currentPage), false);
  document.fonts?.ready.then(reposition).catch(() => {});
  if ("ResizeObserver" in window) {
    const host = document.querySelector(".top-nav");
    if (host) new ResizeObserver(reposition).observe(host);
  } else {
    window.addEventListener("resize", reposition);
  }
}
