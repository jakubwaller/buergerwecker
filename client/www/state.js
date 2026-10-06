// App-wide state: navigation, the subscription list, the catalog cache and
// the few facts every screen may need (permission, language, availability).
import { api } from "./api.js";
import { getLang } from "./i18n.js";
import * as push from "./push.js";
import * as widget from "./widget.js";

export const state = {
  permission: "prompt", // granted | denied | prompt | unsupported
  subs: null, // null until loaded, then the server's list
  subsError: null,
  subsLoading: false,
  checkin: null, // a subscription id to ask "still looking?" about
  unavailable: false, // the server's APP_API_ENABLED gate is closed
  onboarding: false,
};

// --- Navigation: three tabs, each with its own stack -----------------------

export const TABS = ["cities", "subs", "settings"];
const nav = {
  tab: "cities",
  stacks: { cities: [{ name: "cities" }], subs: [{ name: "subs" }], settings: [{ name: "settings" }] },
};

let renderHook = () => {};
export function onRender(fn) {
  renderHook = fn;
}
export const render = () => renderHook();

export const currentTab = () => nav.tab;
export const stack = () => nav.stacks[nav.tab];
export const top = () => stack()[stack().length - 1];

export function go(name, params = {}) {
  stack().push({ name, params });
  render();
}

export function back() {
  if (stack().length <= 1) return false;
  stack().pop();
  render();
  return true;
}

export function switchTab(tab, entries) {
  nav.tab = tab;
  if (entries) nav.stacks[tab] = entries;
  render();
}

export function resetNav() {
  nav.tab = "cities";
  for (const t of TABS) nav.stacks[t] = [{ name: t }];
}

// Re-render only screens that show `what`; a form being filled in is left
// alone so nothing typed is lost.
export function changed(what) {
  const name = top().name;
  if (name === "form") return;
  if (what === "subs" && name !== "subs") return;
  render();
}

// --- Catalog cache (per session and language) ------------------------------

const catalogCache = new Map();

export function cityDetail(slug) {
  const key = `${slug}|${getLang()}`;
  if (!catalogCache.has(key)) {
    const p = api.city(slug, getLang()).catch((e) => {
      catalogCache.delete(key);
      throw e;
    });
    catalogCache.set(key, p);
  }
  return catalogCache.get(key);
}

let citiesCache = new Map();
export function cities() {
  const lang = getLang();
  if (!citiesCache.has(lang)) {
    const p = api.cities(lang).then((r) => r.cities ?? []).catch((e) => {
      citiesCache.delete(lang);
      throw e;
    });
    citiesCache.set(lang, p);
  }
  return citiesCache.get(lang);
}

export function forgetCaches() {
  catalogCache.clear();
  citiesCache = new Map();
}

// --- Subscriptions ---------------------------------------------------------

// Launch, resume, and after verification: never on a timer. GET /device
// comes first each time, for `verified`.
let inflight = null;
// Calls that overlap (resume and a verification landing together) share one
// round trip.
export function refreshSubs() {
  if (!inflight) inflight = doRefresh().finally(() => (inflight = null));
  return inflight;
}

async function doRefresh() {
  if (!push.getDevice()) {
    state.subs = [];
    state.subsError = null;
    changed("subs");
    return;
  }
  state.subsLoading = true;
  try {
    await push.refreshStatus().catch(() => {});
    // Until the setup push has arrived the list is 403; the waiting screen
    // is up instead, and the list loads once the device is verified.
    if (push.awaitingVerification()) return;
    const r = await push.authed(() => api.subscriptions());
    state.subs = r?.subscriptions ?? [];
    state.subsError = null;
    syncWidget();
  } catch (e) {
    state.subsError = e;
  } finally {
    state.subsLoading = false;
  }
  changed("subs");
}

// Tells the home-screen widget which cities matter now. Coalesced: a burst
// of changes (the list loading, then an edit) is one write of the latest
// state. Never throws, never waits: the widget is a convenience.
let widgetPending = false;
export function syncWidget() {
  if (widgetPending) return;
  widgetPending = true;
  Promise.resolve()
    .then(async () => {
      widgetPending = false;
      if (state.subs == null) return;
      const list = await cities().catch(() => []);
      await widget.sync(state.subs, list, getLang());
    })
    .catch(() => {});
}

// Puts a subscription the server just returned into the list.
export function upsertSub(sub) {
  if (!sub) return;
  const list = state.subs ? [...state.subs] : [];
  const i = list.findIndex((s) => s.id === sub.id);
  if (i >= 0) list[i] = sub;
  else list.unshift(sub);
  state.subs = list;
  syncWidget();
}

export function dropSub(id) {
  state.subs = (state.subs || []).filter((s) => s.id !== id);
  syncWidget();
}
