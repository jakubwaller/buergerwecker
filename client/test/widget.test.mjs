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

const { buildConfig, sync, clear, invalidate, widgetStrings, MAX_CITIES, CONFIG_VERSION } = await import("../www/widget.js");
const { STRINGS } = await import("../www/i18n.js");
const { BUNDLE_IDS, APP_GROUP, KEYCHAIN_GROUP } = await import("../ios/asc.mjs");

const here = (p) => new URL(`../${p}`, import.meta.url);
const read = (p) => readFileSync(here(p), "utf8");
const IOS = "ios/App/BuergerweckerWidget/BuergerweckerWidget.swift";
const IOS_PLUGIN = "ios/App/App/WidgetBridgePlugin.swift";
const IOS_SECURE = "ios/App/App/SecureStorePlugin.swift";
const JAVA = "android/app/src/main/java/de/buergerwecker/app";
const ENTITLEMENTS = ["ios/App/App/App.entitlements", "ios/App/BuergerweckerWidget/BuergerweckerWidget.entitlements"];

const sub = (city, appointment_type, active = true, extra = {}) => ({
  id: `${city}-${appointment_type}`, city, appointment_type, active,
  locations: "all", weekdays: [1, 2, 3, 4, 5, 6, 7], time_start: "00:00", time_end: "23:59", max_days_ahead: null, ...extra,
});
const open = { locations: "all", weekdays: [1, 2, 3, 4, 5, 6, 7], timeStart: "00:00", timeEnd: "23:59", maxDaysAhead: null };
const cityList = [
  { slug: "leipzig", city: "Leipzig", office: "Bürgeramt" },
  { slug: "bonn", city: "Bonn", office: "Bürgerdienste" },
];

test("the config lists the cities of the active alerts, each alert with its own filter", () => {
  const c = buildConfig([sub("leipzig", "a"), sub("leipzig", "b"), sub("bonn", "c"), sub("leipzig", "x", false)], cityList, "de");
  assert.equal(c.v, 3);
  assert.equal(c.v, CONFIG_VERSION);
  assert.equal(c.lang, "de");
  assert.deepEqual(c.cities, [
    { slug: "leipzig", name: "Leipzig", office: "Bürgeramt", alerts: [{ service: "a", ...open }, { service: "b", ...open }] },
    { slug: "bonn", name: "Bonn", office: "Bürgerdienste", alerts: [{ service: "c", ...open }] },
  ]);
});

test("a special-category alert (Art. 9) never reaches the widget, nor does a city it alone watches", () => {
  const special = { consent_special: true };
  const c = buildConfig(
    [sub("leipzig", "sensitive", true, special), sub("leipzig", "a", true, { consent_special: false }), sub("bonn", "sensitive", true, special)],
    cityList,
    "de",
  );
  assert.deepEqual(c.cities, [{ slug: "leipzig", name: "Leipzig", office: "Bürgeramt", alerts: [{ service: "a", ...open }] }]);
  assert.doesNotMatch(JSON.stringify(c), /sensitive|bonn|Bonn/);
  // Only special ones: as if there were no alert at all.
  assert.deepEqual(buildConfig([sub("bonn", "sensitive", true, special)], cityList, "de").cities, []);
});

