import { test } from "node:test";
import assert from "node:assert/strict";
import { toApiError, errorText, request, configure, isUnavailable, siteUrl, api, BASE_URL } from "../www/api.js";
import { STRINGS } from "../www/i18n.js";

const res = (status, body) => ({ status, text: async () => (body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body)) });

test("an error answer keeps the server's key and sentence", () => {
  assert.deepEqual(toApiError(400, { error: "unknown_type", message: "Unbekanntes Anliegen." }), {
    status: 400, error: "unknown_type", message: "Unbekanntes Anliegen.",
  });
  assert.deepEqual(toApiError(409, { error: "too_many_subscriptions", limit: 10 }), {
    status: 409, error: "too_many_subscriptions", message: null, limit: 10,
  });
  assert.deepEqual(toApiError(429, { error: "rate_limited", retry_after: 42 }), {
    status: 429, error: "rate_limited", message: null, retryAfter: 42,
  });
  assert.deepEqual(toApiError(502, null), { status: 502, error: "http_502", message: null });
  assert.deepEqual(toApiError(500, "<html>"), { status: 500, error: "http_500", message: null });
});

test("what a person reads: the server's sentence whenever there is one", () => {
  // The waitlist has causes the app cannot tell apart (the city's plan, a
  // per-device or per-city app limit): the server's sentence names the one.
  assert.deepEqual(errorText({ status: 503, error: "waitlist_full", message: "Dieses Gerät hat schon …" }), { text: "Dieses Gerät hat schon …" });
  assert.deepEqual(errorText({ status: 409, error: "too_many_subscriptions", message: "Schon 10.", limit: 10 }), { text: "Schon 10." });
  assert.deepEqual(errorText({ status: 413, error: "too_large", message: "Die Anfrage ist zu groß." }), { text: "Die Anfrage ist zu groß." });
  assert.deepEqual(errorText({ status: 429, error: "rate_limited", message: "Heute schon zu viele Geräte.", retryAfter: 3600 }), {
    text: "Heute schon zu viele Geräte.",
  });
  assert.deepEqual(errorText({ status: 415, error: "unsupported_media_type", message: "Nur JSON." }), { text: "Nur JSON." });
  assert.deepEqual(errorText({ status: 400, error: "invalid_token", message: "Kein Push-Token." }), { text: "Kein Push-Token." });
  assert.deepEqual(errorText({ status: 400, error: "invalid_time", message: "Ungültige Uhrzeit." }), { text: "Ungültige Uhrzeit." });
});

test("what a person reads without one: our own fallback per key, else the generic sentence", () => {
  assert.deepEqual(errorText({ status: 503, error: "waitlist_full", message: null }), { key: "err.waitlist_full", vars: {} });
  assert.deepEqual(errorText({ status: 409, error: "too_many_subscriptions", message: null, limit: 10 }), {
    key: "err.too_many_subscriptions", vars: { limit: 10 },
  });
  assert.deepEqual(errorText({ status: 0, error: "timeout", message: null }), { key: "err.timeout", vars: {} });
  assert.deepEqual(errorText({ status: 0, error: "network", message: null }), { key: "err.network", vars: {} });
  assert.deepEqual(errorText({ status: 429, error: "rate_limited", message: null, retryAfter: 60 }), { key: "err.rate_limited", vars: {} });
  assert.deepEqual(errorText({ status: 413, error: "too_large", message: null }), { key: "err.too_large", vars: {} });
  assert.deepEqual(errorText({ status: 403, error: "not_subscribed", message: null }), { key: "err.not_subscribed", vars: {} });
  assert.deepEqual(errorText({ status: 415, error: "http_415", message: null }), { key: "err.generic", vars: {} });
  assert.deepEqual(errorText({ status: 400, error: "invalid_time", message: "  " }), { key: "err.generic", vars: {} });
  assert.deepEqual(errorText({ status: 400, error: "invalid_time", message: null }), { key: "err.generic", vars: {} });
  assert.deepEqual(errorText(new Error("boom")), { key: "err.generic", vars: {} });
  for (const k of ["err.waitlist_full", "err.rate_limited", "err.too_large", "err.not_subscribed", "err.network", "err.timeout"]) {
    assert.ok(STRINGS.de[k] && STRINGS.en[k], k);
  }
});

