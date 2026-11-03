/** Compact "SYSTEM" session card shown beside the 我的赛事 heading. */
import { $ } from "./dom.js";

const SPARK_BARS = 48;
const MAX_SAMPLES = 48;

const samples = [];
let bars = null;

function text(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

function ensureBars() {
  if (bars) return bars;
  const host = $("systemSpark");
  if (!host) return null;
  host.replaceChildren();
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < SPARK_BARS; index += 1) {
    fragment.appendChild(document.createElement("i"));
  }
  host.appendChild(fragment);
  bars = Array.from(host.children);
  return bars;
}

function renderSpark() {
  const nodes = ensureBars();
  if (!nodes) return;
  const values = samples.slice(-SPARK_BARS);
  const peak = Math.max(...values, 0.001);
  const pad = SPARK_BARS - values.length;
  nodes.forEach((bar, index) => {
    const position = index - pad;
    const value = position >= 0 ? values[position] : null;
    if (value === null || value === undefined) {
      bar.style.height = "2px";
      bar.classList.remove("current");
      return;
    }
    bar.style.height = `${Math.max(12, Math.round((value / peak) * 100))}%`;
    bar.classList.toggle("current", position === values.length - 1);
  });
}

function tickClock() {
  text("systemClock", new Date().toLocaleTimeString("zh-CN", { hour12: false }));
}

function clockText(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function cardState(status) {
  if (status === "online") return "online";
  if (status === "login") return "login";
  if (status === "error" || status === "stopped") return "error";
  return "starting";
}

function setFootLeft(value, title) {
  const node = $("systemPool");
  if (!node) return;
  node.textContent = value;
  node.title = title || value;
}

export function updateSystemCard(payload) {
  const card = $("systemCard");
  if (!card) return;
  const session = payload?.session || {};
  const counts = payload?.counts || {};
  const backfill = payload?.backfill || {};
  const status = String(session.status || "starting");
  const message = String(session.message || "");
  const agents = Array.isArray(session.agents) ? session.agents : [];
  const cycle = Number(session.last_cycle_seconds);

  card.dataset.state = cardState(status);
  card.title = message || "";

  text("systemSessions", status === "online" ? "1" : "0");
  text("systemLive", counts.live === undefined || counts.live === null ? "—" : String(counts.live));
  text(
    "systemLatency",
    Number.isFinite(cycle) && cycle > 0 ? `${cycle.toFixed(2)}s` : "—",
  );

  if (status === "online" && Number.isFinite(cycle) && cycle > 0) {
    samples.push(cycle);
    if (samples.length > MAX_SAMPLES) samples.shift();
    renderSpark();
  }

  if (status === "online") {
    setFootLeft(`未结算 ${Number(counts.open || 0)} · 已结算 ${Number(counts.settled || 0)}`);
  } else {
    // Keep the operator guidance that used to live in the full-width status bar.
    setFootLeft(message || "等待采集会话…", message);
  }

  const parts = [];
  if (agents.length) parts.push(`代理 ${agents.length}`);
  const synced = clockText(session.last_open_at);
  if (synced) parts.push(`更新 ${synced}`);
  if (backfill.running) {
    parts.push(`补历史 ${Number(backfill.done || 0)}/${Number(backfill.total || 0)}`);
  }
  text("systemSync", parts.join(" · ") || "等待同步");
}

export function setSystemCardError(message) {
  const card = $("systemCard");
  if (!card) return;
  card.dataset.state = "error";
  card.title = message || "";
  text("systemSessions", "0");
  text("systemSync", message || "无法连接");
}

window.setInterval(tickClock, 1000);
tickClock();
ensureBars();
