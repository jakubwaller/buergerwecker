// Dates, times and filter summaries, in the app's language. Pure functions,
// tested in test/format.test.mjs. Own month and weekday tables rather than
// Intl: the output is then the same on every WebView and in Node.
import { t } from "./i18n.js";

const pad = (n) => String(n).padStart(2, "0");

// ISO weekday, 1 = Monday … 7 = Sunday, as the server counts them.
const isoWeekday = (jsDay) => (jsDay === 0 ? 7 : jsDay);

// A slot's own date, "2026-10-08". It is a calendar day at the city's office,
// not an instant: never shifted through the device's time zone.
export function parseDay(day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ""));
  if (!m) return null;
  return { y: +m[1], m: +m[2], d: +m[3] };
}

function dayKey(y, m, d) {
  return `${y}-${pad(m)}-${pad(d)}`;
}

// "Do., 8. Okt." / "Thu 8 Oct"; "heute"/"today" and "morgen"/"tomorrow"
// relative to `now` (the device's own calendar day).
export function formatDay(day, lang, now = new Date()) {
  const p = parseDay(day);
  if (!p) return String(day ?? "");
  const today = dayKey(now.getFullYear(), now.getMonth() + 1, now.getDate());
  const tm = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  const tomorrow = dayKey(tm.getFullYear(), tm.getMonth() + 1, tm.getDate());
  const key = dayKey(p.y, p.m, p.d);
  if (key === today) return t("date.today", {}, lang);
  if (key === tomorrow) return t("date.tomorrow", {}, lang);
  const wd = isoWeekday(new Date(Date.UTC(p.y, p.m - 1, p.d)).getUTCDay());
  return t("date.dayMonth", { wd: t(`weekday.${wd}`, {}, lang), d: p.d, m: t(`month.${p.m}`, {}, lang) }, lang);
}

// One slot: "Do., 8. Okt., 09:30 Uhr" / "Thu 8 Oct, 09:30".
export function formatSlot(slot, lang, now = new Date()) {
  if (!slot) return "";
  const day = formatDay(slot.date, lang, now);
  return slot.time ? t("date.atTime", { day, time: t("date.time", { time: slot.time }, lang) }, lang) : day;
}

// An instant from the server ("2026-10-06T12:04:00Z") in the device's local
// time: "heute, 14:04 Uhr". null for a missing or unparsable value.
export function formatInstant(iso, lang, now = new Date()) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const day = formatDay(dayKey(d.getFullYear(), d.getMonth() + 1, d.getDate()), lang, now);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return t("date.atTime", { day, time: t("date.time", { time }, lang) }, lang);
}

// Just the local calendar day of an instant: "Di., 20. Okt."
export function formatInstantDay(iso, lang, now = new Date()) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return formatDay(dayKey(d.getFullYear(), d.getMonth() + 1, d.getDate()), lang, now);
}

// [1,2,3,4,5] → "Mo–Fr"; [1,3,5] → "Mo, Mi, Fr"; all seven → "jeden Tag".
// A run of three or more days is written as a range.
export function weekdaySummary(days, lang) {
  const set = [...new Set((days || []).map(Number))].filter((d) => d >= 1 && d <= 7).sort((a, b) => a - b);
  if (set.length === 7 || set.length === 0) return t("subs.everyDay", {}, lang);
  const name = (d) => t(`weekday.${d}`, {}, lang);
  const parts = [];
  for (let i = 0; i < set.length; ) {
    let j = i;
    while (j + 1 < set.length && set[j + 1] === set[j] + 1) j++;
    if (j - i >= 2) parts.push(`${name(set[i])}–${name(set[j])}`);
    else for (let k = i; k <= j; k++) parts.push(name(set[k]));
    i = j + 1;
  }
  return parts.join(", ");
}

// "08:00–12:00", or "ganztags" for the whole day (the form's defaults).
export function timeWindow(start, end, lang) {
  const s = start || "00:00";
  const e = end || "23:59";
  if (s === "00:00" && (e === "23:59" || e === "24:00")) return t("subs.anyTime", {}, lang);
  return `${s}–${e}`;
}

// The one-line filter summary under a subscription:
// "Mo–Fr · 08:00–12:00 · nächste 14 Tage · 2 Standorte".
export function filterSummary(sub, lang) {
  const parts = [weekdaySummary(sub.weekdays, lang), timeWindow(sub.time_start, sub.time_end, lang)];
  if (sub.max_days_ahead) parts.push(t("subs.nextDays", { n: sub.max_days_ahead }, lang));
  const locs = sub.locations;
  if (locs === "all" || !Array.isArray(locs) || !locs.length) parts.push(t("subs.allOffices", {}, lang));
  return parts.join(" · ");
}

// What a service's card shows from the slots endpoint: the earliest slot,
// up to `few` more after it, and how many beyond those exist.
export function slotPreview(service, few = 3) {
  if (!service) return { earliest: null, next: [], more: 0 };
  const slots = Array.isArray(service.slots) ? service.slots : [];
  const earliest = service.earliest ?? slots[0] ?? null;
  const same = (a, b) => a && b && a.date === b.date && a.time === b.time && a.location === b.location;
  const rest = slots.filter((s) => !same(s, earliest));
  const next = rest.slice(0, few);
  const total = Number.isFinite(service.n_total) ? service.n_total : slots.length;
  const shown = (earliest ? 1 : 0) + next.length;
  return { earliest, next, more: Math.max(0, total - shown) };
}
