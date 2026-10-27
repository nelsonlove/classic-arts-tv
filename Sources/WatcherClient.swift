import Foundation
import Combine

/// One credit card the watcher read off the screen.
struct Card: Identifiable, Decodable, Equatable {
    let id: Int
    let firstSeen: String
    let lastSeen: String
    let lines: [String]
    let thumb: String

    enum CodingKeys: String, CodingKey {
        case id, lines, thumb
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
    }

    var text: String { lines.joined(separator: "\n") }
    var when: Date? { ISO8601DateFormatter().date(from: firstSeen) }
}

/// Polls the watcher service for what is playing and what played recently.
@MainActor
final class WatcherClient: ObservableObject {
    @Published private(set) var now: Card?
    @Published private(set) var recent: [Card] = []
    @Published private(set) var reachable = true

    /// The watcher's base URL. Override it in UserDefaults under `watcherURL` without a rebuild.
    static let defaultBase = URL(string: "https://host.example.com/classicarts")!
    static let overrideKey = "watcherURL"

    var base: URL {
        if let text = UserDefaults.standard.string(forKey: Self.overrideKey), let url = URL(string: text) {
            return url
        }
        return Self.defaultBase
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
        URL(string: card.thumb, relativeTo: base)
    }
}
