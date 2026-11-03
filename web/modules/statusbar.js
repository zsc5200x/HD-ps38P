/** 公共底栏：系统状态、系统时间、运行时长、版本号与数据同步。 */

const $ = (id) => document.getElementById(id);

const HEALTH = {
  starting: ["系统启动中", "warn"],
  login: ["系统等待登录", "warn"],
  online: ["系统安全运行中", "ok"],
  error: ["系统运行异常", "error"],
  stopped: ["系统已停止", "error"],
};

let bound = false;
let uptimeBase = null;
let uptimeAt = 0;

function formatUptime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function setHealth(text, tone) {
  const node = $("statusbarHealth");
  if (!node) return;
  node.dataset.tone = tone;
  const label = node.querySelector("b");
  if (label) label.textContent = text;
}

function setSync(text, tone) {
  const node = $("statusbarSync");
  if (!node) return;
  node.dataset.tone = tone;
  const label = node.querySelector("b");
  if (label) label.textContent = text;
}

function setVersion(version) {
  const normalized = version ? `v${String(version).replace(/^v/i, "")}` : "";
  const bar = $("statusbarVersion");
  if (bar) bar.textContent = normalized || "未提供";
  const brand = $("brandVersion");
  if (brand) {
    brand.textContent = normalized || "v—";
    brand.hidden = !normalized;
  }
}

function tick() {
  const time = $("statusbarTime");
  if (time) time.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  if (uptimeBase === null) return;
  const uptime = $("statusbarUptime");
  if (uptime) uptime.textContent = formatUptime(uptimeBase + (Date.now() - uptimeAt) / 1000);
}

function apply(payload) {
  const session = payload.session || {};
  const status = String(session.status || "starting");
  const [healthText, tone] = HEALTH[status] || ["系统状态未知", "idle"];
  setHealth(healthText, tone);

  setVersion(payload.version);
  if (payload.uptimeSeconds !== undefined && payload.uptimeSeconds !== null) {
    uptimeBase = Number(payload.uptimeSeconds) || 0;
    uptimeAt = Date.now();
    tick();
  }

  const lastOpen = session.last_open_at ? new Date(session.last_open_at).getTime() : NaN;
  if (status === "online") {
    if (Number.isFinite(lastOpen) && Date.now() - lastOpen > 120_000) {
      setSync("数据同步延迟", "warn");
    } else {
      setSync("数据同步正常", "ok");
    }
  } else if (status === "login" || status === "starting") {
    setSync("等待数据同步", "warn");
  } else {
    setSync("数据同步异常", "error");
  }
}

export function bindStatusbar() {
  if (bound || !$("statusbarHealth")) return;
  bound = true;
  window.addEventListener("ps38:status", (event) => {
    const payload = event.detail;
    if (!payload || payload.ok === false) {
      setHealth("服务无法连接", "error");
      setSync("数据同步中断", "error");
      return;
    }
    apply(payload);
  });
  tick();
  window.setInterval(tick, 1000);
}
