// The device registration and verification rules in push.js, against a fake
// Capacitor and a fake fetch. Nothing here touches the network.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

const prefs = new Map();
globalThis.Capacitor = {
  isNativePlatform: () => true,
  isPluginAvailable: (n) => n === "Preferences",
  getPlatform: () => "ios",
  Plugins: {
    Preferences: {
      get: async ({ key }) => ({ value: prefs.get(key) ?? null }),
      set: async ({ key, value }) => void prefs.set(key, value),
      remove: async ({ key }) => void prefs.delete(key),
      clear: async () => prefs.clear(),
    },
  },
};

const { configure } = await import("../www/api.js");
const push = await import("../www/push.js");
configure({ getCredentials: push.credentials });

let routes = [];
let calls = [];
globalThis.fetch = async (url, opts) => {
  const path = url.replace(/^https:\/\/buergerwecker\.de\/api\/v1/, "");
  calls.push({ method: opts.method, path, body: opts.body ? JSON.parse(opts.body) : undefined, auth: opts.headers.Authorization });
  const r = routes.find((r) => r.method === opts.method && r.path === path);
  if (!r) return { status: 404, text: async () => "" };
  const out = typeof r.reply === "function" ? r.reply(calls.at(-1)) : r.reply;
  return { status: out[0], text: async () => (out[1] === undefined ? "" : JSON.stringify(out[1])) };
};

beforeEach(async () => {
  prefs.clear();
  routes = [];
  calls = [];
  await push.loadDevice();
});

test("first token registers the device and keeps id and secret in Preferences", async () => {
  routes = [{ method: "POST", path: "/devices", reply: [201, { device_id: 5, secret: "sec", language: "de", verified: false }] }];
  await push.onToken("tok-1");
  assert.deepEqual(calls[0].body, { platform: "apns", token: "tok-1", language: "de" });
  assert.equal(push.getDevice().id, 5);
  assert.equal(push.awaitingVerification(), true);
  assert.deepEqual(JSON.parse(prefs.get("device")), { id: 5, secret: "sec", token: "tok-1", platform: "apns", verified: false });
});

test("a server without verification leaves verified unknown, which is not waiting", async () => {
  routes = [{ method: "POST", path: "/devices", reply: [201, { device_id: 5, secret: "sec", language: "de" }] }];
  await push.onToken("tok-1");
  assert.equal(push.getDevice().verified, null);
  assert.equal(push.awaitingVerification(), false);
});

test("the same token does nothing, a rotated one is sent with PUT /device", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "sec", token: "tok-1", platform: "apns", verified: true }));
  await push.loadDevice();
  await push.onToken("tok-1");
  assert.equal(calls.length, 0);
  routes = [{ method: "PUT", path: "/device", reply: [200, { device_id: 5 }] }];
  await push.onToken("tok-2");
  assert.deepEqual(calls[0], { method: "PUT", path: "/device", body: { token: "tok-2" }, auth: "Bearer 5.sec" });
  assert.equal(push.getDevice().token, "tok-2");
});

test("409 token_in_use registers afresh", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "sec", token: "tok-1", platform: "apns", verified: true }));
  await push.loadDevice();
  routes = [
    { method: "PUT", path: "/device", reply: [409, { error: "token_in_use" }] },
    { method: "POST", path: "/devices", reply: [201, { device_id: 9, secret: "new", verified: true }] },
  ];
  await push.onToken("tok-2");
  assert.equal(push.getDevice().id, 9);
  assert.equal(push.getDevice().token, "tok-2");
});

test("401 or 410 device_retired: drop, register the same token again, retry once", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "old", token: "tok-1", platform: "apns", verified: true }));
  await push.loadDevice();
  let n = 0;
  routes = [
    { method: "GET", path: "/subscriptions", reply: () => (n++ === 0 ? [410, { error: "device_retired" }] : [200, { subscriptions: [] }]) },
    { method: "POST", path: "/devices", reply: [201, { device_id: 6, secret: "fresh", verified: true }] },
  ];
  const { api } = await import("../www/api.js");
  assert.deepEqual(await push.authed(() => api.subscriptions()), { subscriptions: [] });
  assert.deepEqual(calls.map((c) => `${c.method} ${c.path}`), ["GET /subscriptions", "POST /devices", "GET /subscriptions"]);
  assert.equal(calls[1].body.token, "tok-1");
  assert.equal(calls[2].auth, "Bearer 6.fresh");
});

