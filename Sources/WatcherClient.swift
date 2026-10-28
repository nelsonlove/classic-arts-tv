import Foundation
import Combine

/// One credit card the watcher read off the screen.
struct Card: Identifiable, Decodable, Equatable {
    let id: Int
    let firstSeen: String
    let lastSeen: String
    let lines: [String]
    let cleanLines: [String]?
    let category: String?
    let thumb: String
    let urls: [String]?
    let entities: [Entity]?
    let search: [String: String]?

    enum CodingKeys: String, CodingKey {
        case id, lines, thumb, category, urls, entities, search
        case cleanLines = "clean_lines"
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
    }

    /// A named person, group or work on the card, with its Wikipedia page when the watcher found one.
    struct Entity: Decodable, Equatable, Identifiable {
        let name: String
        let kind: String
        let wikipedia: String?
        let summary: String?
        let sure: Bool?
        var id: String { kind + ":" + name }
    }

    /// Everything the card links to: Wikipedia pages, printed web addresses, then search links.
    var links: [(label: String, url: URL, sure: Bool)] {
        var out: [(String, URL, Bool)] = []
        for e in entities ?? [] {
            if e.sure == true, let w = e.wikipedia, let u = URL(string: w) { out.append((e.name, u, true)) }
        }
        for s in urls ?? [] {
            if let u = URL(string: s) { out.append((u.host ?? s, u, true)) }
        }
        if let y = search?["youtube"], let u = URL(string: y) { out.append(("YouTube", u, true)) }
        if let a = search?["apple_music"], let u = URL(string: a) { out.append(("Apple Music", u, true)) }
        return out
    }

    /// The model-corrected reading when the watcher has one, else the raw OCR.
    var displayLines: [String] {
        if let cleanLines, !cleanLines.isEmpty { return cleanLines }
        return lines
    }

    var text: String { displayLines.joined(separator: "\n") }
    var when: Date? { ISO8601DateFormatter().date(from: firstSeen) }
}

/// Polls the watcher service for what is playing and what played recently.
@MainActor
final class WatcherClient: ObservableObject {
    @Published private(set) var now: Card?
    @Published private(set) var recent: [Card] = []
    @Published private(set) var reachable = true

    /// The watcher's base URL: `watcherURL` in UserDefaults wins, else `CASWatcherURL` from Info.plist (set by `CAS_WATCHER_URL` in Local.xcconfig), else none.
    static let overrideKey = "watcherURL"

    var base: URL? {
        if let text = UserDefaults.standard.string(forKey: Self.overrideKey), let url = URL(string: text) {
            return url
        }
        if let text = Bundle.main.object(forInfoDictionaryKey: "CASWatcherURL") as? String, !text.isEmpty, let url = URL(string: text) {
            return url
        }
        return nil
    }

    private var poller: Task<Void, Never>?
    private let decoder = JSONDecoder()

    func start(every seconds: Double = 30) {
        poller?.cancel()
        poller = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(seconds))
            }
        }
    }

    func stop() {
        poller?.cancel()
        poller = nil
    }

    func refresh() async {
        struct NowReply: Decodable { let now: Card? }
        struct RecentReply: Decodable { let cards: [Card] }
        guard let base else {
            reachable = false
            return
        }
        do {
            async let nowData = URLSession.shared.data(from: base.appending(path: "api/now"))
            async let recentData = URLSession.shared.data(from: base.appending(path: "api/recent").appending(queryItems: [URLQueryItem(name: "limit", value: "50")]))
            let (nowBytes, _) = try await nowData
            let (recentBytes, _) = try await recentData
            now = try decoder.decode(NowReply.self, from: nowBytes).now
            recent = try decoder.decode(RecentReply.self, from: recentBytes).cards
            reachable = true
        } catch {
            reachable = false
        }
    }

    func thumbURL(_ card: Card) -> URL? {
        base.flatMap { URL(string: card.thumb, relativeTo: $0) }
    }
}
