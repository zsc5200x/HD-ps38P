import { bindMergeEvents } from "./modules/merge/page.js";
import { bindMyEventsEvents } from "./modules/my-events/bindings.js";
import { pollMyEventsEvents } from "./modules/my-events/page.js";
import { bindTopNav } from "./modules/nav.js";
import { bindPoolEvents } from "./modules/pool.js";
import { refreshCollectorStatus } from "./modules/status.js";

bindTopNav();
bindMergeEvents();
bindMyEventsEvents();
bindPoolEvents();

window.setInterval(() => {
  if (!document.hidden) pollMyEventsEvents();
}, 1000);

window.setInterval(() => {
  if (!document.hidden) refreshCollectorStatus();
}, 4000);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshCollectorStatus();
    pollMyEventsEvents();
  }
});
