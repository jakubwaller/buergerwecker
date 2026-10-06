// The home-screen widget: what the page hands it (www/widget.js), and the
// contract between that, the iOS extension and the Android widget, which are
// native code no test here can run. A name, a key or an App Group that drifts
// between those files fails here instead of on a phone.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join } from "node:path";

const calls = [];
let available = true;
globalThis.Capacitor = {
  isNativePlatform: () => true,
  isPluginAvailable: (n) => available && n === "WidgetBridge",
  getPlatform: () => "ios",
  Plugins: {
    WidgetBridge: {
      setConfig: async (a) => void calls.push(["setConfig", a]),
      clear: async () => void calls.push(["clear"]),
    },
  },
};

const { buildConfig, sync, clear, widgetStrings, MAX_CITIES } = await import("../www/widget.js");
const { STRINGS } = await import("../www/i18n.js");
const { BUNDLE_IDS, APP_GROUP } = await import("../ios/asc.mjs");

const here = (p) => new URL(`../${p}`, import.meta.url);
const read = (p) => readFileSync(here(p), "utf8");
const IOS = "ios/App/BuergerweckerWidget/BuergerweckerWidget.swift";
const IOS_PLUGIN = "ios/App/App/WidgetBridgePlugin.swift";
const JAVA = "android/app/src/main/java/de/buergerwecker/app";

const sub = (city, appointment_type, active = true) => ({ id: `${city}-${appointment_type}`, city, appointment_type, active });
const cityList = [
  { slug: "leipzig", city: "Leipzig", office: "Bürgeramt" },
  { slug: "bonn", city: "Bonn", office: "Bürgerdienste" },
];

test("the config lists the cities of the active alerts, with the services each one watches", () => {
  const c = buildConfig([sub("leipzig", "a"), sub("leipzig", "b"), sub("bonn", "c"), sub("leipzig", "x", false)], cityList, "de");
  assert.equal(c.v, 1);
  assert.equal(c.lang, "de");
  assert.deepEqual(c.cities, [
    { slug: "leipzig", name: "Leipzig", office: "Bürgeramt", services: ["a", "b"] },
    { slug: "bonn", name: "Bonn", office: "Bürgerdienste", services: ["c"] },
  ]);
});

test("no active alert, no cities: the widget asks the person to open the app", () => {
  assert.deepEqual(buildConfig([], cityList, "en").cities, []);
  assert.deepEqual(buildConfig(null, null, "en").cities, []);
  assert.deepEqual(buildConfig([sub("leipzig", "a", false)], cityList, "en").cities, []);
});

test("an unknown city falls back to its slug, and the list is capped", () => {
  assert.equal(buildConfig([sub("zz", "a")], [], "de").cities[0].name, "zz");
  const many = Array.from({ length: 9 }, (_, i) => sub(`c${i}`, "a"));
  assert.equal(buildConfig(many, [], "de").cities.length, MAX_CITIES);
});

test("the widget gets the page's own words and date tables, in the chosen language", () => {
  for (const lang of ["de", "en"]) {
    const s = widgetStrings(lang);
    for (const k of ["widget.openApp", "widget.noSlots", "widget.noSnapshot", "widget.asOf", "date.today", "date.tomorrow",
                     "date.dayMonth", "date.atTime", "date.time"]) assert.ok(s[k], `${lang} ${k}`);
    for (let i = 1; i <= 7; i++) assert.ok(s[`weekday.${i}`], `${lang} weekday.${i}`);
    for (let i = 1; i <= 12; i++) assert.ok(s[`month.${i}`], `${lang} month.${i}`);
    assert.ok(!("tab.cities" in s), "nothing else leaks into the widget");
  }
  assert.equal(buildConfig([], [], "de").strings["widget.noSlots"], STRINGS.de["widget.noSlots"]);
  assert.equal(buildConfig([], [], "en").strings["widget.noSlots"], STRINGS.en["widget.noSlots"]);
});

test("sync hands the plugin one JSON string and never a credential; clear forgets", async () => {
  calls.length = 0;
  await sync([sub("leipzig", "a")], cityList, "de");
  assert.equal(calls.length, 1);
  const [name, arg] = calls[0];
  assert.equal(name, "setConfig");
  assert.deepEqual(Object.keys(arg), ["config"]);
  const parsed = JSON.parse(arg.config);
  assert.deepEqual(Object.keys(parsed).sort(), ["cities", "lang", "strings", "v"]);
  assert.doesNotMatch(arg.config, /secret|Bearer|token/i);
  await clear();
  assert.deepEqual(calls[1], ["clear"]);
});

test("without the plugin (web, an older build) sync and clear do nothing", async () => {
  available = false;
  calls.length = 0;
  await sync([sub("leipzig", "a")], cityList, "de");
  await clear();
  assert.deepEqual(calls, []);
  available = true;
});

// --- The native side -------------------------------------------------------

const walk = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]));

