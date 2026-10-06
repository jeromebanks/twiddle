# M2: Set alarms from the command line

Built from `epic/12` at 6efc6bb7a4, reviewed by Codex (round 3, approved).

## Steps as run
- Read-only and `--dry-run` pictures: run by the agent (`alarm list`, `sources`, `add`/`edit`/`rm --dry-run`).
- Real-speaker steps (KALX, sound, Bandcamp alarms, fallback, edit/delete/recreate, try/snooze/stop): run by the poster's reply on #12 (below); the Spotify alarm was not verified (rate limited).

> The alarms all worked as you would expect them to. For the "gentle rise", it did a few ambient rising tones before turning to the normal Sonos alarm "ding dong". Unable to verify the spotify because of rate limiting, but bandcamp and KALX streaming worked.

## Known gaps
- Spotify alarm unverified; fully-off-network fallback and snooze/stop not individually reported.
- No `alarm recreate` verb.
- Tech debt #135, #136.

Pictures: alarm-list.svg, sources.svg, add-dry.svg, edit-dry.svg, rm-dry.svg.
