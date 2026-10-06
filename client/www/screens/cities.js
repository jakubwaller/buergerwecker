// Every city, grouped by city name like the website's switcher: one city can
// have several offices (Bürgerbüro, Kfz-Zulassung …), each its own entry.
import { t } from "../i18n.js";
import { h, loading, errorBox } from "../ui.js";
import { cities, go } from "../state.js";
import { permissionHint } from "./common.js";

export const title = () => t("cities.title");

export function groupCities(list) {
  const groups = new Map();
  for (const c of list) {
    const key = c.city || c.slug;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  return [...groups.entries()]
    .sort((a, b) => a[0].localeCompare(b[0], "de"))
    .map(([city, entries]) => ({ city, entries }));
}

let query = "";

export function mount(el) {
  el.append(permissionHint() ?? "");
  const body = h("div", null, loading());
  el.append(body);
  const load = () => {
    body.replaceChildren(loading());
    cities()
      .then((list) => show(body, groupCities(list)))
      .catch((e) => body.replaceChildren(errorBox(e, load)));
  };
  load();
}

function show(body, groups) {
  const listEl = h("div", { class: "city-list" });
  const search = h("input", {
    type: "search",
    class: "search",
    placeholder: t("cities.search"),
    "aria-label": t("cities.search"),
    value: query,
    oninput: (e) => {
      query = e.target.value;
      fill();
    },
  });
  const fill = () => {
    const q = query.trim().toLowerCase();
    const hits = groups.filter(
      (g) => !q || g.city.toLowerCase().includes(q) || g.entries.some((c) => (c.office || "").toLowerCase().includes(q)),
    );
    listEl.replaceChildren(
      ...(hits.length
        ? hits.map((g) =>
            h(
              "section",
              { class: "card city-group" },
              h("h2", null, g.city),
              g.entries.map((c) =>
                h(
                  "button",
                  { class: "row", onclick: () => go("city", { slug: c.slug }) },
                  h("span", null, c.office || c.label || c.slug),
                  h("span", { class: "chev", "aria-hidden": "true" }, "›"),
                ),
              ),
            ),
          )
        : [h("p", { class: "muted center" }, t("cities.none"))]),
    );
  };
  fill();
  body.replaceChildren(search, listEl);
}
