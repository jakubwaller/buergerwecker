// One office: per service the earliest free slot and the soonest few after
// it, from the server's last poll. The snapshot is what buergerwecker.de
// already fetched for its subscribers; opening this screen asks the city
// nothing. Booking happens on the city's own page, in the system browser.
import { t, getLang } from "../i18n.js";
import { h, fill, loading, errorBox } from "../ui.js";
import { api } from "../api.js";
import { openExternal } from "../native.js";
import { formatInstant, formatSlot, slotPreview } from "../format.js";
import { cityDetail, go } from "../state.js";

export const title = () => "";

// Services with a free slot first (soonest first), then those someone is
// watching, then the rest in catalog order.
export function orderServices(services, snapshot) {
  const byId = new Map((snapshot?.services ?? []).map((s) => [s.id, s]));
  const rank = (svc) => {
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
      // The snapshot is optional: a server without it, or a hiccup, still
      // leaves the services and the booking button.
      api.slots(params.slug, lang).catch(() => null),
    ])
      .then(([detail, snapshot]) => show(body, detail, snapshot, lang))
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

function show(body, detail, snapshot, lang) {
  const book = h(
    "button",
    { class: "btn btn-primary", onclick: () => openExternal(detail.booking_url) },
    t("city.book"),
  );
  const asOf = formatInstant(snapshot?.polled_at, lang);
  const cards = orderServices(detail.services ?? [], snapshot).map(({ svc, snap }) => {
    const watch = h(
      "button",
      { class: "btn btn-secondary", onclick: () => go("form", { slug: detail.slug, service: svc.id }) },
      t("city.watch"),
    );
    let content;
    if (!snap) {
      content = h("p", { class: "muted" }, t("city.unwatched"));
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
    return h("section", { class: "card service" }, h("h3", null, svc.name), content, watch);
  });
  fill(
    body,
    h("h1", null, detail.label || detail.city),
    h("p", { class: "disclaimer" }, t("city.disclaimer", { city: detail.city })),
    detail.note ? h("p", { class: "disclaimer" }, detail.note) : null,
    h("div", { class: "stack" }, book, h("p", { class: "muted small" }, t("city.bookHint"))),
    h("h2", null, t("city.services")),
    asOf ? h("p", { class: "muted small" }, t("city.asOf", { time: asOf })) : null,
    ...cards,
  );
}
