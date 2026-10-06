// Run with TZ=Europe/Berlin (npm test sets it), so local-time output is fixed.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  formatDay, formatSlot, formatInstant, formatInstantDay, weekdaySummary, timeWindow, filterSummary, slotPreview,
} from "../www/format.js";

const NOW = new Date("2026-10-06T10:00:00Z"); // Tuesday, 12:00 in Berlin

test("a slot's calendar day, relative to today, never shifted by the time zone", () => {
  assert.equal(formatDay("2026-10-06", "de", NOW), "heute");
  assert.equal(formatDay("2026-10-07", "en", NOW), "tomorrow");
  assert.equal(formatDay("2026-10-08", "de", NOW), "Do., 8. Okt.");
  assert.equal(formatDay("2026-10-08", "en", NOW), "Thu 8 Oct");
  assert.equal(formatDay("2026-12-31", "de", NOW), "Do., 31. Dez.");
  assert.equal(formatDay("not a day", "de", NOW), "not a day");
});

test("a slot: day and the office's own time", () => {
  assert.equal(formatSlot({ date: "2026-10-08", time: "09:30" }, "de", NOW), "Do., 8. Okt., 09:30 Uhr");
  assert.equal(formatSlot({ date: "2026-10-07", time: "14:05" }, "en", NOW), "tomorrow, 14:05");
  assert.equal(formatSlot({ date: "2026-10-08" }, "en", NOW), "Thu 8 Oct");
});

test("the server's UTC instants show in local time", () => {
  assert.equal(formatInstant("2026-10-06T09:04:00Z", "de", NOW), "heute, 11:04 Uhr");
  assert.equal(formatInstant("2026-10-05T22:30:00Z", "en", NOW), "today, 00:30");
  assert.equal(formatInstant("2026-10-20T08:00:00Z", "en", NOW), "Tue 20 Oct, 10:00");
  assert.equal(formatInstant(null, "de", NOW), null);
  assert.equal(formatInstant("garbage", "de", NOW), null);
  assert.equal(formatInstantDay("2026-10-20T23:30:00Z", "de", NOW), "Mi., 21. Okt.");
});

test("weekdays: runs of three or more as a range", () => {
  assert.equal(weekdaySummary([1, 2, 3, 4, 5], "de"), "Mo–Fr");
  assert.equal(weekdaySummary([1, 3, 5], "en"), "Mon, Wed, Fri");
  assert.equal(weekdaySummary([1, 2, 4, 5, 6], "de"), "Mo, Di, Do–Sa");
  assert.equal(weekdaySummary([1, 2, 3, 4, 5, 6, 7], "en"), "every day");
  assert.equal(weekdaySummary([6, 7], "de"), "Sa, So");
});

test("time window and the one-line summary", () => {
  assert.equal(timeWindow("00:00", "23:59", "de"), "ganztags");
  assert.equal(timeWindow("08:00", "12:00", "en"), "08:00–12:00");
  assert.equal(
    filterSummary({ weekdays: [1, 2, 3, 4, 5], time_start: "08:00", time_end: "12:00", max_days_ahead: 14, locations: "all" }, "de"),
    "Mo–Fr · 08:00–12:00 · nächste 14 Tage · alle Standorte",
  );
  assert.equal(
    filterSummary({ weekdays: [1, 2, 3, 4, 5, 6, 7], time_start: "00:00", time_end: "23:59", max_days_ahead: null, locations: ["a"] }, "en"),
    "every day · any time",
  );
});

test("slot preview: the earliest, a few after it, and the rest as a count", () => {
  const slot = (date, time, location = "x") => ({ date, time, location, location_name: "Amt" });
  const svc = {
    n_total: 9,
    earliest: slot("2026-10-07", "08:00"),
    slots: [slot("2026-10-07", "08:00"), slot("2026-10-07", "09:00"), slot("2026-10-08", "10:00"), slot("2026-10-09", "11:00"), slot("2026-10-10", "12:00")],
  };
  const p = slotPreview(svc, 3);
  assert.deepEqual(p.earliest, svc.earliest);
  assert.deepEqual(p.next.map((s) => s.time), ["09:00", "10:00", "11:00"]);
  assert.equal(p.more, 5);
  assert.deepEqual(slotPreview({ n_total: 0, earliest: null, slots: [] }), { earliest: null, next: [], more: 0 });
  assert.equal(slotPreview({ earliest: slot("2026-10-07", "08:00"), slots: [slot("2026-10-07", "08:00")] }).more, 0);
});
