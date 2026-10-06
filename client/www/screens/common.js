// Pieces more than one screen shows.
import { t } from "../i18n.js";
import { h } from "../ui.js";
import { openAppSettings } from "../native.js";
import * as push from "../push.js";
import { state, render } from "../state.js";

// The persistent hint while alerts cannot work: notifications denied, not
// asked yet, or a build without push at all.
export function permissionHint() {
  if (state.permission === "granted") return null;
  if (state.permission === "unsupported") return h("div", { class: "notice" }, h("p", null, t("perm.unsupported")));
  if (state.permission === "denied") {
    return h(
      "div",
      { class: "notice" },
      h("p", null, t("perm.denied")),
      h("button", { class: "btn btn-secondary", onclick: () => openAppSettings() }, t("perm.openSettings")),
    );
  }
  return h(
    "div",
    { class: "notice" },
    h("p", null, t("perm.prompt")),
    h(
      "button",
      {
        class: "btn btn-secondary",
        onclick: async () => {
          state.permission = await push.requestPermission().catch(() => "denied");
          if (state.permission === "granted") push.register().catch(() => {});
          render();
        },
      },
      t("perm.allow"),
    ),
  );
}