test("every WidgetBridge method the page calls exists on iOS and Android", () => {
  const called = new Set();
  for (const f of walk(here("www/").pathname).filter((f) => f.endsWith(".js"))) {
    for (const m of readFileSync(f, "utf8").matchAll(/\bw\??\.(\w+)\(/g)) {
      if (readFileSync(f, "utf8").includes('plugin("WidgetBridge")')) called.add(m[1]);
    }
  }
  assert.deepEqual([...called].sort(), ["clear", "setConfig"]);
  const swift = read(IOS_PLUGIN);
  const java = read(`${JAVA}/WidgetBridgePlugin.java`);
  assert.match(swift, /jsName = "WidgetBridge"/);
  assert.match(java, /@CapacitorPlugin\(name = "WidgetBridge"\)/);
  for (const name of called) {
    assert.match(swift, new RegExp(`CAPPluginMethod\\(name: "${name}"`), `${name} on iOS`);
    assert.match(swift, new RegExp(`@objc func ${name}\\(_ call: CAPPluginCall\\)`), `${name} implemented on iOS`);
    assert.match(java, new RegExp(`@PluginMethod\\s+public void ${name}\\(PluginCall call\\)`), `${name} on Android`);
  }
});

test("the plugin is registered where each platform builds its bridge", () => {
  assert.match(read(`${JAVA}/MainActivity.java`), /registerPlugin\(WidgetBridgePlugin\.class\)/);
  assert.match(read("ios/App/App/MainViewController.swift"), /registerPluginInstance\(WidgetBridgePlugin\(\)\)/);
  // The scene creates the window's view controller itself, so the storyboard's
  // class alone would not do.
  assert.match(read("ios/App/App/SceneDelegate.swift"), /rootViewController = MainViewController\(\)/);
  assert.match(read("ios/App/App/Base.lproj/Main.storyboard"), /customClass="MainViewController"/);
});

test("the App Group is one name: asc.mjs, both entitlements, both Swift files", () => {
  assert.equal(APP_GROUP, "group.de.buergerwecker.app");
  for (const f of ["ios/App/App/App.entitlements", "ios/App/BuergerweckerWidget/BuergerweckerWidget.entitlements"]) {
    assert.match(read(f), new RegExp(`<key>com.apple.security.application-groups</key>\\s*<array>\\s*<string>${APP_GROUP}</string>`), f);
  }
  for (const f of [IOS, IOS_PLUGIN]) assert.ok(read(f).includes(`"${APP_GROUP}"`), f);
});

test("the two Swift targets agree on the storage keys, Android on one preference file", () => {
  const key = (src, name) => new RegExp(`${name} = "(\\w+)"`).exec(src)?.[1];
  for (const name of ["configKey", "cacheKey"]) {
    assert.ok(key(read(IOS), name), name);
    assert.equal(key(read(IOS), name), key(read(IOS_PLUGIN), name), name);
  }
  const java = read(`${JAVA}/EarliestSlotWidget.java`);
  assert.equal(/KEY_CONFIG = "(\w+)"/.exec(java)[1], key(read(IOS), "configKey"), "same key on both platforms");
  assert.equal(/KEY_CACHE = "(\w+)"/.exec(java)[1], key(read(IOS), "cacheKey"));
  assert.doesNotMatch(read(`${JAVA}/WidgetBridgePlugin.java`), /"widget_config"|getSharedPreferences\("/, "the plugin uses the widget's constants");
});

test("every string key native code reads is one the page sends", () => {
  const sent = widgetStrings("de");
  for (const f of [IOS, `${JAVA}/EarliestSlotWidget.java`]) {
    const src = read(f);
    const keys = [...src.matchAll(/"((?:widget|date)\.\w+)"/g)].map((m) => m[1]);
    assert.ok(keys.length > 5, `found the keys in ${f}`);
    for (const k of keys) assert.ok(k in sent, `${f}: ${k} is not sent`);
  }
});

test("the widget extension is in the Xcode project, embedded, signed with the right profile", () => {
  const pbx = read("ios/App/App.xcodeproj/project.pbxproj");
  assert.ok(BUNDLE_IDS.some((b) => b.identifier === "de.buergerwecker.app.widget"));
  assert.match(pbx, /PRODUCT_BUNDLE_IDENTIFIER = de\.buergerwecker\.app\.widget;/);
  assert.match(pbx, /Embed Foundation Extensions/);
  assert.match(pbx, /com\.apple\.product-type\.app-extension/);
  assert.match(pbx, /CODE_SIGN_ENTITLEMENTS = BuergerweckerWidget\/BuergerweckerWidget\.entitlements;/);
  for (const f of ["Info.plist", "PrivacyInfo.xcprivacy", "BuergerweckerWidget.swift", "de.lproj/Localizable.strings", "en.lproj/Localizable.strings"]) {
    assert.ok(existsSync(here(`ios/App/BuergerweckerWidget/${f}`)), f);
  }
  // The export options in the workflow name a profile for every bundle id.
  const workflow = readFileSync(new URL("../../.github/workflows/app-build.yml", import.meta.url), "utf8");
  for (const { identifier } of BUNDLE_IDS) {
    assert.ok(workflow.includes(`<key>${identifier}</key><string>Buergerwecker CI ${identifier}</string>`), identifier);
  }
});

test("both privacy manifests declare the App Group's defaults", () => {
  for (const f of ["ios/App/App/PrivacyInfo.xcprivacy", "ios/App/BuergerweckerWidget/PrivacyInfo.xcprivacy"]) {
    assert.match(read(f), /<string>1C8F\.1<\/string>/, f);
  }
});

test("the Android widget is declared with the platform's update period, and does not book", () => {
  const manifest = read("android/app/src/main/AndroidManifest.xml");
  assert.match(manifest, /android:name="\.EarliestSlotWidget"/);
  const info = read("android/app/src/main/res/xml/earliest_slot_widget_info.xml");
  const period = Number(/updatePeriodMillis="(\d+)"/.exec(info)[1]);
  assert.ok(period >= 30 * 60 * 1000, "below the platform's 30-minute floor it is raised anyway");
  for (const f of [IOS, `${JAVA}/EarliestSlotWidget.java`]) {
    // A boolean, so a failure does not print the whole source file.
    assert.ok(!/setRequestMethod|httpMethod|"(POST|PUT|DELETE)"|\/go\/|booking_url/.test(read(f)), `${f} only reads`);
  }
});
