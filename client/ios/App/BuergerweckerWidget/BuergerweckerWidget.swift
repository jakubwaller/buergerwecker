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
        let services: [String]?
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
        let location_name: String?
    }
    struct Service: Decodable {
        let id: String
        let polled_at: String?
        let earliest: Earliest?
    }
    let services: [Service]
}

enum SlotsAPI {
    // nil on any failure (network, 404 while the gate is closed, bad JSON):
    // the caller falls back to the cache.
    static func fetch(slug: String, services: [String], lang: String) async -> Snapshot? {
        var comps = URLComponents(string: "\(Shared.apiBase)/cities/\(slug)/slots")
        comps?.queryItems = [URLQueryItem(name: "lang", value: lang)]
        guard let url = comps?.url else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 15
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let body = try? JSONDecoder().decode(SlotsResponse.self, from: data) else { return nil }
        let wanted = body.services.filter { services.isEmpty || services.contains($0.id) }
        let iso = ISO8601DateFormatter()
        let polled = wanted.compactMap { $0.polled_at.flatMap(iso.date(from:)) }.max()
        let slots = wanted.compactMap { s in
            s.earliest.map { Slot(date: $0.date, time: $0.time, office: $0.location_name) }
        }
        return Snapshot(slot: slots.min { $0.sortKey < $1.sortKey }, polledAt: polled, hasData: !wanted.isEmpty)
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
           "widget.noSlots": "Gerade kein freier Termin", "widget.noSnapshot": "Noch keine Daten",
           "widget.asOf": "Stand {time}"],
    "en": ["widget.openApp": "Set up an alert in the app and this widget shows the earliest free slot.",
           "widget.noSlots": "No free slot right now", "widget.noSnapshot": "No data yet",
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
                group.addTask { (c.slug, await SlotsAPI.fetch(slug: c.slug, services: c.services ?? [], lang: lang)) }
            }
            for await (slug, snap) in group { if let snap { fresh[slug] = snap } }
        }
        // Only the cities still in the list stay in the cache, so a removed
        // alert leaves nothing behind.
        cache = cache.filter { key, _ in cities.contains { $0.slug == key } }
        for (slug, snap) in fresh { cache[slug] = snap }
        Cache.save(cache)
        let rows = cities.map { Row(id: $0.slug, name: $0.name, office: $0.office ?? "", snapshot: cache[$0.slug]) }
        return SlotEntry(date: Date(), words: Words(config.strings), rows: rows)
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
        entry.words(row.snapshot?.hasData == true ? "widget.noSlots" : "widget.noSnapshot")
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
