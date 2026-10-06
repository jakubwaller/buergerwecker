import WidgetKit
import SwiftUI

// The home-screen widget: the earliest free slot in the cities the person has
// alerts for, small and medium, refreshed about every twenty minutes. It only
// shows; tapping it opens the app, and nothing here books anything.
//
// The page (client/www/widget.js) writes the city list, the language and the
// words to show into the App Group's UserDefaults; the widget fetches
// GET /api/v1/cities/<slug>/slots itself (a public route, so no credential is
// shared with it) and keeps the last answer in the same place. While the
// server's API gate is closed (404) or the network is down, it shows that last
// answer with its time, or, with none, the neutral "set up an alert" text.

// MARK: - Storage (repeated in App/WidgetBridgePlugin.swift; client/test/widget.test.mjs checks)

enum Shared {
    static let group = "group.de.buergerwecker.app"
    static let configKey = "widget_config"
    static let cacheKey = "widget_cache"
    static let apiBase = "https://buergerwecker.de/api/v1"
    static let openURL = URL(string: "buergerwecker://")!
    static var defaults: UserDefaults? { UserDefaults(suiteName: group) }
}

// MARK: - What the page wrote

struct Config: Decodable {
    struct City: Decodable {
        let slug: String
        let name: String
        let office: String?
        let alerts: [Alert]?
    }
    // One alert's filter, as the server's app/filters.py matches() reads it.
    struct Alert: Decodable {
        let service: String
        let locations: Locations?   // "all" or a list of office ids
        let weekdays: [Int]?        // ISO, 1 = Monday
        let timeStart: String?      // "HH:MM", inclusive
        let timeEnd: String?        // "HH:MM", inclusive
        let maxDaysAhead: Int?
    }
    enum Locations: Decodable {
        case all
        case list([String])
        init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            if let ids = try? c.decode([String].self) { self = .list(ids) } else { self = .all }
        }
    }
    let lang: String?
    let strings: [String: String]?
    let cities: [City]

    static func load() -> Config? {
        guard let raw = Shared.defaults?.string(forKey: Shared.configKey),
              let data = raw.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(Config.self, from: data)
    }
}

// MARK: - What the server answered

struct Slot: Codable, Equatable {
    let date: String   // "2026-10-08", a calendar day at the office
    let time: String?  // "09:30"
    let office: String?

    var sortKey: String { date + " " + (time ?? "") }
}

// One city's last answer. `slot == nil && hasData` means "polled, nothing
// free"; `!hasData` means the server has no snapshot for it yet.
struct Snapshot: Codable {
    var slot: Slot?
    var polledAt: Date?
    var hasData: Bool
}

private struct SlotsResponse: Decodable {
    struct Earliest: Decodable {
        let date: String
        let time: String?
        let location: String?
        let location_name: String?
    }
    struct Service: Decodable {
        let id: String
        let polled_at: String?
        let slots: [Earliest]?
    }
    let services: [Service]
}

enum SlotsAPI {
    // nil on any failure (network, 404 while the gate is closed, bad JSON):
    // the caller falls back to the cache.
    static func fetch(slug: String, alerts: [Config.Alert], lang: String) async -> Snapshot? {
        var comps = URLComponents(string: "\(Shared.apiBase)/cities/\(slug)/slots")
        comps?.queryItems = [URLQueryItem(name: "lang", value: lang)]
        guard let url = comps?.url else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 15
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let body = try? JSONDecoder().decode(SlotsResponse.self, from: data) else { return nil }
        return snapshot(from: body, alerts: alerts)
    }

