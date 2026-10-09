// The notifications list (www/inbox.js): what goes in, what counts as the
// same notification, and how long it stays. Against a fake Capacitor.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

const prefs = new Map();
const delivered = { list: [], times: {} };
globalThis.Capacitor = {
  isNativePlatform: () => true,
  isPluginAvailable: (n) => n === "Preferences" || n === "PushNotifications" || n === "PushGate",
  getPlatform: () => "ios",
  Plugins: {
    Preferences: {
      get: async ({ key }) => ({ value: prefs.get(key) ?? null }),
      set: async ({ key, value }) => void prefs.set(key, value),
      remove: async ({ key }) => void prefs.delete(key),
      clear: async () => prefs.clear(),
    },
    PushNotifications: {
      getDeliveredNotifications: async () => ({ notifications: delivered.list }),
    },
    PushGate: { posted: async () => ({ times: delivered.times }) },
  },
};

const inbox = await import("../www/inbox.js");

const DAY = 86400000;
const T0 = Date.UTC(2026, 9, 8, 9, 0);
const slotsPush = (over = {}) => ({
  id: "n1",
  title: "Neue Termine in Leipzig",
  body: "Bürgerbüro Mitte: Do. 9. Okt. 09:30",
  data: { type: "slots", city: "leipzig", sub: "7", url: "https://buergerwecker.de/go/leipzig", aps: { alert: {} } },
  ...over,
});

beforeEach(async () => {
  prefs.clear();
  delivered.list = [];
  delivered.times = {};
  inbox.reset();
  await inbox.load(T0);
});

test("slots and check-in notifications go in; the setup push and strangers do not", () => {
  const e = inbox.entryOf(slotsPush(), T0);
  assert.deepEqual(e, {
    id: "n1",
    at: T0,
    type: "slots",
    title: "Neue Termine in Leipzig",
    body: "Bürgerbüro Mitte: Do. 9. Okt. 09:30",
    city: "leipzig",
    sub: "7",
  });
  assert.equal(inbox.entryOf({ id: "c", data: { type: "checkin", sub: 7 } }, T0).sub, "7");
  assert.equal(inbox.entryOf({ id: "v", data: { type: "verify", code: "123" } }, T0), null);
  // Android's shade copy without one of our tags: the system's extras, not the push's data.
  assert.equal(inbox.entryOf({ id: 0, tag: "FCM-Notification:12", title: "x", body: "y", data: { "android.title": "x" } }, T0), null);
  assert.equal(inbox.entryOf({ id: 0, tag: "verify-3", title: "x", body: "y", data: {} }, T0), null);
  assert.equal(inbox.entryOf(null, T0), null);
});

test("the same notification received and then tapped is one entry, at its first sighting", async () => {
  assert.equal(await inbox.record(slotsPush(), T0), true);
  // An Android tap: the data, the id, no text.
  assert.equal(await inbox.record({ id: "n1", data: slotsPush().data }, T0 + 60000), false);
  assert.equal(inbox.items().length, 1);
  assert.equal(inbox.items()[0].at, T0);
  assert.equal(inbox.items()[0].title, "Neue Termine in Leipzig");
});

test("a tap without text first, its text later, fills the entry in", async () => {
  await inbox.record({ id: "n1", data: slotsPush().data }, T0);
  await inbox.record(slotsPush(), T0 + 1000);
  assert.equal(inbox.items().length, 1);
  assert.equal(inbox.items()[0].body, "Bürgerbüro Mitte: Do. 9. Okt. 09:30");
});

test("two pushes with the same text are two entries when both have ids", async () => {
  await inbox.record(slotsPush({ id: "n1" }), T0);
  await inbox.record(slotsPush({ id: "n2" }), T0 + 3600000);
  assert.deepEqual(inbox.items().map((e) => e.id), ["n2", "n1"], "newest first");
});

test("without ids, the same alert's same text within a day is one entry", async () => {
  await inbox.record(slotsPush({ id: undefined }), T0);
  await inbox.record(slotsPush({ id: undefined }), T0 + 3600000);
  assert.equal(inbox.items().length, 1);
  await inbox.record(slotsPush({ id: undefined }), T0 + 2 * DAY);
  assert.equal(inbox.items().length, 2);
});

