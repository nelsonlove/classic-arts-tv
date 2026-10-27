import SwiftUI
import AVKit

/// One full-screen live player, with a spinner while it tunes in and a retry button when it fails.
struct PlayerScreen: View {
    @StateObject private var model = PlayerModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            VideoPlayer(player: model.player)
                .ignoresSafeArea()
            overlay
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { model.resumeIfNeeded() }
        }
        #if os(iOS)
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        #endif
    }

    @ViewBuilder
    private var overlay: some View {
        switch model.status {
        case .loading:
            ProgressView("Tuning in…")
                .tint(.white)
                .foregroundStyle(.white)
        case .failed(let message):
            VStack(spacing: 16) {
                Text("Stream unavailable")
                    .font(.title2)
                Text(message)
                    .font(.footnote)
                    .multilineTextAlignment(.center)
                Button("Try again") { model.load() }
            }
            .foregroundStyle(.white)
            .padding()
        case .playing:
            EmptyView()
        }
    }
}
