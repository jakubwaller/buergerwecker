// What the app keeps on the phone. Two places, on purpose:
//
//  - Small persisted values (the language, "onboarded", the device record
//    without its credential) through the Preferences plugin: UserDefaults on
//    iOS, SharedPreferences on Android. Those go into iCloud and Finder
//    backups, so nothing in them may be a secret.
//  - The device credential ({ id, secret }, the Bearer every authenticated call
//    sends) through the app's own SecureStore plugin: the Keychain on iOS
//    (AfterFirstUnlockThisDeviceOnly, never synchronised, in the keychain
//    access group the widget extension shares, App/SecureStorePlugin.swift),
//    and on Android a value encrypted with an AES-GCM key that never leaves
//    the AndroidKeyStore, in a preference file kept out of backup and device
//    transfer (SecureStore.java, res/xml/data_extraction_rules.xml).
//
// Outside the app (a desktop browser while developing) both are kept in memory
// only, never in localStorage.
import { plugin } from "./native.js";

const memory = new Map();

export async function get(key) {
  const p = plugin("Preferences");
  const raw = p ? (await p.get({ key })).value : memory.get(key) ?? null;
  if (raw == null) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export async function set(key, value) {
  const raw = JSON.stringify(value);
  const p = plugin("Preferences");
  if (p) await p.set({ key, value: raw });
  else memory.set(key, raw);
}

export async function remove(key) {
  const p = plugin("Preferences");
  if (p) await p.remove({ key });
  else memory.delete(key);
}

export async function clear() {
  const p = plugin("Preferences");
  if (p) await p.clear();
  else memory.clear();
}

// --- The device credential ---------------------------------------------------

const secure = () => plugin("SecureStore");
let memoryCredential = null;
// The JSON text the secure store holds as far as this launch knows: a write of
// the same value is skipped, because every write also reloads the widget.
let known;

// { id, secret } from the stored JSON text, or null for anything else.
export function parseCredential(raw) {
  if (typeof raw !== "string" || !raw) return null;
  let c;
  try {
    c = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!c || typeof c !== "object") return null;
  const idOk = (typeof c.id === "number" && Number.isFinite(c.id)) || (typeof c.id === "string" && c.id !== "");
  if (!idOk || typeof c.secret !== "string" || !c.secret) return null;
  return { id: c.id, secret: c.secret };
}

export async function getCredential() {
  const s = secure();
  const raw = s ? ((await s.get())?.value ?? null) : memoryCredential;
  known = raw;
  return parseCredential(raw);
}

export async function setCredential({ id, secret }) {
  const raw = JSON.stringify({ id, secret });
  if (raw === known) return;
  const s = secure();
  known = undefined; // unknown until the write is through
  if (s) await s.set({ value: raw });
  else memoryCredential = raw;
  known = raw;
}

export async function removeCredential() {
  const s = secure();
  known = undefined;
  if (s) await s.clear();
  else memoryCredential = null;
  known = null;
}

// Builds before this one kept the credential inside the Preferences record
// `key` ({ id, secret, token, platform, verified }), which is in every backup.
// Moves it into the secure store, reads it back, and only once it is there
// takes id and secret out of the record. Should the secure store refuse, the
// record stays as it was, for the next launch to try again, and the credential
// is returned for this launch to use; otherwise null.
export async function migrateCredential(key) {
  const rec = await get(key);
  if (!rec || typeof rec !== "object" || !("id" in rec || "secret" in rec)) return null;
  const { id, secret, ...rest } = rec;
  const legacy = parseCredential(JSON.stringify({ id, secret }));
  if (legacy) {
    try {
      await setCredential(legacy);
      const back = await getCredential();
      if (!back || back.id !== legacy.id || back.secret !== legacy.secret) return legacy;
    } catch {
      return legacy;
    }
  }
  await set(key, rest);
  return null;
}
