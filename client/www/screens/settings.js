// Language, delete my data, the site's legal pages, the version.
import { t, getLang, setLang, LANGS } from "../i18n.js";
import { h, toast, errorMessage } from "../ui.js";
import { openExternal, plugin } from "../native.js";
import { SITE_URL } from "../api.js";
import * as push from "../push.js";
import * as store from "../store.js";
import * as widget from "../widget.js";
import * as inbox from "../inbox.js";
import { state, render, forgetCaches, resetNav, syncWidget } from "../state.js";

export const title = () => t("settings.title");

const LANG_NAMES = { de: "Deutsch", en: "English" };

const page = (path) => () => openExternal(`${SITE_URL}${path}${getLang() === "en" ? "?lang=en" : ""}`);

export function mount(el) {
  const langs = h(
    "div",
    { class: "segmented", role: "group", "aria-label": t("settings.language") },
    LANGS.map((l) =>
      h(
        "button",
        {
          class: l === getLang() ? "active" : "",
          "aria-pressed": l === getLang() ? "true" : "false",
          onclick: async () => {
            if (l === getLang()) return;
            setLang(l);
            document.documentElement.lang = l;
            await store.set("lang", l);
            forgetCaches();
            render();
            // The server writes pushes in the device's language.
            push.updateLanguage(l).catch(() => {});
            // The widget's words and dates are in the app's language too.
            syncWidget();
          },
        },
        LANG_NAMES[l],
      ),
    ),
  );

  const del = h("button", { class: "btn btn-danger" }, t("settings.delete"));
  del.onclick = async () => {
    if (!globalThis.confirm(t("settings.confirmDelete"))) return;
    del.disabled = true;
    try {
      await push.deleteEverything();
      state.subs = null;
      inbox.reset();
      widget.clear().catch(() => {});
      state.checkin = null;
      forgetCaches();
      resetNav();
      toast(t("settings.deleted"));
      state.onboarding = true;
      render();
    } catch (err) {
      toast(errorMessage(err));
    } finally {
      del.disabled = false;
    }
  };

  const version = h("p", { class: "muted small center" });
  plugin("App")
    ?.getInfo()
    .then((i) => (version.textContent = t("settings.version", { v: `${i.version} (${i.build})` })))
    .catch(() => {});

  el.append(
    h("section", { class: "card" }, h("h2", null, t("settings.language")), langs),
    h(
      "section",
      { class: "card" },
      h("button", { class: "row", onclick: page("/datenschutz") }, h("span", null, t("settings.privacy")), h("span", { class: "chev" }, "↗")),
      h("button", { class: "row", onclick: page("/impressum") }, h("span", null, t("settings.imprint")), h("span", { class: "chev" }, "↗")),
      h("button", { class: "row", onclick: page("/kontakt") }, h("span", null, t("settings.contact")), h("span", { class: "chev" }, "↗")),
    ),
    h("section", { class: "card" }, h("p", { class: "muted small" }, t("settings.deleteHint")), del),
    h("p", { class: "muted small center" }, t("settings.noBooking")),
    version,
  );
}