test("every request with a body says it is JSON, and every API write has one", async () => {
  configure({ getCredentials: () => ({ id: 7, secret: "s3cret" }) });
  const seen = [];
  const fetchImpl = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    seen.push({ url, method: opts.method, headers: opts.headers, body: opts.body });
    return res(200, {});
  };
  try {
    await api.registerDevice("apns", "tok", "de");
    await api.updateDevice({ language: "en" });
    await api.verify("code");
    await api.resendVerification();
    await api.createSubscription({ city: "leipzig" });
    await api.updateSubscription(1, { city: "leipzig" });
    await api.renewSubscription(1);
    await api.deleteSubscription(1);
    await api.deleteDevice();
    await api.subscriptions();
  } finally {
    globalThis.fetch = fetchImpl;
  }
  const writes = seen.filter((s) => s.method === "POST" || s.method === "PUT");
  assert.equal(writes.length, 7);
  for (const s of writes) {
    assert.equal(s.headers["Content-Type"], "application/json", `${s.method} ${s.url}`);
    assert.doesNotThrow(() => JSON.parse(s.body), `${s.method} ${s.url} sends JSON`);
  }
  for (const s of seen.filter((s) => s.body === undefined)) assert.equal(s.headers["Content-Type"], undefined, s.url);
});

test("the slots route sends the device credential and passes not_subscribed on as itself", async () => {
  configure({ getCredentials: () => ({ id: 7, secret: "s3cret" }) });
  const fetchImpl = globalThis.fetch;
  let seen;
  globalThis.fetch = async (url, opts) => {
    seen = { url, opts };
    return res(403, { error: "not_subscribed", message: "Für diese Stadt hast du keinen Alarm." });
  };
  try {
    await assert.rejects(api.slots("leipzig", "de"), { status: 403, error: "not_subscribed", message: "Für diese Stadt hast du keinen Alarm." });
  } finally {
    globalThis.fetch = fetchImpl;
  }
  assert.equal(seen.url, `${BASE_URL}/cities/leipzig/slots?lang=de`);
  assert.equal(seen.opts.headers.Authorization, "Bearer 7.s3cret");
  // No device yet: a local 401, nothing sent.
  configure({ getCredentials: () => null });
  let sent = false;
  globalThis.fetch = async () => { sent = true; return res(200, {}); };
  try {
    await assert.rejects(api.slots("leipzig", "de"), { status: 401 });
  } finally {
    globalThis.fetch = fetchImpl;
  }
  assert.equal(sent, false);
});

test("a push's url opens only on https://buergerwecker.de itself", () => {
  assert.equal(siteUrl("https://buergerwecker.de/go/leipzig"), "https://buergerwecker.de/go/leipzig");
  assert.equal(siteUrl("https://buergerwecker.de/go/sub/abc?lang=en"), "https://buergerwecker.de/go/sub/abc?lang=en");
  assert.equal(siteUrl("https://BUERGERWECKER.de:443/go/bonn"), "https://buergerwecker.de/go/bonn", "the same origin, normalised");
  for (const bad of [
    "http://buergerwecker.de/go/leipzig",
    "https://buergerwecker.de.example.com/go/leipzig",
    "https://example.com/?https://buergerwecker.de/",
    "https://www.buergerwecker.de/go/leipzig",
    "https://buergerwecker.de:8443/go/leipzig",
    "https://user:pw@buergerwecker.de/go/leipzig",
    "https://buergerwecker.de@example.com/",
    "//buergerwecker.de/go/leipzig",
    "/go/leipzig",
    "javascript:alert(1)",
    "data:text/html,hi",
    "buergerwecker://",
    "",
    null,
    undefined,
    42,
    { href: "https://buergerwecker.de/" },
  ]) {
    assert.equal(siteUrl(bad), null, String(bad));
  }
});

