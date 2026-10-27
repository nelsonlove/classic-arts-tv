import SwiftUI

/// What is playing now and what played before it, as the watcher read them off the screen credits.
struct RecentListView: View {
    @ObservedObject var client: WatcherClient

    var body: some View {
        List {
            if let now = client.now {
                Section("Now playing") { CardRow(card: now, client: client) }
            }
            Section(client.now == nil ? "Recently played" : "Before that") {
                if client.recent.isEmpty {
                    Text(client.reachable ? "Nothing read yet." : "The watcher is not reachable. Are you on the tailnet?")
                        .foregroundStyle(.secondary)
                }
                ForEach(client.recent.filter { $0.id != client.now?.id }) { card in
                    CardRow(card: card, client: client)
                }
            }
        }
        .refreshable { await client.refresh() }
    }
}

struct CardRow: View {
    let card: Card
    let client: WatcherClient

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
                if let when = card.when {
                    Text(when.formatted(date: .abbreviated, time: .shortened))
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
