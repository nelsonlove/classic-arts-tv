import Foundation

/// Where the live stream comes from.
enum Stream {
    /// The public HLS master playlist that the Classic Arts Showcase web player uses.
    static let defaultURL = URL(string: "https://classicarts.akamaized.net/hls/live/1024257/CAS/master.m3u8")!

    /// Set this key in UserDefaults to point the app at a different playlist without a rebuild.
    static let overrideKey = "streamURL"

    static var url: URL {
        if let text = UserDefaults.standard.string(forKey: overrideKey), let url = URL(string: text) {
            return url
        }
        return defaultURL
    }
}