    // The earliest slot that would also trigger one of the alerts: the
    // alert's own service, and its filter on top.
    fileprivate static func snapshot(from body: SlotsResponse, alerts: [Config.Alert], now: Date = Date()) -> Snapshot {
        let iso = ISO8601DateFormatter()
        let watched = Set(alerts.map { $0.service })
        let wanted = body.services.filter { watched.contains($0.id) }
        let polled = wanted.compactMap { $0.polled_at.flatMap(iso.date(from:)) }.max()
        var best: Slot?
        for svc in wanted {
            for alert in alerts where alert.service == svc.id {
                for e in svc.slots ?? [] where Filter.matches(alert, date: e.date, time: e.time, location: e.location, now: now) {
                    let slot = Slot(date: e.date, time: e.time, office: e.location_name)
                    if best == nil || slot.sortKey < best!.sortKey { best = slot }
                }
            }
        }
        return Snapshot(slot: best, polledAt: polled, hasData: !wanted.isEmpty)
    }
}

// Mirrors app/filters.py matches() (the service is checked by the caller):
// offices "all" or a membership test, the day's ISO weekday in the list, the
// time between start and end inclusive, and, when set, no further than
// maxDaysAhead from today's date in Europe/Berlin (the cities' own zone).
enum Filter {
    static func matches(_ a: Config.Alert, date: String, time: String?, location: String?, now: Date) -> Bool {
        if case .list(let ids)? = a.locations, !ids.contains(location ?? "") { return false }
        let p = date.split(separator: "-").compactMap { Int($0) }
        guard p.count == 3 else { return false }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "Europe/Berlin") ?? .current
        guard let day = cal.date(from: DateComponents(year: p[0], month: p[1], day: p[2], hour: 12)) else { return false }
        if let ahead = a.maxDaysAhead, ahead > 0 {
            let today = cal.startOfDay(for: now)
            let n = cal.dateComponents([.day], from: today, to: cal.startOfDay(for: day)).day ?? 0
            if n > ahead { return false }
        }
        let wd = cal.component(.weekday, from: day)  // 1 = Sunday
        if !(a.weekdays ?? [1, 2, 3, 4, 5, 6, 7]).contains(wd == 1 ? 7 : wd - 1) { return false }
        guard let t = minutes(time), let lo = minutes(a.timeStart ?? "00:00"), let hi = minutes(a.timeEnd ?? "23:59") else { return false }
        return t >= lo && t <= hi
    }

    static func minutes(_ hhmm: String?) -> Int? {
        let p = (hhmm ?? "").split(separator: ":").compactMap { Int($0) }
        return p.count == 2 ? p[0] * 60 + p[1] : nil
    }
}

enum Cache {
    static func load() -> [String: Snapshot] {
        guard let data = Shared.defaults?.data(forKey: Shared.cacheKey) else { return [:] }
        return (try? JSONDecoder().decode([String: Snapshot].self, from: data)) ?? [:]
    }

    static func save(_ cache: [String: Snapshot]) {
        if let data = try? JSONEncoder().encode(cache) { Shared.defaults?.set(data, forKey: Shared.cacheKey) }
    }
}

// MARK: - Words

// The page sends every string; these are only for a widget the page has not
// configured yet (the app never opened since install), in the device language.
private let fallbackStrings: [String: [String: String]] = [
    "de": ["widget.openApp": "Lege in der App einen Alarm an, dann zeigt dieses Widget den frühesten freien Termin.",
           "widget.noMatch": "Gerade kein passender Termin", "widget.noSnapshot": "Noch keine Daten",
           "widget.asOf": "Stand {time}"],
    "en": ["widget.openApp": "Set up an alert in the app and this widget shows the earliest free slot.",
           "widget.noMatch": "No matching slot right now", "widget.noSnapshot": "No data yet",
           "widget.asOf": "As of {time}"],
]

struct Words {
    let strings: [String: String]

    init(_ strings: [String: String]?) {
        if let strings, !strings.isEmpty {
            self.strings = strings
        } else {
            let lang = Locale.current.language.languageCode?.identifier == "de" ? "de" : "en"
            self.strings = fallbackStrings[lang] ?? [:]
        }
    }

