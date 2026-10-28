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
                if let links = card.links, !links.isEmpty {
                    FlowLinks(links: links)
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
/// The card's links in a wrapping row.
struct FlowLinks: View {
    let links: [Card.LinkItem]

    var body: some View {
        Flow(spacing: 10) {
            ForEach(links) { link in
                if let url = link.destination {
                    Link(link.label, destination: url)
                        .font(.caption)
                        .lineLimit(1)
                        .help(link.note ?? "")
                }
            }
        }
    }
}

/// A plain wrapping layout: items go left to right and wrap to the next line. Not lazy, so a List row measures it right.
struct Flow: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += rowHeight + spacing / 2
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: width == .infinity ? x : width, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing / 2
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), proposal: .unspecified)
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
#endif
