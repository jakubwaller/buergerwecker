// The one screen while the server's APP_API_ENABLED gate is closed: every
// /api/v1 route answers 404 not_available, and a stack of error boxes would
// say nothing useful.
import { t, getLang } from "../i18n.js";
import { h } from "../ui.js";
import { openExternal } from "../native.js";
import { SITE_URL } from "../api.js";
import { state, render, forgetCaches } from "../state.js";

export function mount(el, { retry } = {}) {
  el.append(
    h(
      "div",
      { class: "onboarding" },
      h("div", { class: "glyph", "aria-hidden": "true" }),
      h("h1", null, t("unavailable.title")),
      h("p", null, t("unavailable.body")),
      h(
        "div",
        { class: "stack" },
        h(
          "button",
          { class: "btn btn-primary", onclick: () => openExternal(getLang() === "en" ? `${SITE_URL}/?lang=en` : SITE_URL) },
          t("unavailable.open"),
        ),
        h(
          "button",
          {
            class: "btn btn-link",
            onclick: () => {
              state.unavailable = false;
              forgetCaches();
              render();
              retry?.();
            },
          },
          t("unavailable.retry"),
        ),
      ),
    ),
  );
}
