// The alerts list shows a slot only when that alert would have sent it: the
// same checks as the server's app/filters.py matches().
import { test } from "node:test";
import assert from "node:assert/strict";
import { berlinToday, matches, matchingSlots } from "../www/filters.js";

const sub = (over = {}) => ({
  appointment_type: "svc",
  locations: "all",
  weekdays: [1, 2, 3, 4, 5],
  time_start: "08:00",
  time_end: "12:00",
  max_days_ahead: null,
  ...over,
});
// 2026-10-08 is a Thursday.
const slot = (over = {}) => ({ date: "2026-10-08", time: "09:30", location: "a", ...over });

test("today is the Berlin date, not the phone's or UTC's", () => {
  assert.equal(berlinToday(new Date("2026-10-08T21:59:00Z")), "2026-10-08");
  assert.equal(berlinToday(new Date("2026-10-08T22:30:00Z")), "2026-10-09", "after midnight in Berlin (CEST)");
  assert.equal(berlinToday(new Date("2026-12-31T23:30:00Z")), "2027-01-01", "CET");
});

test("offices: all, or the listed ones only", () => {
  const today = "2026-10-08";
  assert.ok(matches(sub(), slot({ location: "z" }), today));
  assert.ok(matches(sub({ locations: ["a", "b"] }), slot({ location: "b" }), today));
  assert.ok(!matches(sub({ locations: ["a"] }), slot({ location: "b" }), today));
});

test("weekdays are ISO, Monday 1 to Sunday 7", () => {
  const today = "2026-10-08";
  assert.ok(!matches(sub(), slot({ date: "2026-10-11" }), today), "Sunday");
  assert.ok(matches(sub({ weekdays: [7] }), slot({ date: "2026-10-11" }), today));
  assert.ok(matches(sub({ weekdays: [1] }), slot({ date: "2026-10-12" }), today), "Monday");
});

test("the time window includes both ends", () => {
  const today = "2026-10-08";
  assert.ok(matches(sub(), slot({ time: "08:00" }), today));
  assert.ok(matches(sub(), slot({ time: "12:00" }), today));
  assert.ok(!matches(sub(), slot({ time: "07:59" }), today));
  assert.ok(!matches(sub(), slot({ time: "12:01" }), today));
  assert.ok(matches(sub({ time_start: null, time_end: null }), slot({ time: "23:59" }), today), "no window is all day");
  assert.ok(!matches(sub(), slot({ time: null }), today), "a slot without a time never matches");
});

test("days ahead: the limit itself is still in, the day after is not", () => {
  const today = "2026-10-08";
  assert.ok(matches(sub({ max_days_ahead: 7 }), slot({ date: "2026-10-15" }), today));
  assert.ok(!matches(sub({ max_days_ahead: 7 }), slot({ date: "2026-10-16" }), today));
  assert.ok(matches(sub({ max_days_ahead: null }), slot({ date: "2027-03-01" }), today));
  assert.ok(!matches(sub(), slot({ date: "8.10.2026" }), today), "an unparsable date never matches");
});

test("matchingSlots keeps the snapshot's order and says when 'none' is certain", () => {
  const now = new Date("2026-10-08T07:00:00Z");
  const service = {
    id: "svc",
    n_total: 3,
    slots: [slot({ time: "07:00" }), slot({ time: "09:00" }), slot({ date: "2026-10-09", time: "10:00" })],
  };
  const r = matchingSlots(sub(), service, now);
  assert.deepEqual(r.slots.map((s) => s.time), ["09:00", "10:00"]);
  assert.equal(r.complete, true);
  assert.equal(matchingSlots(sub(), { ...service, n_total: 250 }, now).complete, false, "cut off at 100");
  assert.deepEqual(matchingSlots(sub(), undefined, now), { slots: [], complete: false }, "not polled yet");
  assert.deepEqual(matchingSlots(sub(), { id: "svc", n_total: 0, slots: [] }, now), { slots: [], complete: true });
});
