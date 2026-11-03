import { $ } from "./dom.js";
import { fetchJson, postJson } from "./http.js";
import { refreshMyEventsAll } from "./my-events/page.js";

let wasBackfilling = false;

const STATUS_TEXT = {
  starting: "启动中",
  login: "等待登录",
  online: "采集在线",
  error: "采集异常",
  stopped: "已停止",
};

function setPill(kind, text, title) {
  const pill = $("collectorPill");
  if (!pill) return;
  pill.classList.remove("live", "login", "error");
  if (kind === "online") pill.classList.add("live");
  else if (kind === "login") pill.classList.add("login");
  else if (kind === "error") pill.classList.add("error");
  const label = $("collectorPillText");
  if (label) label.textContent = text;
  pill.title = title || text;
}

export async function refreshCollectorStatus() {
  try {
    const { response, payload } = await fetchJson(
      "/api/status",
      { cache: "no-store" },
      { timeoutMs: 6000, allowInvalidJson: true },
    );
    if (!response.ok || payload.ok === false) {
      setPill("error", "服务异常", payload.error || `HTTP ${response.status}`);
      window.dispatchEvent(new CustomEvent("ps38:status", {
        detail: { ok: false, error: payload.error || `HTTP ${response.status}` },
      }));
      return;
    }
    window.dispatchEvent(new CustomEvent("ps38:status", { detail: payload }));
    const session = payload.session || {};
    const backfill = payload.backfill || {};
    const status = String(session.status || "starting");
    const message = String(session.message || "");
    const profile = session.profile || {};

    const code = profile.user_code || "";
    if (status === "online") {
      setPill("online", code ? `在线 · ${code}` : "在线", message);
    } else if (status === "login") {
      setPill("login", "等待登录", message);
    } else if (status === "error") {
      setPill("error", "采集异常", message);
    } else if (status === "stopped") {
      setPill("error", "已停止", message);
    } else {
      setPill("", STATUS_TEXT[status] || status, message);
    }

    const backfilling = Boolean(backfill.running);
    if (wasBackfilling && !backfilling) {
      // A finished backfill only writes SETTLED rows, so refresh explicitly.
      refreshMyEventsAll({ reset: true });
    }
    wasBackfilling = backfilling;

    const backfillButton = $("collectorBackfill");
    if (backfillButton) {
      backfillButton.disabled = Boolean(backfill.running);
      backfillButton.title = backfill.running
        ? `正在补采：${Number(backfill.done || 0)}/${Number(backfill.total || 0)}`
        : "补采 history_from 至今的已结算订单";
    }
  } catch (error) {
    setPill("error", "无法连接", String(error?.message || error));
    window.dispatchEvent(new CustomEvent("ps38:status", {
      detail: { ok: false, error: String(error?.message || error) },
    }));
  }
}

export async function requestRefresh() {
  const payload = await postJson("/api/actions/refresh");
  return payload.message || "已请求刷新";
}

export async function requestBackfill() {
  const payload = await postJson("/api/actions/backfill");
  return payload.message || "已请求补采";
}

export async function requestLogin() {
  const payload = await postJson("/api/actions/login");
  return payload.message || "正在重新登录";
}

export async function requestOpenLogin() {
  const payload = await postJson("/api/actions/open-login");
  return payload.message || "正在打开后台浏览器窗口…";
}
