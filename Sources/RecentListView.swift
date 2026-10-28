import SwiftUI

/// What is playing now and what played before it, as the watcher read them off the screen credits.
struct RecentListView: View {
    @ObservedObject var client: WatcherClient

    /// The card to show as now playing: the watcher's own answer, else the latest card it has.
    private var current: Card? { client.now ?? client.recent.first }

    var body: some View {
        List {
            Section {
                if let current {
                    CardRow(card: current, client: client, showsAge: true)
                } else {
                    Text(client.base == nil ? "No watcher is set. Put its address in Local.xcconfig or under watcherURL in UserDefaults." : client.reachable ? "Nothing read yet." : "The watcher is not reachable.")
                        .foregroundStyle(.secondary)
                }
            } header: {
                Text("Now playing")
            } footer: {
                if client.now == nil, current != nil {
                    Text("The last credits the watcher read. Credits show at the start and end of each clip.")
                }
            }
            Section("Recently played") {
                let rest = client.recent.filter { $0.id != current?.id }
                if rest.isEmpty {
                    Text("Nothing else yet.").foregroundStyle(.secondary)
                }
                ForEach(rest) { card in
                    CardRow(card: card, client: client, showsAge: false)
                }
            }
        }
        .refreshable { await client.refresh() }
    }
}

struct CardRow: View {
    let card: Card
    let client: WatcherClient
    var showsAge = false

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            AsyncImage(url: client.thumbURL(card)) { image in
                image.resizable().aspectRatio(contentMode: .fit)
            } placeholder: {
                Color.secondary.opacity(0.2)
            }
            .frame(width: thumbWidth)
            .clipShape(RoundedRectangle(cornerRadius: 4))
            VStack(alignment: .leading, spacing: 4) {
                Text(card.text)
                    .font(.body)
                #if !os(tvOS)
                if !card.links.isEmpty {
                    FlowLinks(links: card.links)
                }
                #endif
                if let when = card.when {
                    let stamp = showsAge ? "Read \(when.formatted(.relative(presentation: .named)))" : when.formatted(date: .abbreviated, time: .shortened)
                    Text(card.category.map { "\($0) · \(stamp)" } ?? stamp)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private var thumbWidth: CGFloat {
        #if os(tvOS)
        240
        #else
        96
        #endif
    }
}

#if !os(tvOS)
/// The card's links in a wrapping row; a doubtful Wikipedia match is shown dimmer.
struct FlowLinks: View {
    let links: [(label: String, url: URL, sure: Bool)]

    var body: some View {
        // A simple wrap: LazyVGrid with adaptive columns keeps it readable on narrow phones.
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 90), alignment: .leading)], alignment: .leading, spacing: 4) {
            ForEach(Array(links.enumerated()), id: \.offset) { _, link in
                Link(link.label, destination: link.url)
                    .font(.caption)
                    .opacity(link.sure ? 1 : 0.6)
                    .lineLimit(1)
            }
        }
    }
}
#endif
