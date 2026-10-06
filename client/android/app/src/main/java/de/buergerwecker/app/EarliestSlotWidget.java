package de.buergerwecker.app;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Collections;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * The home-screen widget: the earliest free slot in the cities the person has
 * alerts for. It only shows; tapping opens the app, and nothing here books.
 *
 * The page (client/www/widget.js, through WidgetBridgePlugin) stores the city
 * list, the language and the words to show; this fetches
 * GET /api/v1/cities/slug/slots itself (a public route, so no credential is
 * shared with it) and keeps the last answer next to the list. While the
 * server's API gate is closed (404) or the network is down it shows that last
 * answer with its time, or, with none, the neutral "set up an alert" text.
 *
 * Refresh: the platform's updatePeriodMillis (30 minutes, its floor), plus an
 * update broadcast whenever the page changes the list. The fetch runs inside
 * that broadcast (goAsync), all cities in parallel with 8 s timeouts, well
 * inside the receiver's budget. The iOS twin is BuergerweckerWidget.swift.
 */
public class EarliestSlotWidget extends AppWidgetProvider {
    static final String PREFS = "buergerwecker_widget";
    static final String KEY_CONFIG = "widget_config";
    static final String KEY_CACHE = "widget_cache";
    private static final String API_BASE = "https://buergerwecker.de/api/v1";
    private static final int TIMEOUT_MS = 8_000;
    private static final int[] ROWS = {R.id.row1, R.id.row2, R.id.row3};
    private static final int[] ROW_NAMES = {R.id.row1_name, R.id.row2_name, R.id.row3_name};
    private static final int[] ROW_OFFICES = {R.id.row1_office, R.id.row2_office, R.id.row3_office};
    private static final int[] ROW_SLOTS = {R.id.row1_slot, R.id.row2_slot, R.id.row3_slot};

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] ids) {
        final PendingResult pending = goAsync();
        final Context app = context.getApplicationContext();
        new Thread(() -> {
            try {
                refresh(app, manager, ids);
            } catch (Throwable t) {
                // Whatever went wrong, the widgets still get drawn from the cache.
                render(app, manager, ids);
            } finally {
                pending.finish();
            }
        }).start();
    }

    // A resize changes how many rows fit; no network for that.
    @Override
    public void onAppWidgetOptionsChanged(Context context, AppWidgetManager manager, int id, Bundle options) {
        render(context, manager, new int[] {id});
    }

    // --- Model -------------------------------------------------------------

    /** One city's last answer. */
    private static final class Snap {
        String date, time, office; // date "2026-10-08", time "09:30", both optional but date
        long polledAt;             // epoch ms of the server's last poll, 0 = unknown
        boolean hasData;           // false: the server has no snapshot for it yet

        boolean hasSlot() { return date != null; }
        String sortKey() { return date + " " + (time == null ? "" : time); }

        JSONObject toJson() throws JSONException {
            return new JSONObject().put("date", date == null ? JSONObject.NULL : date)
                .put("time", time == null ? JSONObject.NULL : time)
                .put("office", office == null ? JSONObject.NULL : office)
                .put("polledAt", polledAt).put("hasData", hasData);
        }

        static Snap fromJson(JSONObject o) {
            Snap s = new Snap();
            s.date = o.isNull("date") ? null : o.optString("date");
            s.time = o.isNull("time") ? null : o.optString("time");
            s.office = o.isNull("office") ? null : o.optString("office");
            s.polledAt = o.optLong("polledAt", 0);
            s.hasData = o.optBoolean("hasData", false);
            return s;
        }
    }

    private static final class Row {
        final String name, office;
        final Snap snap;
        Row(String name, String office, Snap snap) { this.name = name; this.office = office; this.snap = snap; }
    }

    // --- Fetch ---------------------------------------------------------------

    private static void refresh(Context context, AppWidgetManager manager, int[] ids) throws Exception {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        JSONObject config = readConfig(prefs);
        if (config != null) {
            JSONArray cities = config.optJSONArray("cities");
            String lang = config.optString("lang", "de");
            JSONObject cache = readCache(prefs);
            JSONObject next = new JSONObject();
            ExecutorService pool = Executors.newFixedThreadPool(5);
            try {
                List<String> slugs = new ArrayList<>();
                List<Future<Snap>> futures = new ArrayList<>();
                for (int i = 0; cities != null && i < cities.length(); i++) {
                    final JSONObject city = cities.getJSONObject(i);
                    final String slug = city.getString("slug");
                    final JSONArray alerts = city.optJSONArray("alerts");
                    slugs.add(slug);
                    futures.add(pool.submit(() -> fetch(slug, alerts, lang)));
                }
                for (int i = 0; i < slugs.size(); i++) {
                    Snap snap = null;
                    try {
                        snap = futures.get(i).get(TIMEOUT_MS * 2L, TimeUnit.MILLISECONDS);
                    } catch (Exception ignored) {
                        // fall through to the cache
                    }
                    if (snap != null) next.put(slugs.get(i), snap.toJson());
                    else if (cache.has(slugs.get(i))) next.put(slugs.get(i), cache.get(slugs.get(i)));
                }
            } finally {
                pool.shutdownNow();
            }
            // The fetch above can take a while; "Delete my data" may have
            // cleared the list meanwhile. Re-read it: with it gone nothing is
            // saved, and only the cities the current list still names are kept.
            JSONObject current = readConfig(prefs);
            if (current == null) {
                prefs.edit().remove(KEY_CACHE).apply();
            } else {
                prefs.edit().putString(KEY_CACHE, keepOnly(next, current).toString()).apply();
            }
        }
        render(context, manager, ids);
    }

    /** `cache` without the cities the config no longer lists. */
    static JSONObject keepOnly(JSONObject cache, JSONObject config) throws JSONException {
        JSONObject out = new JSONObject();
        JSONArray cities = config.optJSONArray("cities");
        for (int i = 0; cities != null && i < cities.length(); i++) {
            String slug = cities.getJSONObject(i).optString("slug");
            if (cache.has(slug)) out.put(slug, cache.get(slug));
        }
        return out;
    }

    /** null on any failure (network, 404 while the gate is closed, bad JSON). */
    private static Snap fetch(String slug, JSONArray alerts, String lang) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(API_BASE + "/cities/" + URLEncoder.encode(slug, "UTF-8")
                + "/slots?lang=" + URLEncoder.encode(lang, "UTF-8"));
            conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(TIMEOUT_MS);
            conn.setReadTimeout(TIMEOUT_MS);
            conn.setRequestProperty("Accept", "application/json");
            if (conn.getResponseCode() != 200) return null;
            StringBuilder body = new StringBuilder();
            try (BufferedReader in = new BufferedReader(new InputStreamReader(conn.getInputStream(), "UTF-8"))) {
                String line;
                while ((line = in.readLine()) != null) body.append(line);
            }
            return parse(new JSONObject(body.toString()), alerts, Calendar.getInstance());
        } catch (Exception e) {
            return null;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /**
     * The earliest slot that would also trigger one of the alerts: the alert's
     * own service, and its filter on top (matches() below).
     */
    static Snap parse(JSONObject body, JSONArray alerts, Calendar now) throws JSONException {
        Snap snap = new Snap();
        JSONArray services = body.optJSONArray("services");
        for (int i = 0; services != null && i < services.length(); i++) {
            JSONObject svc = services.getJSONObject(i);
            List<JSONObject> mine = new ArrayList<>();
            for (int k = 0; alerts != null && k < alerts.length(); k++) {
                JSONObject a = alerts.getJSONObject(k);
                if (a.optString("service").equals(svc.optString("id"))) mine.add(a);
            }
            if (mine.isEmpty()) continue;
            snap.hasData = true;
            long polled = parseInstant(svc.isNull("polled_at") ? null : svc.optString("polled_at"));
            if (polled > snap.polledAt) snap.polledAt = polled;
            JSONArray slots = svc.optJSONArray("slots");
            for (int j = 0; slots != null && j < slots.length(); j++) {
                JSONObject e = slots.getJSONObject(j);
                String date = e.optString("date", null);
                String time = e.isNull("time") ? null : e.optString("time");
                String location = e.isNull("location") ? null : e.optString("location");
                if (date == null) continue;
                boolean ok = false;
                for (JSONObject a : mine) if (matches(a, date, time, location, now)) { ok = true; break; }
                if (!ok) continue;
                if (!snap.hasSlot() || (date + " " + (time == null ? "" : time)).compareTo(snap.sortKey()) < 0) {
                    snap.date = date;
                    snap.time = time;
                    snap.office = e.isNull("location_name") ? null : e.optString("location_name");
                }
            }
        }
        return snap;
    }

    /**
     * Mirrors app/filters.py matches() (the service is checked by the caller):
     * offices "all" or a membership test, the day's ISO weekday in the list,
     * the time between start and end inclusive, and, when set, no further than
     * maxDaysAhead from today's date in Europe/Berlin (the cities' own zone).
     */
    static boolean matches(JSONObject a, String date, String time, String location, Calendar now) {
        JSONArray offices = a.optJSONArray("locations"); // a string "all" is not an array
        if (offices != null && !contains(offices, location == null ? "" : location)) return false;
        String[] p = date.split("-");
        if (p.length != 3) return false;
        int y, m, d;
        try {
            y = Integer.parseInt(p[0]);
            m = Integer.parseInt(p[1]);
            d = Integer.parseInt(p[2]);
        } catch (NumberFormatException e) {
            return false;
        }
        TimeZone berlin = TimeZone.getTimeZone("Europe/Berlin");
        Calendar day = Calendar.getInstance(berlin);
        day.clear();
        day.set(y, m - 1, d, 12, 0, 0);
        int ahead = a.optInt("maxDaysAhead", 0);
        if (!a.isNull("maxDaysAhead") && ahead > 0) {
            Calendar today = Calendar.getInstance(berlin);
            today.setTimeInMillis(now.getTimeInMillis());
            today.set(Calendar.HOUR_OF_DAY, 12);
            today.set(Calendar.MINUTE, 0);
            today.set(Calendar.SECOND, 0);
            today.set(Calendar.MILLISECOND, 0);
            long days = Math.round((day.getTimeInMillis() - today.getTimeInMillis()) / 86_400_000.0);
            if (days > ahead) return false;
        }
        int dow = day.get(Calendar.DAY_OF_WEEK); // 1 = Sunday
        int iso = dow == Calendar.SUNDAY ? 7 : dow - 1;
        JSONArray weekdays = a.optJSONArray("weekdays");
        if (weekdays != null) {
            boolean in = false;
            for (int i = 0; i < weekdays.length(); i++) if (weekdays.optInt(i) == iso) in = true;
            if (!in) return false;
        }
        int t = minutes(time), lo = minutes(a.optString("timeStart", "00:00")), hi = minutes(a.optString("timeEnd", "23:59"));
        if (t < 0 || lo < 0 || hi < 0) return false;
        return t >= lo && t <= hi;
    }

    private static int minutes(String hhmm) {
        if (hhmm == null) return -1;
        String[] p = hhmm.split(":");
        if (p.length != 2) return -1;
        try {
            return Integer.parseInt(p[0]) * 60 + Integer.parseInt(p[1]);
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    private static boolean contains(JSONArray a, String s) {
        for (int i = 0; i < a.length(); i++) if (s.equals(a.optString(i))) return true;
        return false;
    }

    private static long parseInstant(String iso) {
        if (iso == null) return 0;
        try {
            SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.ROOT);
            f.setTimeZone(TimeZone.getTimeZone("UTC"));
            Date d = f.parse(iso);
            return d == null ? 0 : d.getTime();
        } catch (Exception e) {
            return 0;
        }
    }

    // --- Draw ----------------------------------------------------------------

    private static JSONObject readConfig(SharedPreferences prefs) {
        try {
            String raw = prefs.getString(KEY_CONFIG, null);
            return raw == null ? null : new JSONObject(raw);
        } catch (JSONException e) {
            return null;
        }
    }

    private static JSONObject readCache(SharedPreferences prefs) {
        try {
            return new JSONObject(prefs.getString(KEY_CACHE, "{}"));
        } catch (JSONException e) {
            return new JSONObject();
        }
    }

    private static void render(Context context, AppWidgetManager manager, int[] ids) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        JSONObject config = readConfig(prefs);
        JSONObject strings = config == null ? null : config.optJSONObject("strings");
        List<Row> rows = new ArrayList<>();
        if (config != null && strings != null) {
            JSONObject cache = readCache(prefs);
            JSONArray cities = config.optJSONArray("cities");
            for (int i = 0; cities != null && i < cities.length(); i++) {
                JSONObject c = cities.optJSONObject(i);
                if (c == null) continue;
                JSONObject s = cache.optJSONObject(c.optString("slug"));
                rows.add(new Row(c.optString("name"), c.optString("office"), s == null ? null : Snap.fromJson(s)));
            }
        }
        // Cities with a slot first, earliest first; the rest keep the app's order.
        List<Row> withSlot = new ArrayList<>();
        List<Row> rest = new ArrayList<>();
        for (Row r : rows) (r.snap != null && r.snap.hasSlot() ? withSlot : rest).add(r);
        Collections.sort(withSlot, (a, b) -> a.snap.sortKey().compareTo(b.snap.sortKey()));
        withSlot.addAll(rest);

        for (int id : ids) {
            Bundle options = manager.getAppWidgetOptions(id);
            int minHeight = options == null ? 110 : options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 110);
            int fit = Math.max(1, Math.min(3, (minHeight - 24 - 16) / 36));
            manager.updateAppWidget(id, views(context, strings, withSlot, fit));
        }
    }

    private static RemoteViews views(Context context, JSONObject strings, List<Row> rows, int fit) {
        RemoteViews v = new RemoteViews(context.getPackageName(), R.layout.widget_slot);
        Intent open = context.getPackageManager().getLaunchIntentForPackage(context.getPackageName());
        if (open != null) {
            v.setOnClickPendingIntent(R.id.widget_root,
                PendingIntent.getActivity(context, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT));
        }
        for (int r : ROWS) v.setViewVisibility(r, View.GONE);
        v.setViewVisibility(R.id.as_of, View.GONE);
        if (rows.isEmpty()) {
            v.setViewVisibility(R.id.message, View.VISIBLE);
            v.setTextViewText(R.id.message, context.getString(R.string.widget_open_app));
            return v;
        }
        v.setViewVisibility(R.id.message, View.GONE);
        long newest = 0;
        for (int i = 0; i < Math.min(fit, rows.size()); i++) {
            Row row = rows.get(i);
            v.setViewVisibility(ROWS[i], View.VISIBLE);
            v.setTextViewText(ROW_NAMES[i], row.name);
            boolean hasSlot = row.snap != null && row.snap.hasSlot();
            String office = hasSlot ? row.snap.office : null;
            boolean showOffice = office != null && !office.isEmpty() && office.length() <= 28;
            v.setViewVisibility(ROW_OFFICES[i], showOffice ? View.VISIBLE : View.GONE);
            if (showOffice) v.setTextViewText(ROW_OFFICES[i], office);
            String text;
            if (hasSlot) text = slotText(strings, row.snap.date, row.snap.time);
            else text = context.getString(row.snap != null && row.snap.hasData ? R.string.widget_no_match : R.string.widget_no_snapshot);
            v.setTextViewText(ROW_SLOTS[i], text);
            if (row.snap != null && row.snap.polledAt > newest) newest = row.snap.polledAt;
        }
        if (newest > 0) {
            SimpleDateFormat f = new SimpleDateFormat("HH:mm", Locale.ROOT);
            v.setViewVisibility(R.id.as_of, View.VISIBLE);
            v.setTextViewText(R.id.as_of, fill(word(strings, "widget.asOf"), "time", f.format(new Date(newest))));
        }
        return v;
    }

    // --- Words (the page's own tables; www/format.js formatDay is the twin) ---

    private static String word(JSONObject strings, String key) {
        return strings.optString(key, key);
    }

    private static String fill(String template, String... kv) {
        for (int i = 0; i + 1 < kv.length; i += 2) template = template.replace("{" + kv[i] + "}", kv[i + 1]);
        return template;
    }

    static String slotText(JSONObject strings, String isoDay, String time) {
        String day = dayText(strings, isoDay, Calendar.getInstance());
        if (time == null) return day;
        return fill(word(strings, "date.atTime"), "day", day, "time", fill(word(strings, "date.time"), "time", time));
    }

    /** "Do., 8. Okt." / "Thu 8 Oct", "heute" / "tomorrow", relative to the device's calendar day. */
    static String dayText(JSONObject strings, String isoDay, Calendar now) {
        String[] p = isoDay.split("-");
        if (p.length != 3) return isoDay;
        int y, m, d;
        try {
            y = Integer.parseInt(p[0]);
            m = Integer.parseInt(p[1]);
            d = Integer.parseInt(p[2]);
        } catch (NumberFormatException e) {
            return isoDay;
        }
        Calendar day = Calendar.getInstance();
        day.clear();
        day.set(y, m - 1, d, 12, 0, 0);
        if (sameDay(day, now)) return word(strings, "date.today");
        Calendar tomorrow = (Calendar) now.clone();
        tomorrow.add(Calendar.DAY_OF_MONTH, 1);
        if (sameDay(day, tomorrow)) return word(strings, "date.tomorrow");
        int dow = day.get(Calendar.DAY_OF_WEEK); // 1 = Sunday
        int isoWeekday = dow == Calendar.SUNDAY ? 7 : dow - 1;
        return fill(word(strings, "date.dayMonth"), "wd", word(strings, "weekday." + isoWeekday),
            "d", String.valueOf(d), "m", word(strings, "month." + m));
    }

    private static boolean sameDay(Calendar a, Calendar b) {
        return a.get(Calendar.YEAR) == b.get(Calendar.YEAR) && a.get(Calendar.DAY_OF_YEAR) == b.get(Calendar.DAY_OF_YEAR);
    }
}
