// The notifications of the last weeks (inbox.js), newest first. Tapping one
// opens what it was about as it is now: a slots notification the city's
// overview at that alert's service, since the slots it named may be gone by
// the time it is read; a check-in the alert's question in My alerts.
import { t, getLang } from "../i18n.js";
import { h, toast } from "../ui.js";
import { formatInstant } from "../format.js";
import * as inbox from "../inbox.js";
import { state, go, switchTab, cities } from "../state.js";

export const title = () => t("inbox.title");

// What tapping an entry does. The alert, when this phone still has it, says
// which service to open the city at; a special-category notification names
// no city at all (app/push.py, render_push), so then the alert is the only
// way there.
export function open(entry) {
  const sub = (state.subs ?? []).find((s) => String(s.id) === entry.sub) ?? null;
  if (entry.type === "checkin") {
    state.checkin = sub ? sub.id : null;
    switchTab("subs", [{ name: "subs" }]);
    return;
  }
  const slug = entry.city ?? sub?.city ?? null;
  if (slug) go("city", { slug, service: sub?.city === slug ? sub.appointment_type : undefined });
  else switchTab("subs", [{ name: "subs" }]);
}

// An Android tap carries the push's data but not its text: worded here, in
// the push's own words (push.title_city, push.checkin_title).
function heading(entry, names) {
  if (entry.title) return entry.title;
  if (entry.type === "checkin") return t("inbox.checkinTitle");
  const city = entry.city ? names.get(entry.city) : null;
  return city ? t("inbox.slotsTitleCity", { city }) : t("inbox.slotsTitle");
}

function row(entry, names, lang, unread) {
  return h(
    "button",
    { class: `row inbox-row${unread ? " unread" : ""}`, onclick: () => open(entry) },
    h(
      "span",
      { class: "inbox-body" },
      h("span", { class: "muted small" }, formatInstant(new Date(entry.at).toISOString(), lang)),
      h("strong", null, heading(entry, names)),
      entry.body ? h("span", { class: "small inbox-text" }, entry.body) : null,
    ),
    h("span", { class: "chev", "aria-hidden": "true" }, "›"),
  );
}

export function mount(el) {
  const lang = getLang();
  const items = inbox.items();
  // Shown as new once: the ones that arrived since the list was last open.
  const seenAt = inbox.seenAt();
  inbox.markRead();
  if (!items.length) {
    el.append(h("p", { class: "muted center" }, t("inbox.empty")));
    return;
  }
  const list = h("section", { class: "card inbox" });
  const names = new Map();
  const draw = () => list.replaceChildren(...items.map((e) => row(e, names, lang, e.at > seenAt)));
  draw();
  const clear = h("button", { class: "btn btn-quiet" }, t("inbox.clear"));
  clear.onclick = async () => {
    await inbox.clear();
    toast(t("inbox.cleared"));
    switchTab("inbox", [{ name: "inbox" }]);
  };
  el.append(h("p", { class: "muted small" }, t("inbox.hint", { days: inbox.MAX_AGE_DAYS })), list, clear);
  // City names only for entries that came without their text.
  if (items.some((e) => !e.title && e.city)) {
    cities()
      .then((list) => {
        for (const c of list) names.set(c.slug, c.label || c.city);
        if (el.isConnected) draw();
      })
      .catch(() => {});
  }
}
