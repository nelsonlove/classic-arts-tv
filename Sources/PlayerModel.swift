import AVFoundation
import MediaPlayer
import Combine

/// Owns the AVPlayer, reports its state, and reloads the live stream when it fails or falls asleep.
@MainActor
final class PlayerModel: ObservableObject {
    enum Status: Equatable {
        case loading
        case playing
        case failed(String)
    }

    let player = AVPlayer()
    @Published private(set) var status: Status = .loading

    private var itemObserver: NSKeyValueObservation?
    private var endObserver: NSObjectProtocol?

    init() {
        player.allowsExternalPlayback = true
        player.automaticallyWaitsToMinimizeStalling = true
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .moviePlayback)
        try? session.setActive(true)
        load()
    }

    /// Starts the stream from the live edge. Safe to call again at any time.
    func load() {
        status = .loading
        let item = AVPlayerItem(url: Stream.url)
        itemObserver = item.observe(\.status, options: [.new]) { [weak self] item, _ in
            let newStatus: Status?
            switch item.status {
            case .readyToPlay: newStatus = .playing
            case .failed: newStatus = .failed(item.error?.localizedDescription ?? "The stream did not load.")
            default: newStatus = nil
            }
            guard let newStatus else { return }
            Task { @MainActor in self?.status = newStatus }
        }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemFailedToPlayToEndTime, object: item, queue: .main
        ) { [weak self] note in
            let error = note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
            Task { @MainActor in self?.status = .failed(error?.localizedDescription ?? "The stream stopped.") }
        }
        player.replaceCurrentItem(with: item)
        player.play()
        publishNowPlaying()
    }

    /// Called when the app comes back to the front. A live stream that sat paused is behind, so reload it.
    func resumeIfNeeded() {
        if player.timeControlStatus != .playing { load() }
    }

    private func publishNowPlaying() {
        MPNowPlayingInfoCenter.default().nowPlayingInfo = [
            MPMediaItemPropertyTitle: "Classic Arts Showcase",
            MPMediaItemPropertyArtist: "Live",
            MPNowPlayingInfoPropertyIsLiveStream: true,
        ]
    }
}
