// The home-screen widget's view of the app: which cities it should show.
// The widget itself is native (client/ios/App/BuergerweckerWidget,
// client/android/.../EarliestSlotWidget.java) and fetches
// GET /cities/<slug>/slots on its own, with the device credential it reads
// natively from the secure store (the Keychain group it shares with the app,
// the AndroidKeyStore-encrypted file); the credential never passes through
// here. All the page hands over is the cities of the device's active alerts,
// the services each one watches, the language, and the strings and date words
// the widget shows (native code cannot read i18n.js). Nothing here goes off
// the device.
//
// Special-category alerts (Art. 9, consent_special) are left out entirely: a
// home-screen widget shows the city, a time and an office to anyone who looks
// at the phone, which is exactly what the push for such an alert withholds
// (app/push.py, render_push's redacted branch).
import { plugin } from "./native.js";
import { STRINGS } from "./i18n.js";

export const MAX_CITIES = 5;
// 3: the first list without special-category alerts. Both native widgets treat
// a list with a lower `v`, or none, as no list at all (Shared.configVersion in
// BuergerweckerWidget.swift, CONFIG_VERSION in EarliestSlotWidget.java), so a
// list an older app version left behind can never put such an alert's slot on
// the home screen, whatever runs first after the update.
export const CONFIG_VERSION = 3;

// Every i18n key whose text the native widget needs, by prefix.
const PREFIXES = ["widget.", "date.", "weekday.", "month."];

export function widgetStrings(lang) {
  const bundle = STRINGS[lang] ?? STRINGS.en;
  return Object.fromEntries(Object.entries(bundle).filter(([k]) => PREFIXES.some((p) => k.startsWith(p))));
}

// subs: the server's subscription list; cityList: GET /cities' entries, for
// the display names. Only active alerts count: an expired one no longer
// watches anything. A special-category one (consent_special) never counts, so
// a city whose only alerts are such is not in the list at all. Each alert goes
// over with its own filter, because the widget may only show a slot that would
// also trigger that alert (the server's app/filters.py matches(): service,
// offices, weekdays, time window, days ahead). `locations` is "all" or a list
// of office ids, as the API sends it; times are "HH:MM", weekdays ISO 1 = Monday.
export function buildConfig(subs, cityList, lang) {
  const names = new Map((cityList ?? []).map((c) => [c.slug, c]));
  const bySlug = new Map();
  for (const s of subs ?? []) {
    if (!s || s.active === false || !s.city || !s.appointment_type) continue;
    if (s.consent_special) continue;
    if (!bySlug.has(s.city)) bySlug.set(s.city, []);
    bySlug.get(s.city).push({
      service: s.appointment_type,
      locations: Array.isArray(s.locations) ? s.locations : "all",
      weekdays: Array.isArray(s.weekdays) ? s.weekdays.map(Number) : [1, 2, 3, 4, 5, 6, 7],
      timeStart: s.time_start || "00:00",
      timeEnd: s.time_end || "23:59",
      maxDaysAhead: Number.isFinite(s.max_days_ahead) && s.max_days_ahead > 0 ? s.max_days_ahead : null,
    });
  }
  const cities = [...bySlug].slice(0, MAX_CITIES).map(([slug, alerts]) => {
    const c = names.get(slug);
    return { slug, name: c?.city ?? slug, office: c?.office ?? "", alerts };
  });
  return { v: CONFIG_VERSION, lang, strings: widgetStrings(lang), cities };
}

// The config this launch last handed over. Every setConfig makes the widget
// fetch each of its cities again, and the slots route has a per-device budget
// (about 60 an hour, shared with the app's own city overview); the list
// reloads on every resume, so an unchanged config is not sent twice. The
// first sync of each launch always goes through.
let lastSent = null;

// Fire and forget: the widget is a convenience, a failure here must never
// show in the app. The native plugin stores the JSON and asks the OS to
// reload the widgets; without the plugin (web, an old build) this is a no-op.
export async function sync(subs, cityList, lang) {
  const w = plugin("WidgetBridge");
  if (!w) return;
  const config = JSON.stringify(buildConfig(subs, cityList, lang));
  if (config === lastSent) return;
  await w.setConfig({ config });
  lastSent = config;
}

// The next sync goes through even with the same list: the device changed (a
// new registration, verification won or lost), and with it what the widget's
// own fetch may see.
export function invalidate() {
  lastSent = null;
}

export async function clear() {
  const w = plugin("WidgetBridge");
  lastSent = null;
  if (!w) return;
  await w.clear();
}
