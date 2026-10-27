# Classic Arts TV

An iPhone, iPad and Apple TV app that plays the Classic Arts Showcase live stream, full screen, with nothing else on it. Classic Arts Showcase is a free, non-commercial 24-hour arts channel; its own web player serves a public HLS playlist, and this app plays that playlist in the native player, so AirPlay, the lock screen controls and the Apple TV remote all work as they do for any other video.

## Build

You need Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`). The Xcode project is generated from `project.yml` and is not committed.

```sh
xcodegen generate
open ClassicArts.xcodeproj
```

In Xcode, pick the `ClassicArts` scheme, pick your iPhone or Apple TV as the destination, and press Run. The first run on a device asks you to sign in with your Apple ID under Signing & Capabilities if Xcode does not already know it.

An Apple TV is paired from the Apple TV side: Settings > Remotes and Devices > Remote App and Devices, then it appears in Xcode > Window > Devices and Simulators.

A free Apple ID (a Personal Team) signs a build that runs for 7 days; after that, run it again from Xcode. A paid developer account lifts that limit and allows TestFlight.

## Change the stream

The playlist URL lives in `Sources/Stream.swift`. To try another playlist without a rebuild, set the `streamURL` string in the app's UserDefaults.

## Rights

Classic Arts Showcase gives its feed away free, but this app is for personal use. Publishing it on the App Store would need their written agreement first.
