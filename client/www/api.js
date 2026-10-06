// The one place that talks to the network. Everything goes to
// buergerwecker.de and nowhere else: the app never adds a request to a city's
// own site (the booking page opens in the system browser, via the server's
// /go/<slug> redirect).
//
// Requests are the WebView's own fetch (CapacitorHttp is off in
// capacitor.config.json). The page's origin is capacitor://localhost on iOS
// and https://localhost on Android, so every call is cross-origin and works
// only because the server answers CORS for exactly those two origins on every
// /api/v1 response (CORS_ORIGINS in app/api.py). Two things follow:
//   - request headers stay within Accept, Authorization and Content-Type: the
//     server's preflight allows only the last two beyond the safelisted ones,
//     so any other custom header would fail the preflight (a test holds this);
//   - if server.iosScheme / server.androidScheme ever change, the origin
//     changes with them and CORS_ORIGINS must follow, or every call fails.
// A CORS or connection failure surfaces as a rejected fetch (a TypeError, no
// status), which is the "network" error below; an HTTP error from the app is
// a resolved fetch with a non-2xx status. An error the proxies generate
// themselves (Caddy's 502/503 while the container restarts on a deploy, a
// Cloudflare error or challenge page) never reaches the app, so it carries no
// CORS headers and also arrives as "network", not as http_5xx.
//
// Errors are plain objects, { status, error, message }: `error` is the
// server's key ("waitlist_full", "token_in_use" …) or one of ours
// ("network", "timeout", "http_<status>"), `message` the server's own
// sentence in the device's language when it sent one. Nothing is retried
// here; a write that timed out may or may not have happened, and the person
// decides whether to try again.

export const BASE_URL = "https://buergerwecker.de/api/v1";
export const SITE_URL = "https://buergerwecker.de";
export const TIMEOUT_MS = 15_000;

// Turns a non-2xx answer into the error shape. `body` is the parsed JSON, or
// null when there was none (or it was not JSON).
export function toApiError(status, body) {
  if (body && typeof body === "object" && typeof body.error === "string") {
    const err = { status, error: body.error, message: typeof body.message === "string" ? body.message : null };
    if (Number.isFinite(body.limit)) err.limit = body.limit;
    if (Number.isFinite(body.retry_after)) err.retryAfter = body.retry_after;
    return err;
  }
  return { status, error: status ? `http_${status}` : "network", message: null };
}

export const isApiError = (e) => !!e && typeof e === "object" && "status" in e && "error" in e;

// While the server's APP_API_ENABLED gate is closed, every route answers
// 404 {"error": "not_available"}.
export const isUnavailable = (e) => isApiError(e) && e.status === 404 && e.error === "not_available";

// Which i18n key and variables explain an error to a person. The server's
// own sentence wins over our generic one; a few cases have their own wording
// because they ask the person to do something specific.
export function errorText(err) {
  if (!isApiError(err)) return { key: "err.generic", vars: {} };
  switch (err.error) {
    case "waitlist_full":
      return { key: "err.waitlist_full", vars: {} };
    case "too_many_subscriptions":
      return { key: "err.too_many_subscriptions", vars: { limit: err.limit ?? 10 } };
    case "network":
    case "timeout":
      return { key: `err.${err.error}`, vars: {} };
    case "rate_limited":
      return err.message ? { text: err.message } : { key: "err.rate_limited", vars: {} };
    case "not_found":
      return err.message ? { text: err.message } : { key: "err.not_found", vars: {} };
    default:
      return err.message ? { text: err.message } : { key: "err.generic", vars: {} };
  }
}

let credentials = () => null; // → { id, secret } or null
let onUnavailable = () => {};

export function configure({ getCredentials, unavailable } = {}) {
  if (getCredentials) credentials = getCredentials;
  if (unavailable) onUnavailable = unavailable;
}

// request("GET", "/cities?lang=de") → parsed JSON (null for 204).
// `auth: true` adds the device's bearer; without stored credentials the call
// fails as a 401 without touching the network.
export async function request(method, path, { body, auth = false, fetchImpl = globalThis.fetch, timeoutMs = TIMEOUT_MS } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const c = credentials();
    if (!c) throw { status: 401, error: "unauthorized", message: null };
    headers.Authorization = `Bearer ${c.id}.${c.secret}`;
  }
  const ctrl = typeof AbortController === "function" ? new AbortController() : null;
  let timer;
  // The race as well as the abort signal: the timeout also has to cover a
  // body that stalls after the headers arrived, and a fetch stub that ignores
  // the signal.
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      ctrl?.abort();
      reject({ status: 0, error: "timeout", message: null });
    }, timeoutMs);
  });
  let res;
  try {
    res = await Promise.race([
      fetchImpl(BASE_URL + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl?.signal,
      }),
      timeout,
    ]);
  } catch (e) {
    clearTimeout(timer);
    if (isApiError(e)) throw e;
    throw { status: 0, error: "network", message: null };
  }
  let text = "";
  try {
    text = await Promise.race([res.text(), timeout]);
  } catch (e) {
    clearTimeout(timer);
    if (isApiError(e)) throw e;
  }
  clearTimeout(timer);
  let json = null;
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
  }
  if (res.status < 200 || res.status >= 300) {
    const err = toApiError(res.status, json);
    if (isUnavailable(err)) onUnavailable();
    throw err;
  }
  return json;
}

const q = (lang) => `?lang=${encodeURIComponent(lang)}`;

export const api = {
  cities: (lang) => request("GET", `/cities${q(lang)}`),
  city: (slug, lang) => request("GET", `/cities/${encodeURIComponent(slug)}${q(lang)}`),
  slots: (slug, lang) => request("GET", `/cities/${encodeURIComponent(slug)}/slots${q(lang)}`),

  registerDevice: (platform, token, language) =>
    request("POST", "/devices", { body: { platform, token, language } }),
  device: () => request("GET", "/device", { auth: true }),
  updateDevice: (fields) => request("PUT", "/device", { body: fields, auth: true }),
  deleteDevice: () => request("DELETE", "/device", { auth: true }),
  verify: (code) => request("POST", "/device/verify", { body: { code }, auth: true }),
  resendVerification: () => request("POST", "/device/verify/resend", { body: {}, auth: true }),

  subscriptions: () => request("GET", "/subscriptions", { auth: true }),
  createSubscription: (payload) => request("POST", "/subscriptions", { body: payload, auth: true }),
  updateSubscription: (id, payload) => request("PUT", `/subscriptions/${id}`, { body: payload, auth: true }),
  deleteSubscription: (id) => request("DELETE", `/subscriptions/${id}`, { auth: true }),
  renewSubscription: (id) => request("POST", `/subscriptions/${id}/renew`, { body: {}, auth: true }),
};
