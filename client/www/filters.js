// Which free slots an alert would notify about: the server's app/filters.py
// matches(), over the slots endpoint's snapshot, so the alerts list shows a
// slot only when that alert would have sent it. The widget does the same
// natively (Filter.matches in BuergerweckerWidget.swift). Pure, tested in
// test/filters.test.mjs.

const minutes = (hhmm) => {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(hhmm ?? ""));
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};

// "2026-10-08" as a day count, for differences between calendar days.
const dayNumber = (iso) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso ?? ""));
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) / 86400000 : null;
};

const pad = (n) => String(n).padStart(2, "0");

// Today in Europe/Berlin, the cities' own zone: the server counts
// max_days_ahead from that day, not from the phone's.
export function berlinToday(now = new Date()) {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Berlin",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(now);
    const get = (type) => parts.find((p) => p.type === type)?.value;
    return `${get("year")}-${get("month")}-${get("day")}`;
  } catch {
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  }
}

// One snapshot slot ({ date, time, location }) against one subscription as the
// API sends it: `locations` "all" or a list of office ids, `weekdays` ISO
// (1 = Monday), times "HH:MM" and both ends inclusive, `max_days_ahead` null
// or a number of days. The service is the caller's to check.
export function matches(sub, slot, today) {
  if (Array.isArray(sub.locations) && !sub.locations.includes(slot.location)) return false;
  const d = dayNumber(slot.date);
  if (d == null) return false;
  if (sub.max_days_ahead != null) {
    const t0 = dayNumber(today);
    if (t0 != null && d - t0 > Number(sub.max_days_ahead)) return false;
  }
  const weekday = ((new Date(d * 86400000).getUTCDay() + 6) % 7) + 1;
  const days = Array.isArray(sub.weekdays) ? sub.weekdays.map(Number) : [1, 2, 3, 4, 5, 6, 7];
  if (!days.includes(weekday)) return false;
  const t = minutes(slot.time);
  if (t == null) return false;
  const lo = minutes(sub.time_start || "00:00") ?? 0;
  const hi = minutes(sub.time_end || "23:59") ?? 23 * 60 + 59;
  return t >= lo && t <= hi;
}

// The slots of the service's snapshot entry this alert would notify about,
// soonest first. `complete` is false when "none matches" cannot be said: the
// service is not in the snapshot (not polled yet), or the snapshot was cut off
// (it keeps the soonest 100 per service, app/snapshots.py) and a later slot
// might match.
export function matchingSlots(sub, service, now = new Date()) {
  const slots = Array.isArray(service?.slots) ? service.slots : [];
  const today = berlinToday(now);
  const hits = slots.filter((s) => matches(sub, s, today));
  const complete = !!service && (!Number.isFinite(service.n_total) || service.n_total <= slots.length);
  return { slots: hits, complete };
}
