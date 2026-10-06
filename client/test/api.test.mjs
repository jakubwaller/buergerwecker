import { test } from "node:test";
import assert from "node:assert/strict";
import { toApiError, errorText, request, configure, isUnavailable, BASE_URL } from "../www/api.js";

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

test("what a person reads: own wording for the cases that ask something of them", () => {
  assert.deepEqual(errorText({ status: 503, error: "waitlist_full", message: "x" }), { key: "err.waitlist_full", vars: {} });
  assert.deepEqual(errorText({ status: 409, error: "too_many_subscriptions", message: null, limit: 10 }), {
    key: "err.too_many_subscriptions", vars: { limit: 10 },
  });
  assert.deepEqual(errorText({ status: 0, error: "timeout", message: null }), { key: "err.timeout", vars: {} });
  assert.deepEqual(errorText({ status: 400, error: "invalid_time", message: "Ungültige Uhrzeit." }), { text: "Ungültige Uhrzeit." });
  assert.deepEqual(errorText({ status: 400, error: "invalid_time", message: null }), { key: "err.generic", vars: {} });
  assert.deepEqual(errorText(new Error("boom")), { key: "err.generic", vars: {} });
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
