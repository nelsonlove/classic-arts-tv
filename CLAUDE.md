# classic-arts-tv

An iPhone, iPad, Apple TV and Mac app that plays the Classic Arts Showcase stream, and a watcher service (`watcher/`) that reads the on-screen credits into a now-playing log. The README says how each is built and run.

## Merge rule

Nelson, 2026-10-28, in the session that built this repository: "merge em. you have standing permission to merge". So in this repository the session that owns the work merges its own PR once the PR's independent review has closed with no serious finding, pinned to the reviewed head sha, and tells him after. His word is not needed per PR. A release, a change that spends money, a change to this rule, and anything that sends his data outside still wait for his word.

## Rules of the house

- Nothing personal in tracked files: the Apple team ID and the watcher's address live in the gitignored `Local.xcconfig`; the watcher's settings live in `/etc/cas-watcher.env` on its host. Check `git grep` for a tailnet name or a team ID before every push.
- The watcher is general: every setting is an environment variable with a sane default, and nothing in its code names a host, a container or a fleet.
- Divorce details never appear here; this repository is public.
