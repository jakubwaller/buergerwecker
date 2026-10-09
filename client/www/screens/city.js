// One office: per service the earliest free slot and the soonest few after
// it, from the server's last poll. The snapshot is what buergerwecker.de
// already fetched for its subscribers, and it shows it only to them: the
// slots route takes the device credential and answers 403 not_subscribed to a
// device without a live alert in this city, and leaves out a special-category
// service the device does not itself watch. Opening this screen asks the city
// nothing. Booking happens on the city's own page, in the system browser.
import { t, getLang } from "../i18n.js";
import { h, fill, loading, errorBox, errorMessage } from "../ui.js";
import { isApiError } from "../api.js";
import { openExternal } from "../native.js";
import { formatInstant, formatSlot, slotPreview } from "../format.js";
import { cityDetail, citySlots, go } from "../state.js";

export const title = () => "";

// What the overview says where the snapshot did not come:
//  "subscribe"  the server shows a city's slots only to a device with an alert
//               there (403 not_subscribed), and a phone that never registered
//               has no credential to ask with (401): set up an alert, then;
//  "error"      anything else worth passing on, a 429 on the route's
//               per-device budget say, in the server's own words;
//  null         nothing to say: no error, or a server without the route.
export function slotsNotice(err) {
  if (!err) return null;
  if (isApiError(err) && (err.error === "not_subscribed" || err.status === 401)) return "subscribe";
  if (isApiError(err) && err.status === 404) return null;
  return "error";
}

// The service the screen was opened for (from an alert or a notification)
// first, then those with a free slot (soonest first), then those someone is
// watching, then the rest in catalog order.
export function orderServices(services, snapshot, focus = null) {
  const byId = new Map((snapshot?.services ?? []).map((s) => [s.id, s]));
  const rank = (svc) => {
    if (focus && svc.id === focus) return -1;
    const snap = byId.get(svc.id);
    if (snap?.earliest) return 0;
    if (snap) return 1;
    return 2;
  };
  const when = (svc) => {
    const e = byId.get(svc.id)?.earliest;
    return e ? `${e.date} ${e.time ?? ""}` : "";
  };
  return services
    .map((svc, i) => ({ svc, i, snap: byId.get(svc.id) ?? null }))
    .sort((a, b) => rank(a.svc) - rank(b.svc) || when(a.svc).localeCompare(when(b.svc)) || a.i - b.i);
}

export function mount(el, params) {
  const body = h("div", null, loading());
  el.append(body);
  const load = () => {
    body.replaceChildren(loading());
    const lang = getLang();
    Promise.all([
      cityDetail(params.slug),
      // The snapshot is optional: no alert here, a server without it, or a
      // hiccup still leaves the services and the booking button.
      citySlots(params.slug).then(
        (snapshot) => ({ snapshot, error: null }),
        (error) => ({ snapshot: null, error }),
      ),
    ])
      .then(([detail, slots]) => show(body, detail, slots, lang, params.service ?? null))
      .catch((e) => body.replaceChildren(errorBox(e, load)));
  };
  load();
}

function slotLine(slot, lang) {
  return h(
    "li",
    null,
    h("span", { class: "when" }, formatSlot(slot, lang)),
    slot.location_name ? h("span", { class: "where" }, slot.location_name) : null,
  );
}

function show(body, detail, { snapshot, error }, lang, focus) {
  const book = h(
    "button",
    { class: "btn btn-primary", onclick: () => openExternal(detail.booking_url) },
    t("city.book"),
  );
  const asOf = formatInstant(snapshot?.polled_at, lang);
  const notice = slotsNotice(error);
  const cards = orderServices(detail.services ?? [], snapshot, focus).map(({ svc, snap }) => {
    const watch = h(
      "button",
      { class: "btn btn-secondary", onclick: () => go("form", { slug: detail.slug, service: svc.id }) },
      t("city.watch"),
    );
    let content;
    if (!snapshot) {
      // No snapshot at all: nothing is known about any service, so no card
      // claims anything.
      content = null;
    } else if (!snap) {
      // A special-category service is missing from the snapshot whenever this
      // device does not watch it itself, watched or not by others.
      content = svc.sensitive ? null : h("p", { class: "muted" }, t("city.unwatched"));
    } else {
      const { earliest, next, more } = slotPreview(snap);
      const svcAsOf = formatInstant(snap.polled_at, lang);
      content = h(
        "div",
        null,
        earliest
          ? h(
              "div",
              { class: "earliest" },
              h("span", { class: "label" }, t("city.earliest")),
              h("ul", { class: "slots" }, slotLine(earliest, lang)),
            )
          : h("p", { class: "muted" }, t("city.noneFree")),
        next.length ? h("ul", { class: "slots" }, next.map((s) => slotLine(s, lang))) : null,
        more ? h("p", { class: "muted small" }, t("city.more", { n: more })) : null,
        svcAsOf && svcAsOf !== asOf ? h("p", { class: "muted small" }, t("city.asOf", { time: svcAsOf })) : null,
      );
    }
    return h("section", { class: `card service${svc.id === focus ? " highlight" : ""}` }, h("h3", null, svc.name), content, watch);
  });
  fill(
    body,
    h("h1", null, detail.label || detail.city),
    h("p", { class: "disclaimer" }, t("city.disclaimer", { city: detail.city })),
    detail.note ? h("p", { class: "disclaimer" }, detail.note) : null,
    h("div", { class: "stack" }, book, h("p", { class: "muted small" }, t("city.bookHint"))),
    h("h2", null, t("city.services")),
    notice === "subscribe" ? h("div", { class: "notice" }, h("p", null, t("city.subscribeHint"))) : null,
    notice === "error" ? h("p", { class: "muted small" }, errorMessage(error)) : null,
    asOf ? h("p", { class: "muted small" }, t("city.asOf", { time: asOf })) : null,
    ...cards,
  );
}
