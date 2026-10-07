// Play Integrity in the registration flow (push.js) and the Android plugin it
// talks to (IntegrityPlugin.java), against a fake Capacitor and a fake fetch.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const prefs = new Map();
const state = { platform: "android", integrity: async ({ pushToken }) => ({ token: `verdict-for-${pushToken}` }), asked: [] };
globalThis.Capacitor = {
  isNativePlatform: () => true,
  isPluginAvailable: (n) => ["Preferences", "SecureStore"].includes(n) || (n === "Integrity" && state.platform === "android"),
  getPlatform: () => state.platform,
  Plugins: {
    Preferences: {
      get: async ({ key }) => ({ value: prefs.get(key) ?? null }),
      set: async ({ key, value }) => void prefs.set(key, value),
      remove: async ({ key }) => void prefs.delete(key),
      clear: async () => prefs.clear(),
    },
    SecureStore: { get: async () => ({}), set: async () => {}, clear: async () => {} },
    Integrity: {
      token: async (args) => {
        state.asked.push(args);
        return state.integrity(args);
      },
    },
  },
};

const { configure } = await import("../www/api.js");
const push = await import("../www/push.js");
configure({ getCredentials: push.credentials });

let calls = [];
let replyStatus = 201;
globalThis.fetch = async (url, opts) => {
  calls.push({ method: opts.method, path: url.replace(/^https:\/\/buergerwecker\.de\/api\/v1/, ""), body: JSON.parse(opts.body) });
  const body = replyStatus < 300 ? { device_id: 5, secret: "sec", verified: false, language: "de" } : { error: "integrity_missing", message: "Bitte installiere die App aus Google Play." };
  return { status: replyStatus, text: async () => JSON.stringify(body) };
};

beforeEach(async () => {
  prefs.clear();
  state.platform = "android";
  state.asked = [];
  state.integrity = async ({ pushToken }) => ({ token: `verdict-for-${pushToken}` });
  calls = [];
  replyStatus = 201;
  await push.loadDevice();
});

test("Android registration sends the integrity token, minted for that very push token", async () => {
  await push.onToken("fcm-tok-1");
  assert.deepEqual(state.asked, [{ pushToken: "fcm-tok-1" }]);
  assert.deepEqual(calls[0].body, { platform: "fcm", token: "fcm-tok-1", language: "de", integrity_token: "verdict-for-fcm-tok-1" });
});

test("iOS registration sends no integrity token and never asks the plugin", async () => {
  state.platform = "ios";
  await push.onToken("a".repeat(64));
  assert.equal(calls[0].body.platform, "apns");
  assert.ok(!("integrity_token" in calls[0].body));
  assert.deepEqual(state.asked, []);
});

test("Android sends an integrity token with a changed token, for the new one", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "sec", token: "old-tok", platform: "fcm", verified: true }));
  await push.loadDevice();
  await push.onToken("new-tok");
  assert.equal(calls[0].method, "PUT");
  assert.deepEqual(calls[0].body, { token: "new-tok", integrity_token: "verdict-for-new-tok" });
});

test("a plugin that rejects registers without the field and does not crash", async () => {
  state.integrity = async () => {
    throw new Error("Integrity provider could not be prepared");
  };
  await push.onToken("fcm-tok-2");
  assert.ok(!("integrity_token" in calls[0].body));
  assert.equal(push.getDevice().id, 5);
});

test("a server refusal reaches the caller with the server's message", async () => {
  state.integrity = async () => {
    throw new Error("nope");
  };
  replyStatus = 400;
  await assert.rejects(push.onToken("fcm-tok-3"), (e) => e.error === "integrity_missing" && /Google Play/.test(e.message));
});

// --- the plugin, statically ---------------------------------------------
const javaDir = new URL("../android/app/src/main/java/de/buergerwecker/app/", import.meta.url);
const java = readFileSync(new URL("IntegrityPlugin.java", javaDir), "utf8");
const walk = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]));

test("IntegrityPlugin is a Capacitor plugin named Integrity and MainActivity registers it", () => {
  assert.match(java, /@CapacitorPlugin\(name = "Integrity"\)/);
  assert.match(readFileSync(new URL("MainActivity.java", javaDir), "utf8"), /registerPlugin\(IntegrityPlugin\.class\)/);
  assert.match(readFileSync(new URL("../android/app/build.gradle", import.meta.url), "utf8"), /com\.google\.android\.play:integrity:/);
});

test("every Integrity method the page calls is a @PluginMethod in IntegrityPlugin.java", () => {
  const called = new Set();
  for (const f of walk(new URL("../www/", import.meta.url).pathname).filter((f) => f.endsWith(".js"))) {
    for (const m of readFileSync(f, "utf8").matchAll(/plugin\("Integrity"\)\??\.(\w+)/g)) called.add(m[1]);
  }
  assert.ok(called.has("token"), `found the calls: ${[...called]}`);
  for (const name of called) assert.match(java, new RegExp(`@PluginMethod\\s+public void ${name}\\(PluginCall call\\)`), `${name} is not a plugin method`);
});

test("the plugin hashes the UTF-8 push token to lowercase hex and reads the project number from the sender id", () => {
  assert.match(java, /MessageDigest\.getInstance\("SHA-256"\)/);
  assert.match(java, /StandardCharsets\.UTF_8/);
  assert.match(java, /%02x/);
  assert.match(java, /"gcm_defaultSenderId"/);
});