test("iOS: what sits in the notification centre is picked up once, and not again after a clear", async () => {
  delivered.list = [slotsPush({ id: "n1" }), slotsPush({ id: "n2", body: "other" }), { id: "v", data: { type: "verify" } }];
  assert.equal(await inbox.harvest(T0), true);
  assert.equal(inbox.items().length, 2);
  assert.equal(await inbox.harvest(T0 + 1000), false, "every resume sees them again");
  await inbox.clear(T0 + 2000);
  assert.equal(await inbox.harvest(T0 + 3000), false, "a cleared list stays cleared");
  assert.equal(inbox.items().length, 0);
  delivered.list.push(slotsPush({ id: "n3", body: "new" }));
  assert.equal(await inbox.harvest(T0 + 4000), true);
  assert.deepEqual(inbox.items().map((e) => e.id), ["n3"]);
});

test("iOS: a notification-centre copy is dated by the server's send time, not the app's launch", async () => {
  delivered.list = [slotsPush({ data: { ...slotsPush().data, sent: String(T0 - 3 * 3600000) } })];
  await inbox.harvest(T0);
  assert.equal(inbox.items()[0].at, T0 - 3 * 3600000);
});

test("unread until the list is opened", async () => {
  await inbox.record(slotsPush({ id: "n1" }), T0);
  await inbox.record(slotsPush({ id: "n2" }), T0 + 1000);
  assert.equal(inbox.unread(), 2);
  await inbox.markRead(T0 + 2000);
  assert.equal(inbox.unread(), 0);
  await inbox.record(slotsPush({ id: "n3" }), T0 + 3000);
  assert.equal(inbox.unread(), 1);
});

test("kept for 30 days and 50 entries, and across launches", async () => {
  for (let i = 0; i < 60; i++) await inbox.record(slotsPush({ id: `n${i}` }), T0 + i * 1000);
  assert.equal(inbox.items().length, inbox.MAX_ITEMS);
  assert.equal(inbox.items()[0].id, "n59");
  await inbox.load(T0 + 60000);
  assert.equal(inbox.items().length, inbox.MAX_ITEMS, "read back from Preferences");
  await inbox.load(T0 + 31 * DAY);
  assert.equal(inbox.items().length, 0, "older than 30 days");
});

test("a damaged stored value costs only what is damaged", async () => {
  prefs.set("inbox", JSON.stringify({ items: [{ at: T0, type: "slots", id: "ok" }, { at: "x" }, null, { at: T0, type: "verify" }], seenAt: "no" }));
  await inbox.load(T0);
  assert.deepEqual(inbox.items().map((e) => e.id), ["ok"]);
  assert.equal(inbox.unread(), 1);
  prefs.set("inbox", "not json");
  await inbox.load(T0);
  assert.deepEqual(inbox.items(), []);
});

// Android, as the push plugin reports it: the shade copy (getDeliveredNotifications)
// carries the system's extras and the tag, a tap the data, its message id and
// Firebase's send time.
const shadeCopy = (over = {}) => ({
  id: 0,
  tag: "sub-7",
  title: "Neue Termine in Leipzig",
  body: "Bürgerbüro Mitte: Do. 9. Okt. 09:30",
  data: { "android.title": "Neue Termine in Leipzig" },
  ...over,
});
const androidTap = (over = {}) => ({
  id: "0:1760000000%abc",
  data: {
    type: "slots",
    city: "leipzig",
    sub: "7",
    url: "https://buergerwecker.de/go/leipzig",
    title: "Neue Termine in Leipzig",
    body: "Bürgerbüro Mitte: Do. 9. Okt. 09:30",
    "google.sent_time": T0 - 600000,
  },
  ...over,
});

test("Android: the shade copy's tag says whose it is", () => {
  assert.deepEqual(inbox.entryOf(shadeCopy(), T0), {
    id: null,
    at: T0,
    type: "slots",
    title: "Neue Termine in Leipzig",
    body: "Bürgerbüro Mitte: Do. 9. Okt. 09:30",
    city: null,
    sub: "7",
  });
  const c = inbox.entryOf(shadeCopy({ tag: "checkin-9", title: "Suchst du noch?", body: "…" }), T0);
  assert.equal(c.type, "checkin");
  assert.equal(c.sub, "9");
});

test("Android: a tap brings its text and send time in the data", () => {
  const e = inbox.entryOf(androidTap(), T0);
  assert.equal(e.title, "Neue Termine in Leipzig");
  assert.equal(e.body, "Bürgerbüro Mitte: Do. 9. Okt. 09:30");
  assert.equal(e.at, T0 - 600000);
  assert.equal(inbox.entryOf(androidTap({ data: { ...androidTap().data, "google.sent_time": T0 + 5 } }), T0).at, T0);
});

