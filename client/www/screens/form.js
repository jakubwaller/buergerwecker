// The subscribe form, and the same form for editing: service, offices,
// weekdays, time window, how far ahead. The rules are the website's — the
// server checks every field again (app/signup.py build_filter).
import { t, getLang } from "../i18n.js";
import { h, loading, errorBox, errorMessage, toast } from "../ui.js";
import { api } from "../api.js";
import { openExternal } from "../native.js";
import * as push from "../push.js";
import { state, cityDetail, upsertSub, back, switchTab, stack, render } from "../state.js";
import { permissionHint } from "./common.js";

export const title = (params) => (params.sub ? t("form.titleEdit") : t("form.titleNew"));

const DAY_OPTIONS = [3, 7, 14, 30];

// The request body for POST /subscriptions and PUT /subscriptions/<id>.
export function buildPayload({ slug, service, allOffices, offices, weekdays, timeStart, timeEnd, maxDays, consent, lang }) {
  return {
    city: slug,
    appointment_type: service,
    locations: allOffices || !offices.length ? "all" : offices,
    weekdays: [...weekdays].map(Number).sort((a, b) => a - b),
    time_start: timeStart || "00:00",
    time_end: timeEnd || "23:59",
    max_days_ahead: maxDays ? Number(maxDays) : null,
    consent_special: !!consent,
    language: lang,
  };
}

export function mount(el, params) {
  const body = h("div", null, loading());
  el.append(body);
  const load = () => {
    body.replaceChildren(loading());
    cityDetail(params.slug ?? params.sub?.city)
      .then((detail) => show(body, detail, params))
      .catch((e) => body.replaceChildren(errorBox(e, load)));
  };
  load();
}

