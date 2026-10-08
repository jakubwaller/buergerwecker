// The notifications this phone received, so they can be read again after the
// system has taken them away: iOS and Android drop one from the notification
// centre when it is tapped, a newer digest for the same alert replaces the
// older one there (app/push.py, collapse_id), and one that arrives while the
// app is open shows only as the in-app banner. Kept on the phone only, in
// Preferences, for MAX_AGE_DAYS; "Delete my data" clears it with the rest.
//
// What reaches the page: a push received in the foreground, a tapped one, and
// on iOS whatever still sits in the notification centre at launch or resume
// (getDeliveredNotifications). Android hands that last call the system's own
// copy without the push's data, so there a notification only lands here when
// it is received in the foreground or tapped; and a tap there carries the data
// without the text, which the list words itself (screens/inbox.js).
import * as store from "./store.js";
import { plugin } from "./native.js";

const KEY = "inbox";
export const MAX_ITEMS = 50;
export const MAX_AGE_DAYS = 30;
// Ids already listed once, kept past the entries themselves: a notification
// still in the notification centre after the list was cleared must not come
// back on the next resume.
const MAX_KNOWN = 200;
const DAY_MS = 86400000;

const empty = () => ({ items: [], seenAt: 0, known: [] });
let box = empty();

const str = (v) => (v == null || v === "" ? null : String(v));

// The entry a notification makes, or null when it does not belong in the list
// (the setup push, anything without our `type`). `at` is when the app first
// saw it, in ms.
export function entryOf(n, at) {
  const data = n?.data ?? {};
  const type = data.type;
  if (type !== "slots" && type !== "checkin") return null;
  return {
    id: str(n.id),
    at,
    type,
    title: str(n.title),
    body: str(n.body),
    city: str(data.city),
    sub: str(data.sub),
  };
}

const sameText = (a, b) => a.title && a.body && a.title === b.title && a.body === b.body;

// `b` with `entry` added (newest first), pruned to MAX_AGE_DAYS and MAX_ITEMS.
// The same notification seen twice (received, then tapped; or found in the
// notification centre on every resume) stays one entry: by id, or, where
// either lacks one, by type, alert and text within a day. The first sighting's
// time stays; text the first one lacked is filled in.
export function add(b, entry, now) {
  const fresh = (e) => now - e.at < MAX_AGE_DAYS * DAY_MS;
  let items = b.items.filter(fresh);
  let known = b.known;
  if (entry) {
    if (entry.id && known.includes(entry.id) && !items.some((e) => e.id === entry.id)) {
      return { ...b, items };
    }
    const i = items.findIndex(
      (e) =>
        (entry.id && e.id === entry.id) ||
        ((!entry.id || !e.id) &&
          e.type === entry.type &&
          e.sub === entry.sub &&
          sameText(e, entry) &&
          Math.abs(e.at - entry.at) < DAY_MS),
    );
    if (i >= 0) {
      const prev = items[i];
      items = [...items];
      items[i] = {
        ...prev,
        id: prev.id ?? entry.id,
        title: prev.title ?? entry.title,
        body: prev.body ?? entry.body,
        city: prev.city ?? entry.city,
      };
    } else {
      items = [entry, ...items].sort((a, c) => c.at - a.at);
    }
    if (entry.id && !known.includes(entry.id)) known = [entry.id, ...known].slice(0, MAX_KNOWN);
  }
  return { ...b, items: items.slice(0, MAX_ITEMS), known };
}

// A stored value back, or as much of it as has the right shape.
export function sanitize(v, now) {
  if (!v || typeof v !== "object") return empty();
  const items = (Array.isArray(v.items) ? v.items : []).filter(
    (e) => e && typeof e === "object" && Number.isFinite(e.at) && (e.type === "slots" || e.type === "checkin"),
  );
  const known = (Array.isArray(v.known) ? v.known : []).filter((k) => typeof k === "string").slice(0, MAX_KNOWN);
  return add({ items, seenAt: Number.isFinite(v.seenAt) ? v.seenAt : 0, known }, null, now);
}

export const items = () => box.items;
export const seenAt = () => box.seenAt;
export const unread = () => box.items.filter((e) => e.at > box.seenAt).length;

// Called after every change, for the tab's unread count.
let changedHook = () => {};
export function onChange(fn) {
  changedHook = fn;
}

async function save() {
  await store.set(KEY, box).catch(() => {});
  changedHook();
}

export async function load(now = Date.now()) {
  box = sanitize(await store.get(KEY).catch(() => null), now);
  return box;
}

// true when the list changed.
export async function record(n, now = Date.now()) {
  const entry = entryOf(n, now);
  if (!entry) return false;
  const before = box;
  box = add(box, entry, now);
  if (JSON.stringify(box.items) === JSON.stringify(before.items)) return false;
  await save();
  return true;
}

// iOS: what still sits in the notification centre. true when any of it is new.
export async function harvest(now = Date.now()) {
  let delivered = [];
  try {
    delivered = (await plugin("PushNotifications")?.getDeliveredNotifications?.())?.notifications ?? [];
  } catch {
    return false;
  }
  let changed = false;
  for (const n of delivered) if (await record(n, now)) changed = true;
  return changed;
}

export async function markRead(now = Date.now()) {
  if (!box.items.some((e) => e.at > box.seenAt)) return;
  box = { ...box, seenAt: now };
  await save();
}

export async function clear(now = Date.now()) {
  box = { ...box, items: [], seenAt: now };
  await save();
}

// After "Delete my data": the store is already empty, so is this.
export function reset() {
  box = empty();
  changedHook();
}