test("Android: the shade is picked up once, not again after a clear, and a later tap is the same entry", async () => {
  delivered.list = [shadeCopy(), shadeCopy({ tag: "sub-8", body: "Bürgerbüro Grünau: Fr. 10. Okt. 08:00" })];
  assert.equal(await inbox.harvest(T0), true);
  assert.equal(await inbox.harvest(T0 + 1000), false, "every resume sees them again");
  assert.equal(inbox.items().length, 2);
  // The first one tapped later from the shade: one entry, at Firebase's send time.
  assert.equal(await inbox.record(androidTap(), T0 + 2000), true);
  assert.equal(inbox.items().length, 2);
  const tapped = inbox.items().find((e) => e.sub === "7");
  assert.equal(tapped.at, T0 - 600000);
  assert.equal(tapped.city, "leipzig");
  await inbox.clear(T0 + 3000);
  assert.equal(await inbox.harvest(T0 + 4000), false, "a cleared list stays cleared");
  assert.equal(inbox.items().length, 0);
  // The alert's next digest replaces the old one in the shade: same tag, new text.
  delivered.list = [shadeCopy({ body: "Bürgerbüro Mitte: Mo. 13. Okt. 10:00" })];
  assert.equal(await inbox.harvest(T0 + 5000), true);
  assert.equal(inbox.items().length, 1);
});

test("Android: a shade copy is dated by its post time, not the app's launch", async () => {
  delivered.list = [shadeCopy(), shadeCopy({ tag: "sub-8", body: "Bürgerbüro Grünau: Fr. 10. Okt. 08:00" })];
  delivered.times = { "sub-7": T0 - 3 * 3600000, ranker_group1: T0 - 60000 };
  await inbox.harvest(T0);
  assert.deepEqual(inbox.items().map((e) => [e.sub, e.at]), [["8", T0], ["7", T0 - 3 * 3600000]]);
});

// A special-category digest reads the same for the same count: only the shade's
// post time tells two of them apart.
const sensitive = (over = {}) => shadeCopy({ title: "Neue Termine verfügbar", body: "2 neue passende Termine", ...over });

test("Android: digests that read the same are one entry per posting, also after a clear", async () => {
  delivered.list = [sensitive()];
  delivered.times = { "sub-7": T0 - 7200000 };
  assert.equal(await inbox.harvest(T0), true);
  assert.equal(await inbox.harvest(T0 + 1000), false, "the same posting again");
  // The next digest replaces it in the shade: same tag, same text, new post time.
  delivered.times = { "sub-7": T0 - 3600000 };
  assert.equal(await inbox.harvest(T0 + 2000), true);
  assert.equal(inbox.items().length, 2);
  assert.equal(inbox.unread(), 2);
  await inbox.clear(T0 + 3000);
  assert.equal(await inbox.harvest(T0 + 4000), false, "a cleared list stays cleared");
  delivered.times = { "sub-7": T0 + 5000 };
  assert.equal(await inbox.harvest(T0 + 6000), true, "a later posting is new");
  assert.equal(inbox.items().length, 1);
});

test("Android: a tap joins the shade copy it came from, not an earlier one that reads the same", async () => {
  const tapOf = (sent, id) =>
    androidTap({ id, data: { ...androidTap().data, title: "Neue Termine verfügbar", body: "2 neue passende Termine", "google.sent_time": sent } });
  delivered.list = [sensitive()];
  delivered.times = { "sub-7": T0 - 7200000 };
  await inbox.harvest(T0);
  // Sent an hour after that posting: a later digest, whose shade copy was never seen.
  await inbox.record(tapOf(T0 - 3600000, "0:2"), T0 + 1000);
  assert.equal(inbox.items().length, 2);
  // Sent before it: the same notification, now with its city and send time.
  await inbox.record(tapOf(T0 - 7230000, "0:1"), T0 + 2000);
  assert.deepEqual(inbox.items().map((e) => [e.at, e.city]), [[T0 - 3600000, "leipzig"], [T0 - 7230000, "leipzig"]]);
});

test("Android: a shade copy after a tap or foreground copy that reads the same is a later digest", async () => {
  const seen = androidTap({ id: "0:A", data: { ...androidTap().data, title: "Neue Termine verfügbar", body: "2 neue passende Termine", "google.sent_time": T0 - 4 * 3600000 } });
  await inbox.record(seen, T0 - 4 * 3600000 + 1000);
  await inbox.markRead(T0 - 4 * 3600000 + 2000);
  delivered.list = [sensitive()];
  delivered.times = { "sub-7": T0 - 3600000 };
  assert.equal(await inbox.harvest(T0), true);
  assert.equal(inbox.items().length, 2);
  assert.equal(inbox.unread(), 1);
});
