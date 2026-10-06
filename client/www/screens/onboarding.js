// First launch: what the app does, then the notification permission prompt.
import { t } from "../i18n.js";
import { h } from "../ui.js";
import * as push from "../push.js";
import * as store from "../store.js";
import { state, render, refreshSubs } from "../state.js";

async function finish(ask) {
  if (ask) {
    try {
      state.permission = await push.requestPermission();
    } catch {
      state.permission = "denied";
    }
    if (state.permission === "granted") push.register().catch(() => {});
  }
  await store.set("onboarded", true);
  state.onboarding = false;
  render();
  refreshSubs();
}

export function mount(el) {
  const allow = h("button", { class: "btn btn-primary", onclick: () => busy(() => finish(true)) }, t("onboarding.allow"));
  const browse = h("button", { class: "btn btn-link", onclick: () => busy(() => finish(false)) }, t("onboarding.browse"));
  const busy = async (fn) => {
    allow.disabled = browse.disabled = true;
    try {
      await fn();
    } finally {
      allow.disabled = browse.disabled = false;
    }
  };
  el.append(
    h(
      "div",
      { class: "onboarding" },
      h("div", { class: "glyph", "aria-hidden": "true" }),
      h("h1", null, t("onboarding.title")),
      h("p", null, t("onboarding.p1")),
      h("p", null, t("onboarding.p2")),
      h("div", { class: "stack" }, allow, browse),
    ),
  );
}
