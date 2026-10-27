import SwiftUI

/// A short banner at the top of the picture when a new credit card is read.
struct NowPlayingBanner: View {
    let card: Card

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Now playing").font(.caption).textCase(.uppercase).foregroundStyle(.secondary)
            Text(card.text).font(.callout).lineLimit(6)
        }
        .padding(12)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10))
        .padding()
        .transition(.move(edge: .top).combined(with: .opacity))
    }
}
