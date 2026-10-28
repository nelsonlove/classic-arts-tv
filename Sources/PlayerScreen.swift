import SwiftUI
import AVKit

/// One full-screen live player, with a spinner while it tunes in, a retry button when it fails, and the watcher's credits.
struct PlayerScreen: View {
    @StateObject private var model = PlayerModel()
    @StateObject private var client = WatcherClient()
    @Environment(\.scenePhase) private var scenePhase
    @State private var bannerCard: Card?
    @State private var showList = false
    @State private var bannerTask: Task<Void, Never>?

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            player.ignoresSafeArea()
            overlay
        }
        .overlay(alignment: .top) {
            if let card = bannerCard { NowPlayingBanner(card: card) }
        }
        .animation(.easeInOut, value: bannerCard)
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                model.resumeIfNeeded()
                client.start()
            } else {
                client.stop()
            }
        }
        .onChange(of: client.now) { old, new in
            guard let new, new.id != old?.id else { return }
            showBanner(new)
        }
        .onAppear { client.start() }
        #if os(iOS)
        .statusBarHidden(true)
        .persistentSystemOverlays(.hidden)
        #endif
        #if os(iOS) || os(macOS)
        .overlay(alignment: .bottomTrailing) {
            Button { showList = true } label: {
                Image(systemName: "list.bullet")
                    .font(.title3)
                    .padding(10)
                    .background(.ultraThinMaterial, in: Circle())
            }
            .padding()
            .opacity(0.8)
        }
        .sheet(isPresented: $showList) {
            NavigationStack {
                RecentListView(client: client)
                    .navigationTitle("What's playing")
                    .toolbar { Button("Done") { showList = false } }
            }
        }
        #endif
    }

    @ViewBuilder
    private var player: some View {
        #if os(tvOS)
        TVPlayerView(player: model.player, client: client)
        #elseif os(macOS)
        MacPlayerView(player: model.player)
        #else
        VideoPlayer(player: model.player)
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

    private func showBanner(_ card: Card) {
        bannerTask?.cancel()
        bannerCard = card
        bannerTask = Task {
            try? await Task.sleep(for: .seconds(12))
            if !Task.isCancelled { bannerCard = nil }
        }
    }
}

#if os(tvOS)
/// The native Apple TV player, with the recently-played list as a tab in its Info panel (swipe down on the remote).
struct TVPlayerView: UIViewControllerRepresentable {
    let player: AVPlayer
    let client: WatcherClient

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let controller = AVPlayerViewController()
        controller.player = player
        let info = UIHostingController(rootView: RecentListView(client: client))
        info.title = "What's playing"
        info.preferredContentSize = CGSize(width: 1920, height: 560)
        controller.customInfoViewControllers = [info]
        return controller
    }

    func updateUIViewController(_ controller: AVPlayerViewController, context: Context) {}
}
#endif

#if os(macOS)
/// The AppKit player, with Picture in Picture and the full-screen button in its controls.
struct MacPlayerView: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        view.controlsStyle = .floating
        view.allowsPictureInPicturePlayback = true
        view.showsFullScreenToggleButton = true
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {}
}
#endif