    func callAsFunction(_ key: String, _ vars: [String: String] = [:]) -> String {
        var s = strings[key] ?? key
        for (k, v) in vars { s = s.replacingOccurrences(of: "{\(k)}", with: v) }
        return s
    }

    // "Do., 8. Okt." / "Thu 8 Oct", "heute" / "tomorrow": the page's own
    // formatDay, from its own tables.
    func day(_ iso: String, now: Date = Date()) -> String {
        let parts = iso.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return iso }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = .current
        let comps = DateComponents(year: parts[0], month: parts[1], day: parts[2], hour: 12)
        guard let date = cal.date(from: comps) else { return iso }
        if cal.isDate(date, inSameDayAs: now) { return self("date.today") }
        if let tomorrow = cal.date(byAdding: .day, value: 1, to: now), cal.isDate(date, inSameDayAs: tomorrow) {
            return self("date.tomorrow")
        }
        let weekday = cal.component(.weekday, from: date)  // 1 = Sunday
        let isoWeekday = weekday == 1 ? 7 : weekday - 1
        return self("date.dayMonth", ["wd": self("weekday.\(isoWeekday)"), "d": String(parts[2]), "m": self("month.\(parts[1])")])
    }

    func time(_ t: String) -> String { self("date.time", ["time": t]) }

    func slot(_ s: Slot) -> String {
        let d = day(s.date)
        guard let t = s.time else { return d }
        return self("date.atTime", ["day": d, "time": time(t)])
    }

    func asOf(_ date: Date?) -> String? {
        guard let date else { return nil }
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return self("widget.asOf", ["time": f.string(from: date)])
    }
}

// MARK: - Timeline

struct Row: Identifiable {
    let id: String
    let name: String
    let office: String
    let snapshot: Snapshot?
}

struct SlotEntry: TimelineEntry {
    let date: Date
    let words: Words
    let rows: [Row]  // empty: not configured, or every alert is gone
}

struct SlotProvider: TimelineProvider {
    func placeholder(in context: Context) -> SlotEntry { sample() }

