import SwiftUI

@main
struct ClassicArtsApp: App {
    var body: some Scene {
        WindowGroup {
            PlayerScreen()
        }
        #if os(macOS)
        .defaultSize(width: 960, height: 540)
        #endif
    }
}
