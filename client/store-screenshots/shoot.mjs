// Store screenshots for the app, PapaMap's recipe (docs/store-screenshots
// there): the app's own www/ in headless Chrome, under a stubbed Capacitor and
// a stubbed buergerwecker.de API, shot at Apple's exact pixel sizes, then
// composed with a headline and a subline from texts.json.
//
//   npm i --no-save playwright      (here; it drives the installed Google Chrome)
//   node shoot.mjs <out-dir> [--compose-only]
//
// <out-dir>/raw/<device>-<lang>-<NN>-<name>.png are the bare screens,
// <out-dir>/final/<canvas>-<lang>-<NN>-<name>.png what goes to App Store
// Connect. Cities and services come from the live public catalog; the alerts,
// slots and notifications are staged below, dated from today, and nothing
// here registers a device or touches the real API's authenticated routes.
import { chromium } from "playwright";
import { readFileSync, mkdirSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const WWW = join(here, "..", "www");
// The page is served from the API's own origin, so the stub needs no CORS.
const SITE = "https://buergerwecker.de";
const APP = `${SITE}/app/`;
const OUT = process.argv[2];
const COMPOSE_ONLY = process.argv.includes("--compose-only");
if (!OUT) {
  console.error("usage: node shoot.mjs <out-dir> [--compose-only]");
  process.exit(1);
}
mkdirSync(`${OUT}/raw`, { recursive: true });
mkdirSync(`${OUT}/final`, { recursive: true });

// CSS viewport × deviceScaleFactor must be exactly what Apple lists, or the
// upload is refused. `inset` fakes the status bar and home indicator the app
// lays out around on a phone (style.css reads --safe-top/--safe-bottom).
const DEVICES = {
  iphone69: { viewport: { width: 440, height: 956 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true, inset: [59, 34] },
  ipad13: { viewport: { width: 1032, height: 1376 }, deviceScaleFactor: 2, isMobile: false, hasTouch: true, inset: [24, 20] },
};
const CANVAS = {
  iphone69: { width: 1320, height: 2868, frameWidth: 1140, margin: 90, headlineSize: 88, sublineSize: 44, radius: 72, gap: 26 },
  ipad13: { width: 2064, height: 2752, frameWidth: 1560, margin: 252, headlineSize: 96, sublineSize: 48, radius: 60, gap: 30 },
};
const LANGS = ["de", "en"];

// --- Staged data ------------------------------------------------------------

// Leipzig's Bürgerbüro, ids from the live catalog (GET /cities/leipzig).
const LEIPZIG = {
  pass: "b04658d5-8d85-469a-a635-93337e055b73", // Personalausweis beantragen
  passport: "65974655-4a92-475a-8b2f-579450e285a4", // Reisepass beantragen
  pickup: "7ea65748-8333-4269-bf65-8453f9f27cf4", // Abholung Ausweisdokumente
  move: "29cd0a26-fe7a-4d65-88cd-1e05fd749c71", // An- oder Ummeldung Wohnsitz
  otto: "acf4e0bb-a78c-4670-95fc-50cf15348e0b", // Bürgerbüro Otto-Schill-Straße (Zentrum)
  paunsdorf: "cc242587-a45e-4f3e-98c0-bc19a5db420e", // Bürgerbüro Paunsdorf-Center (Paunsdorf)
};
const DRESDEN_PASSPORT = "488";
const MUENSTER_KFZ = "2407";

const now = new Date();
const isoDay = (d) => d.toISOString().slice(0, 10);
const inDays = (n) => new Date(now.getTime() + n * 86400000);
// The next few working days, so the staged slots never fall on a weekend.
const workdays = [];
for (let n = 1; workdays.length < 4; n++) {
  const d = inDays(n);
  if (d.getDay() % 6) workdays.push(isoDay(d));
}
const [d1, d2, d3, d4] = workdays;
const polledAt = new Date(now.getTime() - 120000).toISOString().replace(/\.\d{3}Z$/, "Z");
const expires = (n) => inDays(n).toISOString().replace(/\.\d{3}Z$/, "Z");

const subs = [
  { id: 41, city: "leipzig", appointment_type: LEIPZIG.pass, locations: [LEIPZIG.otto, LEIPZIG.paunsdorf], weekdays: [1, 2, 3, 4, 5], time_start: "08:00", time_end: "12:00", max_days_ahead: 14, expires_at: expires(12) },
  { id: 38, city: "muenster-kfz", appointment_type: MUENSTER_KFZ, locations: "all", weekdays: [1, 2, 3, 4, 5, 6], time_start: "00:00", time_end: "23:59", max_days_ahead: 7, expires_at: expires(5) },
  { id: 27, city: "dresden", appointment_type: DRESDEN_PASSPORT, locations: "all", weekdays: [1, 2, 3, 4, 5], time_start: "00:00", time_end: "23:59", max_days_ahead: null, expires_at: expires(9) },
].map((s) => ({ language: "de", consent_special: false, created_at: expires(-2), last_notified_at: null, active: true, ...s }));

// What the slots route would answer, per city: service id → slots.
const SNAPSHOTS = {
  leipzig: {
    [LEIPZIG.pass]: [[d2, "09:30", LEIPZIG.otto], [d2, "10:15", LEIPZIG.paunsdorf], [d3, "08:45", LEIPZIG.otto]],
    [LEIPZIG.move]: [[d1, "08:30", LEIPZIG.otto]],
    [LEIPZIG.passport]: [[d4, "14:00", LEIPZIG.paunsdorf]],
    [LEIPZIG.pickup]: [],
  },
  "muenster-kfz": { [MUENSTER_KFZ]: [[d1, "11:00", "243"], [d1, "11:20", "243"]] },
  dresden: { [DRESDEN_PASSPORT]: [] },
};

const detailCache = new Map();
async function liveDetail(slug, lang) {
  const key = `${slug}|${lang}`;
  if (!detailCache.has(key)) {
    const r = await fetch(`${SITE}/api/v1/cities/${slug}?lang=${lang}`);
    if (!r.ok) throw new Error(`live catalog: ${slug} ${r.status}`);
    detailCache.set(key, await r.json());
  }
  return detailCache.get(key);
}

async function slotsFor(slug, lang) {
  const detail = await liveDetail(slug, lang);
  const name = (list, id) => list.find((x) => x.id === id)?.name ?? id;
  const services = Object.entries(SNAPSHOTS[slug] ?? {}).map(([id, rows]) => {
    const slots = rows.map(([date, time, location]) => ({ date, time, location, location_name: name(detail.locations, location) }));
    return { id, name: name(detail.services, id), polled_at: polledAt, n_total: slots.length + (slots.length > 2 ? 4 : 0), earliest: slots[0] ?? null, slots };
  });
  return { slug, polled_at: polledAt, services };
}

// The response for an API request, {status, body}.
async function apiStub(u, method) {
  const lang = u.searchParams.get("lang") === "en" ? "en" : "de";
  const path = u.pathname.replace(/^\/api\/v1/, "");
  let m;
  if (method !== "GET") return { status: 405, body: { error: "not_allowed" } };
  if (path === "/cities" || (m = /^\/cities\/([a-z-]+)$/.exec(path))) {
    const r = await fetch(`${SITE}${u.pathname}?lang=${lang}`);
    return { status: r.status, body: await r.json() };
  }
  if ((m = /^\/cities\/([a-z-]+)\/slots$/.exec(path))) {
    if (!SNAPSHOTS[m[1]]) return { status: 403, body: { error: "not_subscribed" } };
    return { status: 200, body: await slotsFor(m[1], lang) };
  }
  if (path === "/device") return { status: 200, body: { device_id: 1, platform: "apns", language: lang, verified: true, subscriptions: [] } };
  if (path === "/subscriptions") return { status: 200, body: { subscriptions: subs } };
  return { status: 404, body: { error: "not_found" } };
}

// The notifications list, newest first; the first one is still unread.
function inboxFor(lang) {
  const at = (h) => now.getTime() - h * 3600000;
  const de = lang === "de";
  const dm = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.`;
  const dmEn = (iso) => `${Number(iso.slice(8, 10))}/${Number(iso.slice(5, 7))}`;
  const day = de ? dm : dmEn;
  return {
    seenAt: at(5),
    known: [],
    items: [
      { id: "a", at: at(1), type: "slots", city: "leipzig", sub: "41", title: de ? "Neue Termine in Leipzig" : "New appointments in Leipzig",
        body: `Bürgerbüro Otto-Schill-Straße (Zentrum): ${day(d2)} 09:30, ${day(d3)} 08:45\nBürgerbüro Paunsdorf-Center (Paunsdorf): ${day(d2)} 10:15` },
      { id: "b", at: at(19), type: "slots", city: "muenster-kfz", sub: "38", title: de ? "Neue Termine in Münster" : "New appointments in Münster",
        body: `KFZ-Zulassungsstelle, Führerscheine, Fahrschulen: ${day(d1)} 11:00, 11:20` },
      { id: "c", at: at(70), type: "checkin", city: "dresden", sub: "27",
        title: de ? "Suchst du noch einen Termin in Dresden?" : "Still looking for an appointment in Dresden?",
        body: de ? `Ohne Antwort stoppen die Benachrichtigungen am ${dm(isoDay(inDays(9)))} Tippen, um weiter zu suchen oder zu beenden.`
                 : `Without an answer the notifications stop on ${dmEn(isoDay(inDays(9)))}. Tap to keep looking or to stop.` },
    ],
  };
}

const BANNER = {
  de: { title: "Neue Termine in Leipzig", body: (d) => `Bürgerbüro Otto-Schill-Straße (Zentrum): ${d} 09:30, 10:15` },
  en: { title: "New appointments in Leipzig", body: (d) => `Bürgerbüro Otto-Schill-Straße (Zentrum): ${d} 09:30, 10:15` },
};

// --- The Capacitor stub (runs in the page before app.js) --------------------

function capacitorStub(seed) {
  const prefs = new Map(Object.entries(seed.prefs).map(([k, v]) => [k, JSON.stringify(v)]));
  let cred = JSON.stringify({ id: 1, secret: "stub" });
  const listeners = {};
  const ok = async () => ({});
  const Plugins = {
    Preferences: {
      get: async ({ key }) => ({ value: prefs.get(key) ?? null }),
      set: async ({ key, value }) => void prefs.set(key, value),
      remove: async ({ key }) => void prefs.delete(key),
      clear: async () => prefs.clear(),
    },
    SecureStore: { get: async () => ({ value: cred }), set: async ({ value }) => void (cred = value), clear: async () => void (cred = null) },
    PushNotifications: {
      addListener: (ev, fn) => ((listeners[ev] ??= []).push(fn), { remove() {} }),
      checkPermissions: async () => ({ receive: "granted" }),
      requestPermissions: async () => ({ receive: "granted" }),
      register: async () => setTimeout(() => (listeners.registration ?? []).forEach((f) => f({ value: "stub-token" })), 0),
      getDeliveredNotifications: async () => ({ notifications: [] }),
      removeAllDeliveredNotifications: ok,
      createChannel: ok,
    },
    App: { addListener: () => ({ remove() {} }), getInfo: async () => ({ version: "1.0", build: "66" }), minimizeApp: ok },
    Browser: { open: ok },
    WidgetBridge: { setConfig: ok, clear: ok },
  };
  // Only these: an absent PushGate is what tells push.js an iOS build has push.
  window.Capacitor = { isNativePlatform: () => true, getPlatform: () => "ios", isPluginAvailable: (n) => n in Plugins, Plugins };
}

// --- Raw shots ---------------------------------------------------------------

const TYPES = { html: "text/html", js: "text/javascript", css: "text/css", json: "application/json", png: "image/png", svg: "image/svg+xml" };

async function openApp(browser, device, lang, { onboarded = true } = {}) {
  const context = await browser.newContext({
    viewport: device.viewport,
    deviceScaleFactor: device.deviceScaleFactor,
    isMobile: device.isMobile,
    hasTouch: device.hasTouch,
    locale: lang === "de" ? "de-DE" : "en-US",
    colorScheme: "light",
    bypassCSP: true,
  });
  await context.route(`${SITE}/**`, async (route) => {
    const u = new URL(route.request().url());
    if (u.pathname.startsWith("/app/")) {
      const rel = u.pathname.slice(5) || "index.html";
      return route.fulfill({ path: join(WWW, rel), contentType: TYPES[rel.split(".").pop()] ?? "application/octet-stream" });
    }
    if (u.pathname.startsWith("/api/v1/")) {
      const { status, body } = await apiStub(u, route.request().method());
      return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    }
    return route.abort();
  });
  const prefs = { lang, device: { token: "stub-token", platform: "apns", verified: true }, inbox: inboxFor(lang) };
  if (onboarded) prefs.onboarded = true;
  await context.addInitScript(capacitorStub, { prefs });
  const page = await context.newPage();
  await page.goto(APP + "index.html", { waitUntil: "load" });
  const [top, bottom] = device.inset;
  await page.addStyleTag({ content: `:root{--safe-top:${top}px;--safe-bottom:${bottom}px}` });
  return { context, page };
}

// Drives the app through its own navigation module (the same instance app.js
// imported), so every screen renders exactly as a tap would make it.
const nav = (page, fn, ...args) => page.evaluate(([fn, args]) => import("/app/state.js").then((m) => m[fn](...args)), [fn, args]);
const settle = (page, ms = 400) => page.waitForTimeout(ms);

const SHOTS = [
  {
    nn: "01", name: "alerts",
    async stage(page) {
      await nav(page, "switchTab", "subs", [{ name: "subs" }]);
      await page.waitForSelector(".card.sub .slots-row .label");
    },
  },
  {
    nn: "02", name: "city",
    async stage(page, lang) {
      // The alerts list first: the city's cards say "Edit alert" only once it is loaded.
      await nav(page, "switchTab", "subs", [{ name: "subs" }]);
      await page.waitForSelector(".card.sub");
      await nav(page, "switchTab", "cities", [{ name: "cities" }, { name: "city", params: { slug: "leipzig" } }]);
      await page.waitForSelector(".card.service .earliest");
      // Past the disclaimer and the booking button: the cards are the point.
      await page.evaluate(() => document.querySelector("#screen h2").scrollIntoView({ block: "start" }));
    },
  },
  {
    nn: "03", name: "form",
    async stage(page, lang) {
      await nav(page, "switchTab", "cities", [{ name: "cities" }, { name: "city", params: { slug: "leipzig" } }, { name: "form", params: { slug: "leipzig", service: LEIPZIG.pass } }]);
      await page.waitForSelector("form.card select");
      await page.uncheck(`form.card > label.check input`);
      await page.check(`fieldset.loc-list label:has-text("Otto-Schill") input`);
      await page.check(`fieldset.loc-list label:has-text("Paunsdorf") input`);
      await page.fill("input[type=time] >> nth=0", "08:00");
      await page.fill("input[type=time] >> nth=1", "12:00");
      await page.selectOption("form.card select >> nth=1", "14");
      // The offices are the long part: show the filters below them.
      await page.evaluate(() => document.querySelector("fieldset:not(.loc-list)")?.scrollIntoView({ block: "center" }));
    },
  },
  {
    nn: "04", name: "cities",
    async stage(page) {
      await nav(page, "switchTab", "cities", [{ name: "cities" }]);
      await page.waitForSelector(".city-list section");
    },
  },
  {
    nn: "05", name: "inbox",
    async stage(page, lang) {
      await nav(page, "switchTab", "inbox", [{ name: "inbox" }]);
      await page.waitForSelector(".inbox .row");
      // A push arriving while the app is open: the in-app banner, as app.js shows it.
      const b = BANNER[lang];
      const dayText = await page.evaluate(([d, lang]) => import("/app/format.js").then((m) => m.formatDay(d, lang)), [d2, lang]);
      await page.evaluate(([title, body]) => import("/app/ui.js").then((m) => m.banner({ title, body })), [b.title, b.body(dayText)]);
    },
  },
  {
    nn: "06", name: "onboarding", fresh: true,
    async stage(page) {
      await page.waitForSelector(".onboarding .btn-primary");
    },
  },
];

async function shootDevice(browser, deviceName, device, lang) {
  const results = [];
  let app = await openApp(browser, device, lang);
  for (const shot of SHOTS) {
    if (shot.fresh) {
      await app.context.close();
      app = await openApp(browser, device, lang, { onboarded: false });
    }
    const path = `${OUT}/raw/${deviceName}-${lang}-${shot.nn}-${shot.name}.png`;
    try {
      // The banner stays up 8 s on its own; no shot inherits the last one's.
      await app.page.evaluate(() => (document.getElementById("banner").hidden = true));
      await shot.stage(app.page, lang);
      await settle(app.page);
      await app.page.screenshot({ path });
      results.push({ ok: true, shot: shot.name, path });
    } catch (e) {
      results.push({ ok: false, shot: shot.name, reason: e.message.split("\n")[0] });
    }
  }
  await app.context.close();
  return results;
}

// --- Compose -----------------------------------------------------------------

const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// One accent at most, marked *like this* in texts.json.
function headlineHtml(raw) {
  const m = /^(.*?)\*(.+?)\*(.*)$/s.exec(raw);
  if (!m) return escapeHtml(raw);
  return `${escapeHtml(m[1])}<span class="accent">${escapeHtml(m[2])}</span>${escapeHtml(m[3])}`;
}

// The app's own colours (www/style.css, light): bg, text, muted, accent.
function composeHtml({ canvas, headline, subline, image }) {
  const { width, height, frameWidth, margin, headlineSize, sublineSize, radius, gap } = canvas;
  return `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html, body { margin: 0; background: #f5f5f7; overflow: hidden; }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  #canvas { position: relative; width: ${width}px; height: ${height}px; overflow: hidden; background: #f5f5f7; }
  #headline { position: absolute; top: 150px; left: ${margin}px; width: ${width - 2 * margin}px; font-size: ${headlineSize}px; font-weight: 700; line-height: 1.15; letter-spacing: -0.02em; color: #1d1d1f; }
  #headline .accent { color: #2563eb; }
  #subline { position: absolute; left: ${margin}px; width: ${width - 2 * margin}px; font-size: ${sublineSize}px; line-height: 1.35; color: #6b6f76; }
  #frame { position: absolute; left: 50%; transform: translateX(-50%); width: ${frameWidth}px; border-radius: ${radius}px; overflow: hidden; border: 1px solid #d2d2d7; box-shadow: 0 24px 60px rgba(0,0,0,.18); background: #fff; }
  #frame img { display: block; width: 100%; height: auto; }
  </style></head><body><div id="canvas">
  <div id="headline">${headlineHtml(headline)}</div>
  <div id="subline">${escapeHtml(subline)}</div>
  <div id="frame"><img src="${image}"></div></div>
  <script>
  (function () {
    function fit(el, maxLines, minSize) {
      var size = parseFloat(getComputedStyle(el).fontSize);
      var lines = function () { return Math.round(el.scrollHeight / parseFloat(getComputedStyle(el).lineHeight)); };
      for (var i = 0; lines() > maxLines && size > minSize && i < 60; i++) el.style.fontSize = (--size) + "px";
    }
    var h = document.getElementById("headline"), s = document.getElementById("subline"), f = document.getElementById("frame");
    fit(h, 2, 40);
    s.style.top = (h.getBoundingClientRect().bottom + ${gap}) + "px";
    fit(s, 2, 24);
    f.style.top = (s.getBoundingClientRect().bottom + 80) + "px";
    document.body.dataset.ready = "1";
  })();
  </script></body></html>`;
}

async function composeAll(browser) {
  const texts = JSON.parse(readFileSync(join(here, "texts.json"), "utf8"));
  const out = [];
  for (const [canvasName, canvas] of Object.entries(CANVAS)) {
    for (const lang of LANGS) {
      for (const { nn, name } of SHOTS) {
        const raw = `${OUT}/raw/${canvasName}-${lang}-${nn}-${name}.png`;
        const path = `${OUT}/final/${canvasName}-${lang}-${nn}-${name}.png`;
        const t = texts[lang]?.[nn];
        if (!existsSync(raw) || !t) {
          out.push({ ok: false, path, reason: existsSync(raw) ? `no texts.json entry ${lang}/${nn}` : `no raw shot ${raw}` });
          continue;
        }
        const image = `data:image/png;base64,${readFileSync(raw).toString("base64")}`;
        const context = await browser.newContext({ viewport: { width: canvas.width, height: canvas.height }, deviceScaleFactor: 1 });
        const page = await context.newPage();
        await page.setContent(composeHtml({ canvas, headline: t.headline, subline: t.subline, image }), { waitUntil: "load" });
        await page.waitForSelector("body[data-ready='1']");
        await page.screenshot({ path });
        await context.close();
        out.push({ ok: true, path, expected: `${canvas.width}x${canvas.height}` });
      }
    }
  }
  return out;
}

// Width and height from the PNG header, to check against Apple's sizes.
function pixelSize(path) {
  const b = readFileSync(path);
  return `${b.readUInt32BE(16)}x${b.readUInt32BE(20)}`;
}

async function main() {
  const browser = await chromium.launch({ channel: "chrome", args: ["--no-sandbox"] });
  try {
    if (!COMPOSE_ONLY) {
      for (const [deviceName, device] of Object.entries(DEVICES)) {
        for (const lang of LANGS) {
          for (const r of await shootDevice(browser, deviceName, device, lang)) {
            console.log(r.ok ? `raw  ${deviceName}-${lang}-${r.shot}: ${pixelSize(r.path)}` : `SKIP ${deviceName}-${lang}-${r.shot}: ${r.reason}`);
          }
        }
      }
    }
    const bad = [];
    for (const r of await composeAll(browser)) {
      if (!r.ok) {
        console.log(`SKIP ${r.path}: ${r.reason}`);
        continue;
      }
      const size = pixelSize(r.path);
      if (size !== r.expected) bad.push(`${r.path}: ${size}, wanted ${r.expected}`);
      console.log(`final ${r.path}: ${size}`);
    }
    if (bad.length) throw new Error(`off-spec sizes, the store refuses these:\n  ${bad.join("\n  ")}`);
  } finally {
    await browser.close();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
