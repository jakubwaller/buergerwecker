// Start-up, the frame (header, tab bar, banner) and what a notification does.
import { t, setLang, getLang, detectLang } from "./i18n.js";
import { configure, isUnavailable } from "./api.js";
import { h, banner, toast, errorMessage } from "./ui.js";
import { plugin, openExternal } from "./native.js";
import * as push from "./push.js";
import * as store from "./store.js";
import {
  state, TABS, onRender, render, currentTab, stack, top, back, switchTab,
  refreshSubs, forgetCaches,
} from "./state.js";
import * as onboarding from "./screens/onboarding.js";
import * as unavailable from "./screens/unavailable.js";
import * as verifyScreen from "./screens/verify.js";
import * as cities from "./screens/cities.js";
import * as city from "./screens/city.js";
import * as form from "./screens/form.js";
import * as subs from "./screens/subs.js";
import * as settings from "./screens/settings.js";

const SCREENS = { cities, city, form, subs, settings };
const TAB_ICONS = { cities: "⌂", subs: "⏰", settings: "⚙︎" };

let ready = false;
const pending = []; // notifications that arrived before start-up finished

function frame() {
  const root = document.getElementById("app");
  const main = h("main", { id: "screen" });
  if (state.unavailable) {
    unavailable.mount(main, { retry: () => start() });
    root.replaceChildren(main);
    return;
  }
  if (state.onboarding) {
    onboarding.mount(main);
    root.replaceChildren(main);
    return;
  }
  // Registered, but the server has not seen a push reach this phone yet:
  // nothing else works until it has, so nothing else is shown.
  if (push.awaitingVerification()) {
    verifyScreen.mount(main);
    root.replaceChildren(main);
    return;
  }
  const entry = top();
  const screen = SCREENS[entry.name] ?? cities;
  const titleText = screen.title?.(entry.params ?? {}) ?? "";
  const header = h(
    "header",
    { class: "bar" },
    stack().length > 1
      ? h("button", { class: "back", onclick: () => back(), "aria-label": t("nav.back") }, "‹ ", t("nav.back"))
      : h("span", { class: "brand" }, t("app.name")),
    h("span", { class: "bar-title" }, stack().length > 1 ? titleText : ""),
  );
  screen.mount(main, entry.params ?? {});
  if (stack().length === 1 && titleText) main.prepend(h("h1", null, titleText));
  const tabs = h(
    "nav",
    { class: "tabs", "aria-label": "Tabs" },
    TABS.map((tab) =>
      h(
        "button",
        {
          class: tab === currentTab() ? "active" : "",
          "aria-current": tab === currentTab() ? "page" : null,
          onclick: () => {
            if (tab === currentTab()) switchTab(tab, [{ name: tab }]);
            else switchTab(tab);
          },
        },
        h("span", { class: "tab-icon", "aria-hidden": "true" }, TAB_ICONS[tab]),
        h("span", null, t(`tab.${tab}`)),
      ),
    ),
  );
  root.replaceChildren(header, main, tabs);
  main.scrollTop = 0;
}

// What tapping a notification (or its in-app banner) does. The app never
// books: a slots push opens the city's booking page in the system browser.
function act(n) {
  const data = n?.data ?? {};
  if (data.type === "slots") {
    if (data.url) openExternal(data.url);
    if (data.city) switchTab("cities", [{ name: "cities" }, { name: "city", params: { slug: data.city } }]);
  } else if (data.type === "checkin") {
    state.checkin = Number(data.sub) || null;
    switchTab("subs", [{ name: "subs" }]);
  }
}

// The setup push. Its code goes back to the server whether the push was
// tapped or arrived in the foreground; a code already used still answers
// 200, so a second delivery does no harm.
async function handleVerify(data) {
  try {
    const wasWaiting = push.awaitingVerification();
    if ((await push.verify(data.code)) && wasWaiting) toast(t("verify.done"));
  } catch (e) {
    if (e?.error === "invalid_code") {
      verifyScreen.noteInvalidCode();
      render();
    } else toast(errorMessage(e));
  }
}

function onNotification(n, tapped) {
  if (!ready) {
    pending.push([n, tapped]);
    return;
  }
  const data = n?.data ?? {};
  if (data.type === "verify") return handleVerify(data);
  if (tapped) act(n);
  else banner({ title: n.title, body: n.body, onTap: () => act(n) });
}

let errorShown = false;
let knownDeviceId = null;
let knownVerified = null;

async function start() {
  ready = false;
  state.unavailable = false;
  forgetCaches();
  const perm = await push.permission().catch(() => "unsupported");
  state.permission = perm;
  state.onboarding = !(await store.get("onboarded"));
  render();
  if (!state.onboarding && perm === "granted") push.register().catch(() => {});
  ready = true;
  for (const [n, tapped] of pending.splice(0)) onNotification(n, tapped);
  if (!state.onboarding) await refreshSubs();
}

async function boot() {
  setLang((await store.get("lang")) ?? detectLang(globalThis.navigator?.language));
  document.documentElement.lang = getLang();
  configure({
    getCredentials: push.credentials,
    unavailable: () => {
      if (state.unavailable) return;
      state.unavailable = true;
      render();
    },
  });
  const stored = await push.loadDevice();
  knownDeviceId = stored?.id ?? null;
  knownVerified = stored?.verified ?? null;
  push.init({
    getLang,
    channelName: t("channel.name"),
    channelDescription: t("channel.description"),
    changed: (d) => {
      // A device registered (or re-registered under a new id): its list may
      // differ. Verification flipping shows or lifts the waiting screen, and
      // once verified the list can load. A token change alone needs nothing.
      const id = d?.id ?? null;
      const verified = d?.verified ?? null;
      const newId = id !== knownDeviceId;
      const flipped = verified !== knownVerified;
      knownDeviceId = id;
      knownVerified = verified;
      if (!ready) return;
      if (flipped) render();
      if (id && (newId || (flipped && verified !== false))) refreshSubs();
    },
    error: (e) => {
      // Shown once per launch; the next launch tries again by itself. The
      // closed API gate has its own screen and needs no second word.
      if (errorShown || isUnavailable(e)) return;
      errorShown = true;
      toast(t("push.registerFailed", { reason: errorMessage(e) }));
    },
    received: (n) => onNotification(n, false),
    tapped: (n) => onNotification(n, true),
  });

  const App = plugin("App");
  App?.addListener("appStateChange", async ({ isActive }) => {
    if (!isActive || !ready || state.onboarding) return;
    const before = state.permission;
    state.permission = await push.permission().catch(() => before);
    if (state.permission === "granted" && (before !== "granted" || !push.getDevice())) push.register().catch(() => {});
    if (before !== state.permission) render();
    refreshSubs();
  });
  App?.addListener("backButton", () => {
    if (!back()) App.minimizeApp?.();
  });

  onRender(frame);
  await start();
}

boot().catch((e) => {
  const root = document.getElementById("app");
  root.replaceChildren(h("main", { id: "screen" }, h("p", { class: "notice notice-error" }, errorMessage(e))));
});

