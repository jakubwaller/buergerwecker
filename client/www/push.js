// Notifications and the device's registration with buergerwecker.de.
//
// The device is the subscriber: no account, no address. On every launch with
// permission the app asks the OS for its push token; the first time, it
// registers that token (POST /devices) and keeps the returned id and secret in
// the secure store (store.js: the Keychain on iOS, the AndroidKeyStore on
// Android), never in Preferences and never logged; the rest of the record
// (token, platform, verified) stays in Preferences. A rotated token is sent with
// PUT /device. A 401 or 410 device_retired drops the stored device and
// registers afresh: the same token is the same device on the server, so its
// subscriptions carry over (a retired one's have ended with it).
//
// Verification: the server treats a device as real only once a push has
// reached it. Registration answers "verified": false, and every
// authenticated route but GET /device and the two verify routes answers 403
// device_unverified until then. The server sends {type: "verify", code}
// within seconds; the app posts the code back (a used code again is still a
// 200, so a retry is harmless). A server without these routes answers 404 on
// them and reports no `verified` at all, which here means "not yet", never an
// error: the device then counts as verified for the app's purposes.
import { api, isApiError } from "./api.js";
import * as store from "./store.js";
import { plugin, platform } from "./native.js";

const DEVICE_KEY = "device";

let device = null; // { id, secret, token, platform, verified: true|false|null }
let verifySeq = 0; // counts accepted verify codes (onToken's race with the push)
let lang = () => "de";
let handlers = { changed() {}, error() {}, received() {}, tapped() {} };

export const getDevice = () => device;
export const credentials = () => (device ? { id: device.id, secret: device.secret } : null);

// The record in Preferences ({ token, platform, verified }) and the credential
// in the secure store, put back together. Moves a credential an older build
// left in Preferences first (store.migrateCredential).
export async function loadDevice() {
  const fallback = await store.migrateCredential(DEVICE_KEY).catch(() => null);
  const record = await store.get(DEVICE_KEY);
  let cred = fallback ?? (await store.getCredential().catch(() => null));
  if (!record || typeof record !== "object") {
    // A credential with no record beside it belongs to no install here: iOS
    // keeps Keychain items when an app is deleted, so a reinstall finds the
    // previous install's. A reinstall has always started afresh (the same
    // token registers as the same device on the server), and still does.
    if (cred) await store.removeCredential().catch(() => {});
    cred = null;
  }
  device = record && cred ? { ...record, id: cred.id, secret: cred.secret } : null;
  return device;
}

async function saveDevice(d) {
  device = d;
  if (d) {
    await store.setCredential({ id: d.id, secret: d.secret });
    const { id, secret, ...record } = d;
    await store.set(DEVICE_KEY, record);
  } else {
    await store.removeCredential().catch(() => {});
    await store.remove(DEVICE_KEY);
  }
  handlers.changed(device);
}

const pushPlugin = () => plugin("PushNotifications");

// False on the web, and on an Android build made without google-services.json
// (PushGatePlugin.java), where register() would crash the app.
export async function pushSupported() {
  if (!pushPlugin()) return false;
  const gate = plugin("PushGate");
  if (gate) {
    try {
      return !!(await gate.status()).available;
    } catch {
      return false;
    }
  }
  return true;
}

const permissionOf = (r) =>
  r?.receive === "granted" ? "granted" : r?.receive === "denied" ? "denied" : "prompt";

// "granted" | "denied" | "prompt" | "unsupported"
export async function permission() {
  if (!(await pushSupported())) return "unsupported";
  return permissionOf(await pushPlugin().checkPermissions());
}

export async function requestPermission() {
  if (!(await pushSupported())) return "unsupported";
  return permissionOf(await pushPlugin().requestPermissions());
}

// Listeners first, before anything is awaited: a tap that launched the app is
// held by the plugin until someone listens, and the token can arrive fast.
export function init({ getLang, ...h }) {
  if (getLang) lang = getLang;
  handlers = { ...handlers, ...h };
  const P = pushPlugin();
  if (!P) return;
  P.addListener("registration", (t) => {
    onToken(t.value).catch((e) => handlers.error(e));
  });
  P.addListener("registrationError", (e) => handlers.error({ status: 0, error: "registration", message: e?.error ?? null }));
  P.addListener("pushNotificationReceived", (n) => handlers.received(n));
  P.addListener("pushNotificationActionPerformed", (a) => handlers.tapped(a.notification));
  if (platform() === "android") {
    // The server sends FCM pushes with channel_id "slots".
    P.createChannel({
      id: "slots",
      name: h.channelName ?? "Free slots",
      description: h.channelDescription ?? "",
      importance: 4,
      visibility: 1,
    }).catch(() => {});
  }
}

// Asks the OS for the token; it arrives on the `registration` listener.
export async function register() {
  if (!(await pushSupported())) return;
  await pushPlugin().register();
}

const platformKey = () => (platform() === "ios" ? "apns" : "fcm");

// Android only: the server wants a Play Integrity verdict bound to the token
// it is about to register (app/integrity.py). When the plugin cannot produce
// one (a build without google-services.json, a phone without Play Services)
// the field is left out and the server answers integrity_missing, which the
// usual error path shows; this never throws.
async function integrityToken(token) {
  if (platform() !== "android") return undefined;
  try {
    const r = await plugin("Integrity")?.token({ pushToken: token });
    return typeof r?.token === "string" && r.token ? r.token : undefined;
  } catch {
    return undefined;
  }
}

