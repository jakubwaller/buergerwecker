// My subscriptions: what each one watches, until when, and the three things
// to do with one (keep looking, edit, stop). Loaded on launch and on resume
// only (state.refreshSubs), never on a timer.
import { t, getLang } from "../i18n.js";
import { h, loading, errorBox, errorMessage, toast } from "../ui.js";
import { api } from "../api.js";
import { filterSummary, formatInstantDay } from "../format.js";
import * as push from "../push.js";
import { state, cityDetail, go, upsertSub, dropSub, render, refreshSubs } from "../state.js";
import { permissionHint } from "./common.js";

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

function card(sub, detail) {
  const lang = getLang();
  const svc = detail?.services?.find((s) => s.id === sub.appointment_type);
  const locNames = Array.isArray(sub.locations)
    ? sub.locations.map((id) => detail?.locations?.find((l) => l.id === id)?.name).filter(Boolean)
    : [];
  const expired = !sub.active;
  const when = expired
    ? t("subs.expired", { date: formatInstantDay(sub.expires_at, lang) ?? "" })
    : t("subs.runsUntil", { date: formatInstantDay(sub.expires_at, lang) ?? "" });

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
  const list = h("div", null, ordered.map((s) => card(s, null)));
  el.append(list);
  // Names come from the catalog, a request per city at most (cached).
  const slugs = [...new Set(subs.map((s) => s.city))];
  Promise.all(slugs.map((slug) => cityDetail(slug).then((d) => [slug, d]).catch(() => [slug, null]))).then((pairs) => {
    const bySlug = new Map(pairs);
    if (!list.isConnected) return;
    list.replaceChildren(...ordered.map((s) => card(s, bySlug.get(s.city))));
    if (state.checkin) document.getElementById(`sub-${state.checkin}`)?.scrollIntoView({ block: "center" });
  });
}