    func getSnapshot(in context: Context, completion: @escaping (SlotEntry) -> Void) {
        if context.isPreview { completion(sample()); return }
        Task { completion(await load()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<SlotEntry>) -> Void) {
        Task {
            let entry = await load()
            // Not a promise: WidgetKit decides when the refresh really runs.
            let next = Date().addingTimeInterval(20 * 60)
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    private func sample() -> SlotEntry {
        let words = Words(["widget.asOf": "{time}", "date.time": "{time}", "date.atTime": "{day}, {time}",
                           "date.dayMonth": "{wd} {d} {m}", "weekday.1": "Mon", "weekday.2": "Tue",
                           "weekday.3": "Wed", "weekday.4": "Thu", "weekday.5": "Fri", "weekday.6": "Sat",
                           "weekday.7": "Sun", "date.today": "today", "date.tomorrow": "tomorrow"])
        let slot = Slot(date: "2026-01-01", time: "09:30", office: "Bürgeramt")
        return SlotEntry(date: Date(), words: words,
                         rows: [Row(id: "x", name: "Bürgerwecker", office: "", snapshot: Snapshot(slot: slot, polledAt: Date(), hasData: true))])
    }

    private func load() async -> SlotEntry {
        guard let config = Config.load() else { return SlotEntry(date: Date(), words: Words(nil), rows: []) }
        let lang = config.lang ?? "de"
        let cities = config.cities
        var cache = Cache.load()
        var fresh: [String: Snapshot] = [:]
        await withTaskGroup(of: (String, Snapshot?).self) { group in
            for c in cities {
                group.addTask { (c.slug, await SlotsAPI.fetch(slug: c.slug, alerts: c.alerts ?? [], lang: lang)) }
            }
            for await (slug, snap) in group { if let snap { fresh[slug] = snap } }
        }
        // Only the cities still in the list stay in the cache, so a removed
        // alert leaves nothing behind.
        // The fetch above can take a while; "Delete my data" may have cleared
        // the list meanwhile. Re-read it: with it gone nothing is saved (or
        // kept), and only slugs the current list still names are.
        guard let current = Config.load() else {
            Shared.defaults?.removeObject(forKey: Shared.cacheKey)
            return SlotEntry(date: Date(), words: Words(nil), rows: [])
        }
        let slugs = Set(current.cities.map { $0.slug })
        cache = cache.filter { slugs.contains($0.key) }
        for (slug, snap) in fresh where slugs.contains(slug) { cache[slug] = snap }
        Cache.save(cache)
        let rows = current.cities.map { Row(id: $0.slug, name: $0.name, office: $0.office ?? "", snapshot: cache[$0.slug]) }
        return SlotEntry(date: Date(), words: Words(current.strings), rows: rows)
    }
}

// MARK: - Views

private func ordered(_ rows: [Row]) -> [Row] {
    // Cities with a slot first, earliest first; the rest keep the app's order.
    let withSlot = rows.filter { $0.snapshot?.slot != nil }.sorted { $0.snapshot!.slot!.sortKey < $1.snapshot!.slot!.sortKey }
    return withSlot + rows.filter { $0.snapshot?.slot == nil }
}

private func shortOffice(_ s: Slot?) -> String? {
    guard let office = s?.office, !office.isEmpty, office.count <= 28 else { return nil }
    return office
}

struct SlotView: View {
    let entry: SlotEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        Group {
            if entry.rows.isEmpty {
                Text(entry.words("widget.openApp")).font(.footnote).multilineTextAlignment(.leading)
            } else if family == .systemSmall {
                small(ordered(entry.rows)[0])
            } else {
                medium(Array(ordered(entry.rows).prefix(3)))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .widgetURL(Shared.openURL)
        .containerBackground(.fill.tertiary, for: .widget)
    }

    @ViewBuilder private func small(_ row: Row) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(row.name).font(.caption.weight(.semibold)).foregroundStyle(.secondary).lineLimit(1)
            if let slot = row.snapshot?.slot {
                Text(entry.words.day(slot.date)).font(.title3.weight(.bold)).lineLimit(1).minimumScaleFactor(0.7)
                if let t = slot.time { Text(entry.words.time(t)).font(.title3) }
                if let office = shortOffice(slot) { Text(office).font(.caption2).foregroundStyle(.secondary).lineLimit(1) }
            } else {
                Text(emptyText(row)).font(.footnote)
            }
            Spacer(minLength: 0)
            asOf(row)
        }
    }

    @ViewBuilder private func medium(_ rows: [Row]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(rows) { row in
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 0) {
                        Text(row.name).font(.subheadline.weight(.semibold)).lineLimit(1)
                        if let office = shortOffice(row.snapshot?.slot) {
                            Text(office).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer(minLength: 8)
                    if let slot = row.snapshot?.slot {
                        Text(entry.words.slot(slot)).font(.subheadline).lineLimit(1).minimumScaleFactor(0.7)
                    } else {
                        Text(emptyText(row)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
            Spacer(minLength: 0)
            if let newest = rows.compactMap({ $0.snapshot?.polledAt }).max() {
                if let text = entry.words.asOf(newest) { Text(text).font(.caption2).foregroundStyle(.secondary) }
            }
        }
    }

    private func emptyText(_ row: Row) -> String {
        entry.words(row.snapshot?.hasData == true ? "widget.noMatch" : "widget.noSnapshot")
    }

    @ViewBuilder private func asOf(_ row: Row) -> some View {
        if let text = entry.words.asOf(row.snapshot?.polledAt) {
            Text(text).font(.caption2).foregroundStyle(.secondary)
        }
    }
}

@main
struct BuergerweckerWidget: Widget {
    let kind = "BuergerweckerWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SlotProvider()) { entry in
            SlotView(entry: entry)
        }
        .configurationDisplayName("Earliest slot")
        .description("The earliest free slot in your cities.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
