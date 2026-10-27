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

## What's playing

Classic Arts Showcase publishes no playlist, and the stream carries no metadata. The only record of a clip is the credit card burned into the picture at its start and end. The `watcher/` service reads those cards: it decodes one frame every 5 seconds, runs tesseract on it, keeps the frames that read like credits, and stores each card once with a thumbnail. It serves a web page and a small JSON API:

- `GET /api/now`: the latest card, if it was seen in the last 15 minutes
- `GET /api/recent?limit=50`: the latest cards, newest first
- `GET /thumbs/<id>.jpg`: the frame the card was first read from
- `GET /health`: frames seen and whether ffmpeg is running

Install it on any Debian or Ubuntu host with `sudo sh watcher/install.sh` from the `watcher/` folder; settings go in `/etc/cas-watcher.env` (see `cas-watcher.env.example`). It needs about 5% of one CPU thread, under 250 MB of memory, and the stream's bandwidth around the clock: about 13 GB a day at 360p, 18 GB at 480p (set `CAS_STREAM_URL` to the 480p playlist for cleaner reads of names).

The app polls the watcher every 30 seconds, shows a banner when a new card is read, and lists what played: on iPhone and iPad behind the list button, on Apple TV as a tab in the player's Info panel (swipe down on the remote). The watcher's address is in `Sources/WatcherClient.swift`; override it in UserDefaults under `watcherURL`.