function show(body, detail, params) {
  const sub = params.sub ?? null;
  const services = detail.services ?? [];
  const locations = detail.locations ?? [];
  const initialService = sub?.appointment_type ?? params.service ?? services[0]?.id;

  const serviceSel = h(
    "select",
    { name: "service" },
    services.map((s) => h("option", { value: s.id, selected: s.id === initialService }, s.name)),
  );

  const subLocs = Array.isArray(sub?.locations) ? sub.locations : null;
  const all = h("input", { type: "checkbox", checked: !subLocs || !subLocs.length });
  const officeBoxes = locations.map((l) => ({
    id: l.id,
    box: h("input", { type: "checkbox", value: l.id, checked: !!subLocs?.includes(l.id) }),
    label: null,
  }));
  for (const o of officeBoxes) o.label = h("label", { class: "check" }, o.box, h("span", null, locations.find((l) => l.id === o.id).name));
  const officeList = h("fieldset", { class: "loc-list" }, h("legend", null, t("form.someOffices")), officeBoxes.map((o) => o.label));

  const days = new Set(sub?.weekdays ?? [1, 2, 3, 4, 5]);
  const dayButtons = [1, 2, 3, 4, 5, 6, 7].map((d) => {
    const b = h(
      "button",
      {
        type: "button",
        class: "day",
        "aria-pressed": days.has(d) ? "true" : "false",
        onclick: () => {
          if (days.has(d)) days.delete(d);
          else days.add(d);
          b.setAttribute("aria-pressed", days.has(d) ? "true" : "false");
        },
      },
      t(`weekday.${d}`),
    );
    return b;
  });

  const timeStart = h("input", { type: "time", value: sub?.time_start ?? "00:00", "aria-label": t("form.from") });
  const timeEnd = h("input", { type: "time", value: sub?.time_end ?? "23:59", "aria-label": t("form.to") });

  const mda = sub?.max_days_ahead ?? null;
  const maxSel = h(
    "select",
    null,
    h("option", { value: "", selected: !mda }, t("form.noLimit")),
    mda && !DAY_OPTIONS.includes(mda) ? h("option", { value: String(mda), selected: true }, t("form.nDays", { n: mda })) : null,
    DAY_OPTIONS.map((n) => h("option", { value: String(n), selected: mda === n }, t("form.nDays", { n }))),
  );

  const consentBox = h("input", { type: "checkbox", checked: !!sub?.consent_special });
  const consent = h(
    "div",
    { class: "consent-block" },
    h("strong", null, t("consent.title")),
    h("p", null, t("consent.body")),
    h("label", { class: "check" }, consentBox, h("span", null, t("consent.label"))),
    h(
      "p",
      { class: "consent-note" },
      t("consent.note", { days: detail.sensitive_ttl_days ?? 14 }),
      " ",
      h(
        "a",
        {
          href: "#",
          onclick: (e) => {
            e.preventDefault();
            openExternal(`https://buergerwecker.de/datenschutz${getLang() === "en" ? "?lang=en" : ""}`);
          },
        },
        "buergerwecker.de/datenschutz",
      ),
    ),
  );

  const errorEl = h("div", { role: "alert" });
  const submit = h("button", { type: "submit", class: "btn btn-primary" }, sub ? t("form.save") : t("form.submit"));

  // An office that does not offer the chosen service is hidden and unticked,
  // as on the website: an impossible pair would never notify.
  const apply = () => {
    const svc = services.find((s) => s.id === serviceSel.value);
    const offered = Array.isArray(svc?.locations) ? new Set(svc.locations) : null;
    for (const o of officeBoxes) {
      const show = !offered || offered.has(o.id);
      o.label.hidden = !show;
      if (!show) o.box.checked = false;
    }
    officeList.hidden = all.checked;
    consent.hidden = !svc?.sensitive;
  };
  serviceSel.addEventListener("change", apply);
  all.addEventListener("change", apply);
  apply();

  const form = h(
    "form",
    {
      class: "card form",
      onsubmit: async (e) => {
        e.preventDefault();
        errorEl.replaceChildren();
        const svc = services.find((s) => s.id === serviceSel.value);
        const offices = officeBoxes.filter((o) => o.box.checked && !o.label.hidden).map((o) => o.id);
        const fail = (text) => errorEl.replaceChildren(h("div", { class: "notice notice-error" }, h("p", null, text)));
        if (!all.checked && !offices.length) return fail(t("form.pickOffice"));
        if (!days.size) return fail(t("form.pickWeekday"));
        if (svc?.sensitive && !consentBox.checked) return fail(t("form.needsConsent"));
        if (!push.getDevice()) return fail(state.permission === "granted" ? t("form.notReady") : t("form.needsPush"));
        const payload = buildPayload({
          slug: detail.slug,
          service: serviceSel.value,
          allOffices: all.checked,
          offices,
          weekdays: days,
          timeStart: timeStart.value,
          timeEnd: timeEnd.value,
          maxDays: maxSel.value,
          consent: svc?.sensitive && consentBox.checked,
          lang: getLang(),
        });
        submit.disabled = true;
        try {
          const saved = await push.authed(() =>
            sub ? api.updateSubscription(sub.id, payload) : api.createSubscription(payload),
          );
          upsertSub(saved);
          if (sub) {
            toast(t("form.saved"));
            back();
          } else {
            toast(t("form.created"));
            // Back to the city on this tab, and over to the list.
            stack().pop();
            switchTab("subs", [{ name: "subs" }]);
          }
        } catch (err) {
          fail(errorMessage(err));
          if (err?.error === "not_available") render();
        } finally {
          submit.disabled = false;
        }
      },
    },
    h("label", null, t("form.service"), serviceSel),
    consent,
    h("label", { class: "check" }, all, h("span", null, t("form.allOffices"))),
    officeList,
    h("fieldset", null, h("legend", null, t("form.weekdays")), h("div", { class: "days" }, dayButtons)),
    h("fieldset", null, h("legend", null, t("form.timeWindow")), h("div", { class: "times" }, timeStart, h("span", null, "–"), timeEnd)),
    h("label", null, t("form.maxDays"), maxSel),
    errorEl,
    submit,
  );

  body.replaceChildren(
    permissionHint() ?? "",
    h("p", { class: "muted" }, detail.label || detail.city),
    form,
  );
}