test("offices, weekdays, time window and days ahead travel with the alert, as the server's filter has them", () => {
  const f = { locations: ["o1", "o2"], weekdays: [1, 3], time_start: "08:30", time_end: "12:00", max_days_ahead: 14 };
  const [a] = buildConfig([sub("leipzig", "a", true, f)], cityList, "de").cities[0].alerts;
  assert.deepEqual(a, { service: "a", locations: ["o1", "o2"], weekdays: [1, 3], timeStart: "08:30", timeEnd: "12:00", maxDaysAhead: 14 });
  // A 0 or missing limit is no limit, as Filter.from_json normalises it.
  const [b] = buildConfig([sub("leipzig", "a", true, { max_days_ahead: 0 })], cityList, "de").cities[0].alerts;
  assert.equal(b.maxDaysAhead, null);
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
    for (const k of ["widget.openApp", "widget.reopen", "widget.noMatch", "widget.noSnapshot", "widget.asOf", "date.today", "date.tomorrow",
                     "date.dayMonth", "date.atTime", "date.time"]) assert.ok(s[k], `${lang} ${k}`);
    for (let i = 1; i <= 7; i++) assert.ok(s[`weekday.${i}`], `${lang} weekday.${i}`);
    for (let i = 1; i <= 12; i++) assert.ok(s[`month.${i}`], `${lang} month.${i}`);
    assert.ok(!("tab.cities" in s), "nothing else leaks into the widget");
  }
  assert.equal(buildConfig([], [], "de").strings["widget.noMatch"], STRINGS.de["widget.noMatch"]);
  assert.equal(buildConfig([], [], "en").strings["widget.noMatch"], STRINGS.en["widget.noMatch"]);
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

test("an unchanged list is not handed over twice: every setConfig costs the widget a fetch per city", async () => {
  calls.length = 0;
  await sync([sub("leipzig", "a")], cityList, "de");
  await sync([sub("leipzig", "a")], cityList, "de");
  assert.equal(calls.length, 1, "a resume with the same list");
  await sync([sub("leipzig", "a"), sub("bonn", "c")], cityList, "de");
  await sync([sub("leipzig", "a"), sub("bonn", "c")], cityList, "en");
  assert.equal(calls.length, 3, "a new alert, a new language");
  await clear();
  await sync([sub("leipzig", "a"), sub("bonn", "c")], cityList, "en");
  assert.deepEqual(calls.map((c) => c[0]), ["setConfig", "setConfig", "setConfig", "clear", "setConfig"], "after a clear it goes again");
  // A new or newly verified device changes what the widget may fetch: the
  // same list goes over again, which reloads the widget.
  invalidate();
  await sync([sub("leipzig", "a"), sub("bonn", "c")], cityList, "en");
  assert.equal(calls.length, 6, "after invalidate() it goes again");
  await sync([sub("leipzig", "a"), sub("bonn", "c")], cityList, "en");
  assert.equal(calls.length, 6, "and then dedupes as before");
  await clear();
});

test("the app invalidates the widget's last list whenever the device or its verification changes", () => {
  const app = read("www/app.js");
  const changed = app.slice(app.indexOf("changed: (d) =>"), app.indexOf("error: (e) =>"));
  assert.match(changed, /if \(newId \|\| flipped\) widget\.invalidate\(\);/);
  assert.ok(changed.indexOf("widget.invalidate()") < changed.indexOf("if (!ready) return;"), "also before start-up finished");
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
  for (const name of ["configKey", "cacheKey", "lockedKey"]) {
    assert.ok(key(read(IOS), name), name);
    assert.equal(key(read(IOS), name), key(read(IOS_PLUGIN), name), name);
  }
  const java = read(`${JAVA}/EarliestSlotWidget.java`);
  assert.equal(/KEY_CONFIG = "(\w+)"/.exec(java)[1], key(read(IOS), "configKey"), "same key on both platforms");
  assert.equal(/KEY_CACHE = "(\w+)"/.exec(java)[1], key(read(IOS), "cacheKey"));
  assert.equal(/KEY_LOCKED = "(\w+)"/.exec(java)[1], key(read(IOS), "lockedKey"));
  assert.doesNotMatch(read(`${JAVA}/WidgetBridgePlugin.java`), /"widget_config"|getSharedPreferences\("/, "the plugin uses the widget's constants");
  // "Delete my data" forgets the lock with the rest.
  assert.match(read(IOS_PLUGIN), /removeObject\(forKey: Self\.lockedKey\)/);
  assert.match(read(`${JAVA}/WidgetBridgePlugin.java`), /remove\(EarliestSlotWidget\.KEY_LOCKED\)/);
});

test("a list older than CONFIG_VERSION is no list on either platform, and the old cache is never read", () => {
  const swift = read(IOS);
  const java = read(`${JAVA}/EarliestSlotWidget.java`);
  assert.equal(Number(/static let configVersion = (\d+)/.exec(swift)[1]), CONFIG_VERSION, "iOS");
  assert.equal(Number(/static final int CONFIG_VERSION = (\d+);/.exec(java)[1]), CONFIG_VERSION, "Android");
  assert.match(swift, /\(config\.v \?\? 0\) >= Shared\.configVersion else \{ return nil \}/);
  assert.match(java, /config\.optInt\("v", 0\) >= CONFIG_VERSION \? config : null/);
  // The cache written under the old list may hold a special-category slot: a
  // new key, the old one deleted on every load and on "Delete my data".
  const legacy = "widget_cache";
  assert.notEqual(/cacheKey = "(\w+)"/.exec(swift)[1], legacy);
  assert.equal(/legacyCacheKey = "(\w+)"/.exec(swift)[1], legacy);
  assert.equal(/legacyCacheKey = "(\w+)"/.exec(read(IOS_PLUGIN))[1], legacy);
  assert.equal(/KEY_LEGACY_CACHE = "(\w+)"/.exec(java)[1], legacy);
  const load = swift.slice(swift.indexOf("private func load()"));
  assert.ok(load.indexOf("removeObject(forKey: Shared.legacyCacheKey)") < load.indexOf("guard let config = Config.load()"));
  assert.match(load, /guard let config = Config\.load\(\) else \{\s*Shared\.defaults\?\.removeObject\(forKey: Shared\.cacheKey\)\s*Shared\.defaults\?\.removeObject\(forKey: Shared\.lockedKey\)/);
  const refresh = java.slice(java.indexOf("private static void refresh"), java.indexOf("static JSONObject keepOnly"));
  assert.ok(refresh.indexOf("remove(KEY_LEGACY_CACHE)") < refresh.indexOf("readConfig(prefs)"));
  assert.match(refresh, /if \(config == null\) \{[\s\S]*?remove\(KEY_CACHE\)\.remove\(KEY_LOCKED\)/);
  assert.match(read(IOS_PLUGIN), /removeObject\(forKey: Self\.legacyCacheKey\)/);
  assert.match(read(`${JAVA}/WidgetBridgePlugin.java`), /remove\(EarliestSlotWidget\.KEY_LEGACY_CACHE\)/);
});

test("a credential with nothing to ask about lifts the lock on both platforms", () => {
  // An empty list (no alert, or only special-category ones) never fetches,
  // so no answer could ever lift a lock set while the credential was missing.
  const swift = read(IOS);
  const java = read(`${JAVA}/EarliestSlotWidget.java`);
  assert.match(swift, /if case \.bearer = credential, cities\.isEmpty \{ nothingToAsk = true \}/);
  assert.match(swift, /let accepted = nothingToAsk \|\| outcomes\.values\.contains/);
  assert.match(java, /boolean nothingToAsk = auth != null && \(cities == null \|\| cities\.length\(\) == 0\);/);
  assert.match(java, /boolean accepted = nothingToAsk;/);
  // And the lock still only stands when nothing proved the credential works.
  assert.match(swift, /let locked = refused \|\| \(!accepted && /);
  assert.match(java, /boolean locked = refused \|\| \(!accepted && /);
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

// --- "Delete my data" during a fetch ---------------------------------------

test("a refresh that outlives a clear() saves nothing: the config is re-read before the cache is written", () => {
  const swift = read(IOS);
  const load = swift.slice(swift.indexOf("private func load()"));
  assert.ok(load.indexOf("Config.load()") < load.indexOf("withTaskGroup"), "config read first");
  const reread = load.indexOf("guard let current = Config.load()");
  assert.ok(reread > load.indexOf("withTaskGroup"), "and again after the fetch");
  assert.ok(reread < load.indexOf("Cache.save(cache)"), "before the save");
  assert.match(load.slice(reread, load.indexOf("Cache.save(cache)")), /removeObject\(forKey: Shared\.cacheKey\)/);
  assert.match(load, /slugs\.contains/);

  const java = read(`${JAVA}/EarliestSlotWidget.java`);
  const refresh = java.slice(java.indexOf("private static void refresh"), java.indexOf("static JSONObject keepOnly"));
  const fetched = refresh.indexOf("futures.get(i).get(");
  const again = refresh.indexOf("readConfig(prefs)", fetched);
  assert.ok(fetched > 0 && again > fetched, "config re-read after the fetch");
  assert.ok(again < refresh.indexOf("putString(KEY_CACHE"), "before the save");
  assert.match(refresh.slice(again), /current == null[\s\S]*remove\(KEY_CACHE\)/);
  assert.match(refresh, /keepOnly\(next, current\)/);
});

// --- The alert's filter, natively ------------------------------------------

test("both platforms mirror app/filters.py matches(): same checks, same bounds", () => {
  const py = readFileSync(new URL("../../app/filters.py", import.meta.url), "utf8");
  // The server's own lines the natives are written from.
  assert.match(py, /f\.locations != "all" and slot\.location_uuid not in f\.locations/);
  assert.match(py, /\(d - today\)\.days > f\.max_days_ahead/);
  assert.match(py, /d\.isoweekday\(\) not in f\.weekdays/);
  assert.match(py, /t < f\.time_window_start or t > f\.time_window_end/);
  assert.match(py, /ZoneInfo\("Europe\/Berlin"\)/);
  const swift = read(IOS);
  const java = read(`${JAVA}/EarliestSlotWidget.java`);
  for (const [name, src] of [["swift", swift], ["java", java]]) {
    assert.match(src, /Europe\/Berlin/, `${name}: Berlin today`);
    assert.match(src, /t >= lo && t <= hi/, `${name}: inclusive time bounds`);
    assert.match(src, /> ?ahead/, `${name}: days ahead is exclusive of the limit itself`);
  }
  assert.match(swift, /wd == 1 \? 7 : wd - 1/);
  assert.match(java, /dow == Calendar\.SUNDAY \? 7 : dow - 1/);
  // Office id, not name: the slots payload carries `location`.
  assert.match(swift, /location: e\.location,/);
  assert.match(java, /e\.optString\("location"\)/);
});

// --- The device credential: secure store, keychain group, widget -----------

test("every SecureStore method the page calls exists on iOS and Android, and both bridges register it", () => {
  const src = read("www/store.js");
  assert.ok(src.includes('plugin("SecureStore")'));
  const called = new Set([...src.matchAll(/\bs\??\.(\w+)\(/g)].map((m) => m[1]));
  assert.deepEqual([...called].sort(), ["clear", "get", "set"]);
  const swift = read(IOS_SECURE);
  const java = read(`${JAVA}/SecureStorePlugin.java`);
  assert.match(swift, /jsName = "SecureStore"/);
  assert.match(java, /@CapacitorPlugin\(name = "SecureStore"\)/);
  for (const name of called) {
    assert.match(swift, new RegExp(`CAPPluginMethod\\(name: "${name}"`), `${name} on iOS`);
    assert.match(swift, new RegExp(`@objc func ${name}\\(_ call: CAPPluginCall\\)`), `${name} implemented on iOS`);
    assert.match(java, new RegExp(`@PluginMethod\\s+public void ${name}\\(PluginCall call\\)`), `${name} on Android`);
  }
  assert.match(read(`${JAVA}/MainActivity.java`), /registerPlugin\(SecureStorePlugin\.class\)/);
  assert.match(read("ios/App/App/MainViewController.swift"), /registerPluginInstance\(SecureStorePlugin\(\)\)/);
  // An app-target file only compiles if the project lists it.
  const pbx = read("ios/App/App.xcodeproj/project.pbxproj");
  assert.match(pbx, /\/\* SecureStorePlugin\.swift in Sources \*\/ = \{isa = PBXBuildFile/);
  assert.match(pbx, /[0-9A-F]{24} \/\* SecureStorePlugin\.swift in Sources \*\/,/);
  assert.match(read("ios/add-widget-target.rb"), /SecureStorePlugin\.swift/);
  // A credential change reloads the widget on both platforms.
  assert.equal((swift.match(/WidgetCenter\.shared\.reloadAllTimelines\(\)/g) ?? []).length, 2, "set and clear");
  assert.equal((java.match(/WidgetBridgePlugin\.requestUpdate\(getContext\(\)\)/g) ?? []).length, 2, "set and clear");
});

test("iOS: one keychain group in both entitlements, one Keychain item in both targets, never backed up or synced", () => {
  assert.equal(KEYCHAIN_GROUP, "de.buergerwecker.shared");
  for (const f of ENTITLEMENTS) {
    assert.match(read(f), new RegExp(`<key>keychain-access-groups</key>\\s*<array>\\s*<string>\\$\\(AppIdentifierPrefix\\)${KEYCHAIN_GROUP.replaceAll(".", "\\.")}</string>\\s*</array>`), f);
  }
  const plugin = read(IOS_SECURE);
  const widget = read(IOS);
  assert.match(plugin, new RegExp(`groupName = "${KEYCHAIN_GROUP}"`));
  const attr = (src, name) => new RegExp(`static let ${name} = "([\\w.]+)"`).exec(src)?.[1];
  for (const name of ["service", "account"]) {
    assert.ok(attr(plugin, name), name);
    assert.equal(attr(widget, name), attr(plugin, name), `${name}: the widget reads what the app writes`);
  }
  // ThisDeviceOnly: not in a backup that restores onto another phone, not in iCloud Keychain.
  assert.match(plugin, /kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly/);
  assert.doesNotMatch(plugin, /kSecAttrAccessible(Always|AfterFirstUnlock\b|WhenUnlocked\b)/);
  for (const src of [plugin, widget]) assert.match(src, /kSecAttrSynchronizable as String: false/);
  assert.match(read("ios/App/App/Info.plist"), /<key>AppIdentifierPrefix<\/key>\s*<string>\$\(AppIdentifierPrefix\)<\/string>/);
  // Nothing of the credential goes through Preferences any more.
  assert.doesNotMatch(read("ios/App/App/PrivacyInfo.xcprivacy"), /keeps the\s+device credentials/);
});

test("Android: an AndroidKeyStore AES-GCM key, and the file it encrypts into is out of backup and transfer", () => {
  const store = read(`${JAVA}/SecureStore.java`);
  assert.match(store, /"AndroidKeyStore"/);
  assert.match(store, /"AES\/GCM\/NoPadding"/);
  assert.match(store, /KeyProperties\.BLOCK_MODE_GCM/);
  assert.doesNotMatch(store, /setUserAuthenticationRequired|setUnlockedDeviceRequired/, "the widget reads it with the screen locked");
  const file = `${/PREFS = "(\w+)"/.exec(store)[1]}.xml`;
  assert.notEqual(file, "CapacitorStorage.xml", "not the Preferences plugin's file");
  const rules = read("android/app/src/main/res/xml/data_extraction_rules.xml");
  for (const section of ["cloud-backup", "device-transfer"]) {
    const body = new RegExp(`<${section}>([\\s\\S]*?)</${section}>`).exec(rules)?.[1] ?? "";
    assert.match(body, new RegExp(`<exclude domain="sharedpref" path="${file.replace(".", "\\.")}" />`), section);
  }
  assert.match(read("android/app/src/main/res/xml/backup_rules.xml"), new RegExp(`<exclude domain="sharedpref" path="${file.replace(".", "\\.")}" />`));
  const manifest = read("android/app/src/main/AndroidManifest.xml");
  assert.match(manifest, /android:allowBackup="false"/);
  assert.match(manifest, /android:dataExtractionRules="@xml\/data_extraction_rules"/);
  assert.match(manifest, /android:fullBackupContent="@xml\/backup_rules"/);
});

test("both widgets fetch with the device credential, and a refused one shows only 'open the app'", () => {
  const swift = read(IOS);
  const java = read(`${JAVA}/EarliestSlotWidget.java`);
  // To buergerwecker.de and nowhere else.
  for (const src of [swift, java]) {
    assert.match(src, /"https:\/\/buergerwecker\.de\/api\/v1"/);
    assert.ok(src.includes('"not_subscribed"'), "a city without an alert drops out");
  }
  assert.match(swift, /req\.setValue\(bearer, forHTTPHeaderField: "Authorization"\)/);
  assert.match(swift, /Credential\.read\(\)/);
  assert.match(swift, /case 401, 403, 410:\s*return \.refused/);
  assert.match(java, /conn\.setRequestProperty\("Authorization", auth\)/);
  assert.match(java, /SecureStore\.bearer\(context\)/);
  assert.match(java, /code == 401 \|\| code == 403 \|\| code == 410\) return new Fetched\(Fetched\.REFUSED/);
  // Locked: the cache goes, the flag stays until an answer proves the credential works.
  assert.match(swift, /if locked \{\s*Shared\.defaults\?\.removeObject\(forKey: Shared\.cacheKey\)\s*Shared\.defaults\?\.set\(true, forKey: Shared\.lockedKey\)/);
  assert.match(java, /else if \(locked\) \{\s*prefs\.edit\(\)\.remove\(KEY_CACHE\)\.putBoolean\(KEY_LOCKED, true\)/);
  assert.match(swift, /if entry\.locked \{\s*Text\(entry\.words\("widget\.reopen"\)\)/);
  assert.match(java, /if \(locked\) \{[\s\S]{0,200}R\.string\.widget_reopen/);
  // Android draws that text from its own resources, in both languages.
  for (const f of ["values", "values-de"]) {
    assert.match(read(`android/app/src/main/res/${f}/widget.xml`), /<string name="widget_reopen">[^<]+<\/string>/, f);
  }
});
