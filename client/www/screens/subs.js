// My subscriptions: what each one watches, until when, the soonest free slot
// it would notify about right now, and what to do with one (book, see the
// city's slots, keep looking, edit, stop). The list loads on launch and on
// resume only (state.refreshSubs), never on a timer; the slots come from the
// same snapshot as the city overview, one read per city (state.citySlots).
import { t, getLang } from "../i18n.js";
import { h, loading, errorBox, errorMessage, toast } from "../ui.js";
import { api } from "../api.js";
import { openExternal } from "../native.js";
import { filterSummary, formatInstantDay, formatSlot } from "../format.js";
import { matchingSlots } from "../filters.js";
import * as push from "../push.js";
import { state, cityDetail, citySlots, go, upsertSub, dropSub, render, refreshSubs } from "../state.js";
import { permissionHint } from "./common.js";

// Slots shown under an alert: the soonest and this many more.
const NEXT = 2;

export const title = () => t("subs.title");

async function act(button, fn) {
  button.disabled = true;
  try {
    await fn();
  } catch (err) {
    toast(errorMessage(err));
  } finally {
    button.disabled = false;
  }
}

const renew = (sub) => async () => {
  const fresh = await push.authed(() => api.renewSubscription(sub.id));
  upsertSub(fresh);
  if (state.checkin === sub.id) state.checkin = null;
  toast(t("subs.renewed", { date: formatInstantDay(fresh?.expires_at, getLang()) ?? "" }));
  render();
};

const stop = (sub, ask = true) => async () => {
  if (ask && !globalThis.confirm(t("subs.confirmStop"))) return;
  try {
    await push.authed(() => api.deleteSubscription(sub.id));
  } catch (err) {
    if (err?.status !== 404 || err?.error === "not_available") throw err;
  }
  dropSub(sub.id);
  if (state.checkin === sub.id) state.checkin = null;
  toast(t("subs.stopped"));
  render();
};

function slotLine(slot, lang, cls = null) {
  return h(
    "li",
    { class: cls },
    h("span", { class: "when" }, formatSlot(slot, lang)),
    slot.location_name ? h("span", { class: "where" }, slot.location_name) : null,
  );
}

// What this alert would notify about from the city's snapshot, or null while
// the snapshot is not there (not loaded yet, or the read failed).
function matchesOf(sub, snapshot) {
  if (!snapshot) return null;
  return matchingSlots(sub, (snapshot.services ?? []).find((s) => s.id === sub.appointment_type));
}

// The alert's soonest matching slot and the next few, as one row that opens
// the city overview at this service. Without a snapshot the row still offers
// the overview, which says why there are no slots.
function slotsRow(sub, match, lang) {
  let content = h("span", null, t("subs.showSlots"));
  if (match?.slots.length) {
    const [first, ...rest] = match.slots;
    const next = rest.slice(0, NEXT);
    const more = rest.length - next.length;
    content = [
      h("span", { class: "label" }, t("subs.earliest")),
      h("ul", { class: "slots" }, slotLine(first, lang, "earliest"), next.map((s) => slotLine(s, lang))),
      more > 0 || !match.complete
        ? h("span", { class: "muted small" }, match.complete ? t("city.more", { n: more }) : t("subs.showSlots"))
        : null,
    ];
  } else if (match?.complete) {
    content = [h("span", { class: "muted" }, t("subs.noneMatching")), h("span", { class: "small" }, t("subs.showSlots"))];
  }
  return h(
    "button",
    { class: "row slots-row", onclick: () => go("city", { slug: sub.city, service: sub.appointment_type }) },
    h("span", { class: "slots-body" }, content),
    h("span", { class: "chev", "aria-hidden": "true" }, "›"),
  );
}