test("403 device_unverified brings the waiting state back", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "sec", token: "tok-1", platform: "apns", verified: true }));
  await push.loadDevice();
  routes = [{ method: "GET", path: "/subscriptions", reply: [403, { error: "device_unverified", message: "…" }] }];
  const { api } = await import("../www/api.js");
  await assert.rejects(push.authed(() => api.subscriptions()), { status: 403 });
  assert.equal(push.awaitingVerification(), true);
});

test("verify: 200 verifies, 404 is 'not yet', invalid_code is thrown", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "sec", token: "tok-1", platform: "apns", verified: false }));
  await push.loadDevice();
  assert.equal(await push.verify("abc"), false, "no route yet");
  assert.equal(push.awaitingVerification(), true);
  routes = [{ method: "POST", path: "/device/verify", reply: (c) => (c.body.code === "good" ? [200, { verified: true }] : [400, { error: "invalid_code" }]) }];
  await assert.rejects(push.verify("bad"), { error: "invalid_code" });
  assert.equal(await push.verify("good"), true);
  assert.equal(push.awaitingVerification(), false);
  assert.equal(JSON.parse(prefs.get("device")).verified, true);
});

test("resend: 202 sent, 200 already verified, 429 carries retry_after", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "sec", token: "tok-1", platform: "apns", verified: false }));
  await push.loadDevice();
  routes = [{ method: "POST", path: "/device/verify/resend", reply: [202, { verified: false }] }];
  assert.deepEqual(await push.resendVerification(), { sent: true, verified: false });
  routes = [{ method: "POST", path: "/device/verify/resend", reply: [429, { error: "rate_limited", retry_after: 37 }] }];
  await assert.rejects(push.resendVerification(), { error: "rate_limited", retryAfter: 37 });
  routes = [{ method: "POST", path: "/device/verify/resend", reply: [200, { verified: true }] }];
  assert.deepEqual(await push.resendVerification(), { sent: false, verified: true });
  assert.equal(push.awaitingVerification(), false);
});

test("GET /device on launch picks up a flipped flag", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "sec", token: "tok-1", platform: "apns", verified: true }));
  await push.loadDevice();
  routes = [{ method: "GET", path: "/device", reply: [200, { device_id: 5, verified: false, subscriptions: [] }] }];
  await push.refreshStatus();
  assert.equal(push.awaitingVerification(), true);
});

test("a rotated token is unverified until the push sent to it comes back", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "sec", token: "tok-1", platform: "apns", verified: true }));
  await push.loadDevice();
  routes = [
    { method: "PUT", path: "/device", reply: [200, { device_id: 5, verified: false }] },
    { method: "POST", path: "/device/verify", reply: [200, { verified: true }] },
  ];
  await push.onToken("tok-2");
  assert.equal(push.awaitingVerification(), true);
  assert.equal(await push.verify("code-for-tok-2"), true);
  assert.equal(push.awaitingVerification(), false);
});

test("a verify push that lands while PUT /device is in flight is not undone", async () => {
  prefs.set("device", JSON.stringify({ id: 5, secret: "sec", token: "tok-1", platform: "apns", verified: true }));
  await push.loadDevice();
  let release;
  const gate = new Promise((r) => (release = r));
  routes = [{ method: "POST", path: "/device/verify", reply: [200, { verified: true }] }];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    if (opts.method === "PUT") {
      await gate;
      return { status: 200, text: async () => JSON.stringify({ device_id: 5, verified: false }) };
    }
    return realFetch(url, opts);
  };
  try {
    const rotating = push.onToken("tok-2");
    await push.verify("code");
    release();
    await rotating;
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(push.awaitingVerification(), false);
  assert.equal(push.getDevice().token, "tok-2");
});
