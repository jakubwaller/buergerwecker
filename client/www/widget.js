// The home-screen widget's view of the app: which cities it should show.
// The widget itself is native (client/ios/App/BuergerweckerWidget,
// client/android/.../widget) and fetches GET /cities/<slug>/slots on its own;
// that route is public, so no device credential is shared with it. All the
// page hands over is the cities of the device's active alerts, the services
// each one watches, the language, and the strings and date words the widget
// shows (native code cannot read i18n.js). Nothing here goes off the device.
import { plugin } from "./native.js";
import { STRINGS } from "./i18n.js";

export const MAX_CITIES = 5;
export const CONFIG_VERSION = 1;

// Every i18n key whose text the native widget needs, by prefix.
const PREFIXES = ["widget.", "date.", "weekday.", "month."];

export function widgetStrings(lang) {
  const bundle = STRINGS[lang] ?? STRINGS.en;
  return Object.fromEntries(Object.entries(bundle).filter(([k]) => PREFIXES.some((p) => k.startsWith(p))));
}

// subs: the server's subscription list; cityList: GET /cities' entries, for
// the display names. Only active alerts count: an expired one no longer
// watches anything.
export function buildConfig(subs, cityList, lang) {
  const names = new Map((cityList ?? []).map((c) => [c.slug, c]));
  const bySlug = new Map();
  for (const s of subs ?? []) {
    if (!s || s.active === false || !s.city) continue;
    if (!bySlug.has(s.city)) bySlug.set(s.city, new Set());
    if (s.appointment_type) bySlug.get(s.city).add(s.appointment_type);
  }
  const cities = [...bySlug].slice(0, MAX_CITIES).map(([slug, services]) => {
    const c = names.get(slug);
    return { slug, name: c?.city ?? slug, office: c?.office ?? "", services: [...services] };
  });
  return { v: CONFIG_VERSION, lang, strings: widgetStrings(lang), cities };
}

// Fire and forget: the widget is a convenience, a failure here must never
// show in the app. The native plugin stores the JSON and asks the OS to
// reload the widgets; without the plugin (web, an old build) this is a no-op.
export async function sync(subs, cityList, lang) {
  const w = plugin("WidgetBridge");
  if (!w) return;
  await w.setConfig({ config: JSON.stringify(buildConfig(subs, cityList, lang)) });
}

export async function clear() {
  const w = plugin("WidgetBridge");
  if (!w) return;
  await w.clear();
}
