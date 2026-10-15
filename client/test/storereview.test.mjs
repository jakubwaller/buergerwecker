// The rating ask after "I've got one" (native.js requestReview) and the two
// StoreReview plugins it talks to. A plugin that is missing or unregistered
// fails silently on the phone, so the wiring is held here.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const state = { available: true, calls: 0, reply: async () => {} };
globalThis.Capacitor = {
  isNativePlatform: () => true,
  isPluginAvailable: (n) => n === "StoreReview" && state.available,
  getPlatform: () => "ios",
  Plugins: { StoreReview: { requestReview: () => { state.calls++; return state.reply(); } } },
};
const { requestReview } = await import("../www/native.js");

test("requestReview calls the plugin once", () => {
  state.calls = 0;
  requestReview();
  assert.equal(state.calls, 1);
});

test("requestReview is inert in a build without the plugin", () => {
  state.calls = 0;
  state.available = false;
  assert.doesNotThrow(() => requestReview());
  assert.equal(state.calls, 0);
  state.available = true;
});

test("requestReview swallows a rejection", async () => {
  let unhandled = null;
  const onUnhandled = (e) => { unhandled = e; };
  process.on("unhandledRejection", onUnhandled);
  state.reply = async () => { throw new Error("no store"); };
  requestReview();
  await new Promise((r) => setTimeout(r, 10));
  process.off("unhandledRejection", onUnhandled);
  state.reply = async () => {};
  assert.equal(unhandled, null);
});

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

test("only ending an alert asks: \"I've got one\", or Stop after a slots notification", () => {
  const subs = read("../www/screens/subs.js");
  assert.equal(subs.match(/requestReview\b/g).length, 2, "the import and one call");
  assert.match(subs, /if \(gotOne \|\| notifiedSlots\(sub\.id\)\) setTimeout\(requestReview, 1000\);/);
  assert.match(subs, /stop\(sub, false, true\)/);
});

test("Android: StoreReviewPlugin is named StoreReview, registered, and Play Review is a dependency", () => {
  const dir = "../android/app/src/main/java/de/buergerwecker/app/";
  const java = read(dir + "StoreReviewPlugin.java");
  assert.match(java, /@CapacitorPlugin\(name = "StoreReview"\)/);
  assert.match(java, /@PluginMethod\s+public void requestReview\(PluginCall call\)/);
  assert.match(read(dir + "MainActivity.java"), /registerPlugin\(StoreReviewPlugin\.class\)/);
  assert.match(read("../android/app/build.gradle"), /com\.google\.android\.play:review:/);
});

test("iOS: StoreReviewPlugin is named StoreReview, registered, and compiled into the app", () => {
  const swift = read("../ios/App/App/StoreReviewPlugin.swift");
  assert.match(swift, /jsName = "StoreReview"/);
  assert.match(swift, /CAPPluginMethod\(name: "requestReview"/);
  assert.match(read("../ios/App/App/MainViewController.swift"), /registerPluginInstance\(StoreReviewPlugin\(\)\)/);
  assert.match(read("../ios/App/App.xcodeproj/project.pbxproj"), /StoreReviewPlugin\.swift in Sources/);
});