function card(sub, detail, snapshot) {
  const lang = getLang();
  const svc = detail?.services?.find((s) => s.id === sub.appointment_type);
  const locNames = Array.isArray(sub.locations)
    ? sub.locations.map((id) => detail?.locations?.find((l) => l.id === id)?.name).filter(Boolean)
    : [];
  const expired = !sub.active;
  const when = expired
    ? t("subs.expired", { date: formatInstantDay(sub.expires_at, lang) ?? "" })
    : t("subs.runsUntil", { date: formatInstantDay(sub.expires_at, lang) ?? "" });
  const match = expired ? null : matchesOf(sub, snapshot);

  const buttons = [];
  if (state.checkin === sub.id) {
    const yes = h("button", { class: "btn btn-primary" }, t("subs.checkinYes"));
    yes.onclick = () => act(yes, renew(sub));
    const no = h("button", { class: "btn btn-secondary" }, t("subs.checkinNo"));
    no.onclick = () => act(no, stop(sub, false));
    buttons.push(h("p", { class: "checkin-q" }, t("subs.checkinQ")), yes, no);
  } else {
    if (expired) {
      const keep = h("button", { class: "btn btn-primary" }, t("subs.keepLooking"));
      keep.onclick = () => act(keep, renew(sub));
      buttons.push(keep);
    } else if (match?.slots.length && detail?.booking_url) {
      // The app never books: this opens the city's own page, as the
      // notification's tap does.
      buttons.push(h("button", { class: "btn btn-primary", onclick: () => openExternal(detail.booking_url) }, t("city.book")));
    }
    buttons.push(h("button", { class: "btn btn-secondary", onclick: () => go("form", { sub }) }, t("subs.edit")));
    const end = h("button", { class: "btn btn-quiet" }, t("subs.stop"));
    end.onclick = () => act(end, stop(sub));
    buttons.push(end);
  }

  return h(
    "section",
    { class: `card sub${expired ? " inactive" : ""}${state.checkin === sub.id ? " highlight" : ""}`, id: `sub-${sub.id}` },
    h("h3", null, svc?.name ?? t("subs.unknownService")),
    h("p", { class: "muted" }, detail?.label ?? sub.city),
    locNames.length ? h("p", { class: "small" }, locNames.join(", ")) : null,
    h("p", { class: "small" }, filterSummary(sub, lang)),
    h("p", { class: `small status${expired ? " warn" : ""}` }, when),
    expired ? null : slotsRow(sub, match, lang),
    h("div", { class: "actions" }, buttons),
  );
}

export function mount(el) {
  el.append(permissionHint() ?? "");
  if (state.subs === null && state.subsLoading) return el.append(loading());
  if (state.subsError && !state.subs?.length) return el.append(errorBox(state.subsError, () => refreshSubs()));
  const subs = state.subs ?? [];
  if (!subs.length) return el.append(h("p", { class: "muted center" }, t("subs.empty")));
  // The check-in's subscription first.
  const ordered = [...subs].sort((a, b) => (b.id === state.checkin) - (a.id === state.checkin));
  const list = h("div", null, ordered.map((s) => card(s, null, null)));
  el.append(list);
  // Names come from the catalog, a request per city at most (cached). Slots
  // only for a city with a live alert: the server shows them to no one else
  // (403 not_subscribed). Whichever arrives second redraws the list once more.
  let bySlug = null;
  let snaps = new Map();
  const show = () => {
    if (!bySlug || !list.isConnected) return;
    list.replaceChildren(...ordered.map((s) => card(s, bySlug.get(s.city), snaps.get(s.city))));
  };
  const slugs = [...new Set(subs.map((s) => s.city))];
  Promise.all(slugs.map((slug) => cityDetail(slug).then((d) => [slug, d]).catch(() => [slug, null]))).then((pairs) => {
    bySlug = new Map(pairs);
    show();
    if (state.checkin) document.getElementById(`sub-${state.checkin}`)?.scrollIntoView({ block: "center" });
  });
  const live = [...new Set(subs.filter((s) => s.active).map((s) => s.city))];
  Promise.all(live.map((slug) => citySlots(slug).then((r) => [slug, r]).catch(() => [slug, null]))).then((pairs) => {
    snaps = new Map(pairs);
    show();
  });
}
