const _now = new Date(Date.now() - 240 * 60_000);
const _pad = (value) => String(value).padStart(2, "0");
const TODAY = `${_now.getUTCFullYear()}-${_pad(_now.getUTCMonth() + 1)}-${_pad(_now.getUTCDate())}`;
const YESTERDAY = new Date(Date.parse(`${TODAY}T00:00:00Z`) - 86_400_000).toISOString().slice(0, 10);

export const state = {
  currentPage: "my-events",
  toastTimer: null,
  myEvents: {
    status: "ALL",
    agent: "",
    search: "",
    from: YESTERDAY,
    to: TODAY,
    basis: "created",
    hours: 24,
    tz: -240,
    view: "orders",
    rows: [],
    agents: [],
    total: 0,
    offset: 0,
    loading: false,
    ready: null,
    lastCursor: null,
    banner: [],
    bannerTimer: null,
    fixtures: [],
    fixtureTotal: 0,
    fixtureOffset: 0,
    fixtureLoading: false,
    fixtureLastRefreshAt: 0,
    fixtureOpenKey: "",
    fixtureOrders: new Map(),
    copy: [],
    copyTotal: 0,
    copyOffset: 0,
    copyLoading: false,
    copyLastRefreshAt: 0,
  },
};