async function registerFresh(token) {
  const r = await api.registerDevice(platformKey(), token, lang(), await integrityToken(token));
  await saveDevice({
    id: r.device_id,
    secret: r.secret,
    token,
    platform: platformKey(),
    verified: typeof r.verified === "boolean" ? r.verified : null,
  });
}

// The token from the OS: register when there is no device yet, send it on when
// it changed, nothing when it is the one already on file.
export async function onToken(token) {
  if (!token) return;
  if (!device) return registerFresh(token);
  if (device.token === token) return;
  try {
    const seq = verifySeq;
    const r = await api.updateDevice({ token, integrity_token: await integrityToken(token) });
    // A changed token is unverified again until the setup push sent to it
    // comes back; the waiting screen shows meanwhile. Should that push have
    // been posted already while this request was in flight, it stays verified.
    let verified = device.verified;
    if (typeof r?.verified === "boolean") verified = r.verified || verifySeq !== seq;
    await saveDevice({ ...device, token, verified });
  } catch (e) {
    // token_in_use: the token already belongs to a device row, which a fresh
    // POST /devices takes over. 401/410: this device is gone. 403: an
    // unverified device cannot change its token, so the new token registers
    // as a device of its own and is verified by its own setup push.
    if (isApiError(e) && (e.error === "token_in_use" || e.error === "device_unverified" || e.status === 401 || (e.status === 410 && e.error === "device_retired"))) {
      return registerFresh(token);
    }
    throw e;
  }
}

const needsFreshDevice = (e) =>
  isApiError(e) && (e.status === 401 || (e.status === 410 && e.error === "device_retired"));

export const isUnverified = (e) => isApiError(e) && e.status === 403 && e.error === "device_unverified";

// True while the server waits for this device's setup push: the app shows
// only the waiting screen then (screens/verify.js).
export const awaitingVerification = () => device?.verified === false;

// A 404 from a route the server does not have yet, as opposed to the closed
// APP_API_ENABLED gate (404 not_available), which is handled in api.js.
const notYet = (e) => isApiError(e) && e.status === 404 && e.error !== "not_available";

// Runs an authenticated call; on 401 or 410 device_retired, registers the
// stored token afresh and runs it once more (the first attempt was refused
// outright, so nothing happened twice). A 403 device_unverified marks the
// device as waiting for its setup push, which brings up the waiting screen.
export async function authed(call) {
  try {
    return await call();
  } catch (e) {
    if (isUnverified(e) && device && device.verified !== false) await saveDevice({ ...device, verified: false });
    if (!needsFreshDevice(e) || !device) throw e;
    const token = device.token;
    await saveDevice(null);
    if (!token) throw e;
    await registerFresh(token);
    return call();
  }
}

// GET /device: whether the server counts this device as verified. Read on
// every launch, and on resume while the waiting screen is up. `verified`
// stays null against a server that does not report it.
export async function refreshStatus() {
  if (!device) return device;
  const d = await authed(() => api.device());
  const verified = typeof d?.verified === "boolean" ? d.verified : null;
  if (verified !== device.verified) await saveDevice({ ...device, verified });
  return device;
}

// The code from a {type: "verify"} push, whenever one arrives: after
// registration, after a token rotation (PUT /device answers verified: false
// and a push to the new token follows), after a reinstall. true when the
// server took it; false
// on a server without the route (404) or with no device. A wrong or expired
// code throws {error: "invalid_code"}, and the waiting screen offers Resend.
export async function verify(code) {
  if (!device || !code) return false;
  let r;
  try {
    r = await authed(() => api.verify(String(code)));
  } catch (e) {
    if (notYet(e)) return false;
    throw e;
  }
  if (r && r.verified === false) return false;
  verifySeq++;
  await saveDevice({ ...device, verified: true });
  return true;
}

// Asks the server to send the setup push again: 202 {verified: false} when
// sent, 200 {verified: true} when the device already is (then the app just
// carries on), 429 rate_limited with retry_after (thrown, for the screen to
// wait that long). Returns { sent, verified }.
export async function resendVerification() {
  let r;
  try {
    r = await authed(() => api.resendVerification());
  } catch (e) {
    if (notYet(e)) return { sent: false, verified: null };
    throw e;
  }
  if (r?.verified === true) {
    await saveDevice({ ...device, verified: true });
    return { sent: false, verified: true };
  }
  return { sent: true, verified: false };
}

export async function updateLanguage(language) {
  if (!device) return;
  await authed(() => api.updateDevice({ language }));
}

// "Delete my data": the server's row and every subscription with it, then
// everything this app kept. An already-gone device (401/410) is as deleted
// as it gets.
export async function deleteEverything() {
  if (device) {
    try {
      await api.deleteDevice();
    } catch (e) {
      if (!needsFreshDevice(e)) throw e;
    }
  }
  device = null;
  // Everything this app kept, except the language the person chose: deleting
  // their data is no reason to switch the app to another language. The
  // credential first: it is the part that must not outlive this. Should the
  // secure store fail to delete it, it is dead on the server already, and the
  // next launch drops it anyway (no record beside it, loadDevice).
  await store.removeCredential().catch(() => {});
  const lang = await store.get("lang");
  await store.clear();
  if (lang) await store.set("lang", lang);
  try {
    await pushPlugin()?.removeAllDeliveredNotifications?.();
  } catch {
    /* nothing to clear */
  }
}