test("request: JSON in and out, bearer only when asked, 204 is null", async () => {
  configure({ getCredentials: () => ({ id: 7, secret: "s3cret" }) });
  const seen = [];
  const fetchImpl = async (url, opts) => {
    seen.push({ url, opts });
    return opts.method === "DELETE" ? res(204) : res(201, { id: 1 });
  };
  assert.deepEqual(await request("POST", "/subscriptions", { body: { a: 1 }, auth: true, fetchImpl }), { id: 1 });
  assert.equal(seen[0].url, `${BASE_URL}/subscriptions`);
  assert.equal(seen[0].opts.headers.Authorization, "Bearer 7.s3cret");
  assert.equal(seen[0].opts.headers["Content-Type"], "application/json");
  assert.equal(seen[0].opts.body, '{"a":1}');
  assert.equal(await request("DELETE", "/subscriptions/1", { auth: true, fetchImpl }), null);
  await request("GET", "/cities?lang=de", { fetchImpl });
  assert.equal(seen[2].opts.headers.Authorization, undefined);
});

test("request: an error answer, the closed API gate, a dead network, a timeout", async () => {
  let gateClosed = 0;
  configure({ unavailable: () => gateClosed++ });
  await assert.rejects(request("GET", "/x", { fetchImpl: async () => res(503, { error: "waitlist_full" }) }), {
    status: 503, error: "waitlist_full",
  });
  const err = await request("GET", "/x", { fetchImpl: async () => res(404, { error: "not_available" }) }).catch((e) => e);
  assert.ok(isUnavailable(err));
  assert.equal(gateClosed, 1);
  await assert.rejects(request("GET", "/x", { fetchImpl: async () => { throw new TypeError("offline"); } }), {
    status: 0, error: "network",
  });
  let calls = 0;
  await assert.rejects(
    request("POST", "/x", { body: {}, timeoutMs: 20, fetchImpl: () => { calls++; return new Promise(() => {}); } }),
    { status: 0, error: "timeout" },
  );
  assert.equal(calls, 1, "a write is never retried");
});

test("request: no credentials means a local 401, nothing sent", async () => {
  configure({ getCredentials: () => null });
  let sent = false;
  await assert.rejects(request("GET", "/subscriptions", { auth: true, fetchImpl: async () => { sent = true; return res(200, {}); } }), {
    status: 401,
  });
  assert.equal(sent, false);
});

test("CapacitorHttp stays off: the page's fetch is the WebView's, CORS comes from the server", async () => {
  const { readFileSync } = await import("node:fs");
  const cfg = JSON.parse(readFileSync(new URL("../capacitor.config.json", import.meta.url), "utf8"));
  assert.equal(cfg.plugins.CapacitorHttp.enabled, false);
  // CORS_ORIGINS in app/api.py is derived from the default schemes.
  assert.equal(cfg.server?.iosScheme, undefined, "a custom iosScheme changes the origin: update CORS_ORIGINS");
  assert.equal(cfg.server?.androidScheme, undefined, "a custom androidScheme changes the origin: update CORS_ORIGINS");
});

test("request headers stay within what the server's CORS preflight allows", async () => {
  const allowed = new Set(["accept", "authorization", "content-type"]);
  configure({ getCredentials: () => ({ id: 1, secret: "s" }) });
  const seen = [];
  const fetchImpl = async (_url, opts) => { seen.push(Object.keys(opts.headers)); return res(200, {}); };
  await request("GET", "/x", { fetchImpl });
  await request("POST", "/x", { body: { a: 1 }, auth: true, fetchImpl });
  await request("DELETE", "/x", { auth: true, fetchImpl });
  assert.equal(seen.length, 3);
  for (const names of seen) for (const n of names) assert.ok(allowed.has(n.toLowerCase()), `header ${n} would fail CORS preflight`);
});
