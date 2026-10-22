// openExternal leaves the app for the system browser: Capacitor turns a
// top-level navigation to an outside origin into an OS open, but on iOS only
// while the app is active, so the hand-off waits for activation.
import { test } from "node:test";
import assert from "node:assert/strict";

let active = true;
const listeners = [];
globalThis.Capacitor = {
  isNativePlatform: () => true,
  getPlatform: () => "ios",
  isPluginAvailable: (n) => n === "App",
  Plugins: {
    App: {
      getState: async () => ({ isActive: active }),
      addListener: (name, fn) => { listeners.push(fn); return Promise.resolve({ remove() { listeners.splice(listeners.indexOf(fn), 1); } }); },
    },
  },
};
globalThis.location = { href: "app://index.html" };
const { openExternal } = await import("../www/native.js");

test("an active app navigates at once, and only to http(s)", async () => {
  await openExternal("https://termine.example.com/select2?md=4");
  assert.equal(globalThis.location.href, "https://termine.example.com/select2?md=4");
  await openExternal("javascript:alert(1)");
  assert.equal(globalThis.location.href, "https://termine.example.com/select2?md=4");
});

test("a push tap before activation waits for the app to become active", async () => {
  active = false;
  globalThis.location.href = "app://index.html";
  const opening = openExternal("https://termine.example.com/");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(globalThis.location.href, "app://index.html");
  assert.equal(listeners.length, 1);
  listeners[0]({ isActive: false });
  listeners[0]({ isActive: true });
  await opening;
  assert.equal(globalThis.location.href, "https://termine.example.com/");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(listeners.length, 0);
});
