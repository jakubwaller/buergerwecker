// The city overview's answer when the slot snapshot does not come: the server
// shows a city's slots only to a device with a live alert there (403
// not_subscribed), so that case asks the person to set one up, in both
// languages, instead of reading like an error.
import { test } from "node:test";
import assert from "node:assert/strict";
import { slotsNotice } from "../www/screens/city.js";
import { STRINGS } from "../www/i18n.js";
import { toApiError, errorText } from "../www/api.js";

test("403 not_subscribed, or no device to ask with, asks for an alert in that city", () => {
  assert.equal(slotsNotice(toApiError(403, { error: "not_subscribed", message: "Kein Alarm hier." })), "subscribe");
  assert.equal(slotsNotice({ status: 401, error: "unauthorized", message: null }), "subscribe", "no stored credential");
  for (const lang of ["de", "en"]) assert.ok(STRINGS[lang]["city.subscribeHint"], lang);
  assert.notEqual(STRINGS.de["city.subscribeHint"], STRINGS.en["city.subscribeHint"]);
});

test("other failures pass the server's word on; nothing to say without one", () => {
  assert.equal(slotsNotice(null), null);
  assert.equal(slotsNotice(toApiError(404, { error: "unknown_city" })), null, "a server without the snapshot");
  // The slots route's per-device budget: shown, but in the app's own words,
  // not the website's sign-up sentence the server sends with every 429.
  const limited = toApiError(429, { error: "rate_limited", retry_after: 600, message: "Too many sign-ups were made in a short time." });
  assert.equal(slotsNotice(limited), "error");
  assert.deepEqual(errorText(limited), { key: "err.rate_limited", vars: {} });
  assert.equal(slotsNotice({ status: 0, error: "network", message: null }), "error");
  assert.equal(slotsNotice(toApiError(403, { error: "device_unverified" })), "error");
});
