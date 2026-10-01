# CLAUDE.md — working in this repo

twiddle is three tools that share one codebase: `dial` (every radio station's
now-playing, one key to listen), `scene` (tonight's local shows and who the
bands are) and the Sonos tools (control by room name, a Spotify relay, and
diagnostics for speakers that drop out). Its mascot is Twiddle
(`CHARACTERS.md`). This file is how to work in the code without breaking
anything or anybody's speakers.

| What you want | Where it is |
|---|---|
| What twiddle is, and getting started | `README.md`, `docs/QUICKSTART.md` |
| Every command; how the Sonos side measures things, and its traps | `docs/GUIDE.md` |
| The `scene` TUI: keys, sources, extending it | `docs/SCENE.md` |
| How `scene` works inside: builder, dataset, what it pulls from where | `docs/SCENE-ARCHITECTURE.md` |
| Playing Spotify through a relay instead of Sonos's cloud | `docs/SPOTIFY.md` |
| Twiddle and any other characters | `CHARACTERS.md` |
| Adding a station / a venue / a visualizer | skills `add-radio-station`, `add-venue-source`, `add-visualizer` |

`uv sync` then `uv run pytest` (about 1150 tests, a minute). Nothing in the
tests touches the network, a speaker or Spotify.

## Read-only vs writing

These commands can change what a real speaker in someone's home is doing, so
know which half you're in. Room names in the examples (`roam`, `Living Room`)
are the author's; `twiddle rooms` lists yours.

The CLI has two halves: control verbs that name a room, and `diag` for
everything that measures. The old top-level diagnostic spellings still work as
aliases — the launchd plist depends on them.

| Command | Effect |
|---|---|
| `rooms`, `status`, `snapshot` | **read-only — safe any time** |
| `relay doctor`, `relay measure`, `relay login`, `relay status` | **read-only — touch no speaker** |
| `spotify auth/devices/search/now` | **read-only — Spotify API only, no speaker** |
| `np`, `np -i`, `info` | **read-only — stations' sites + MusicBrainz/Wikipedia/Discogs/Bandcamp, no speaker** |
| `comedy artists/refresh/new`, `comedy rate` | **read-only, except `rate`** — `rate` only appends to `logs/comedy_played.jsonl`, no speaker |
| `scene list`, `scene venue`, `scene schedule` | **read-only — read the local dataset (`~/.local/share/twiddle/scene/dataset.json`), no network, no speaker** (`scene list --refresh` runs `scene build` first) |
| `scene build` | **network; writes only the dataset (+ its caches) — no speaker.** The only thing that scrapes The List / the venues' own listings / Wikipedia / MusicBrainz / Bandcamp (and Spotify identity, only if already signed in). One-shot, locked, never interactive; run it on a timer (`scene schedule` prints the launchd agent) |
| `scene login` | **read-only — a Spotify sign-in for the Mac's own player; no speaker** |
| `scene` (TUI) | **reads the dataset; browsing scrapes nothing and bulk-enriches nothing** (`r` runs `scene build` in a subprocess; with no dataset it builds one once). **Writes only on `p`/`R`/`d`** — Spotify always; a speaker only when the device is the relay. A **Bandcamp** track needs no Spotify: on the relay device it re-points the Roam itself (journalled; asks twice and pauses Spotify if music is playing through the relay), on This Mac it's `ffplay`. The genre scan is read-only: throttled Bandcamp searches, cached 14 days. Interrupting anything playing elsewhere asks twice; taking Spotify *off* a live relay also journals `spotify_transfer_from_relay` and saves what was playing for `R`. A Bandcamp track follows `d` to the new output and stops on the old; any Spotify play stops one left elsewhere |
| `stations [--tag]`, `stations tags/search/probe` | **read-only — the catalog files / radio-browser.info, SomaFM, TuneIn / one stream + the station's site; no speaker, and nothing written (adding a station is writing a catalog file: skill `add-radio-station`)** |
| `dial list [--tag]` | **read-only — stations' sites only, no speaker** |
| `v` in `dial` / `scene`, `viz list/snapshot/demo` | **read-only — no speaker.** Its own ffmpeg decode of the playing stream (a second listener); on the relay only as an *observer* (`?observer`), never counted in `listeners`/`clients_total`/`dropped_chunks`; an older relay without `X-Twiddle-Observer` is not tapped at all. Shows the stream, **not** the speaker: never evidence that sound came out |
| `dial` (TUI) | **writes only on `enter`/`s`/`R`/`d`/volume/mute** (`t` filters by tag: display only) — to the chosen Sonos room (journalled) or this Mac (`ffplay` + macOS volume). Spotify is consulted only when the room is on the relay; if music is playing through it, tuning asks twice, pauses Spotify, journals `spotify_paused_for_radio`; `R` puts it back. `d` moves a playing station to the new output and stops it on the old (only if that still plays what dial started; never the relay) |
| `diag scan/ping/watch/analyse/baseline/baseline-diff`, `daemon status` | **read-only — safe any time** |
| `play`, `pause`, `stop`, `next`, `prev`, `volume`/`vol`, `mute`, `bass`, `treble`, `balance`, `loudness`, `shuffle`, `repeat`, `stream`, `restore`, `group`, `ungroup`, `sleep <duration|off>` | **writes to a speaker** (bare `sleep` only reads the timer; setting one journals a span around when it will stop the room) |
| `diag serve`, `diag radio`, `diag soak`, `relay start/up/down`, `tune`, `spotify play`, `spotify discover` | **writes transport + volume to a speaker** |
| `comedy sleep` | **writes transport + a native Sonos sleep timer** — plays 2-3 whole albums back-to-back on the target room (default the room `roam`) and arms `ConfigureSleepTimer` to stop it; see `comedy.py` |

Every writing command takes `--dry-run`, which resolves the target and prints
what it would do without touching a speaker. Use it when you are unsure what a
name resolves to.

**Target speakers by name, not IP** (`--room roam`, `--room "Living Room"`).
Bonded followers — the right Roam, both surrounds — cannot take commands; the
CLI routes to the coordinator and says so in its output. `Invisible`, not
`is_satellite`, is what marks a speaker unaddressable, and a group's
coordinator comes from the `Coordinator` attribute, never the group ID prefix.

While waiting to capture a real fault, avoid the writing commands: they pollute
the evidence. `watch` never writes, which is why its windows are clean.

Every write is journalled to `logs/interventions.jsonl` (including serving
spans), and `analyse` discounts events within 45s of one. **Keep that intact** —
it is the only thing separating real faults from ones you caused.

If you do use them, restore what you found — and there is now a command for
exactly that, so there is no excuse for drifting:

```bash
uv run twiddle snapshot --room roam    # before you touch anything
uv run twiddle restore  --room roam    # after
```

**Do not assume what is playing — ask.** `uv run twiddle status` is the only
trustworthy answer, and `snapshot` captures whatever is actually there rather
than what some doc once claimed. People change sources and volume freely, so
any station or level written down anywhere is stale the moment it is written.

## The daemon

A launchd agent watches continuously and is the source of the best evidence:

```bash
uv run twiddle daemon status                      # is it alive?
uv run twiddle analyse --log logs/daemon.jsonl    # what has it seen?
```

It samples every 30s, anchored to one mains-powered speaker, rotates at 50MB, and is restarted
by launchd if it exits. Its log is **gitignored** (it churns constantly).

If `daemon status` says **STALE**, it is almost certainly macOS Local Network
privacy: a launchd agent cannot show the consent prompt, so it is denied LAN
access — the same request succeeds from a terminal. Fix is
*System Settings → Privacy & Security → Local Network*, enable the `uv` entry;
launchd retries every 60s and recovers on its own.

**Known gap:** the daemon subscribes AVTransport on the anchor only, so it
records no transport state for the other speakers: "what was playing, and
when" can't be recovered for them after the fact.

## Before you trust any number

`docs/GUIDE.md` documents the measurement traps, each of which produced a **wrong
conclusion** first. The two most likely to bite a new analysis:

- **The PHY error counter resets on every read**, so a single sample is
  meaningless. Use `devices.sample_phy_rate()`.
- **A satellite reporting `PLAYING` does not mean sound is coming out** — there
  is a confirmed false negative on record. Use the affected speaker's
  control-port latency and ICMP loss as the dropout proxy.

## Recording new observations — no doc edits needed

**Don't write routine observations into docs.** Prose goes stale and needs
rewriting; an append-only log does not. Use:

```bash
uv run python tools/observe.py add --heard "right speaker dropped" --speaker R
uv run python tools/observe.py add --kind measurement --note "router RSSI: Roam L -74dBm"
uv run python tools/observe.py list          # read them back
```

This writes `logs/observations.jsonl`. **Anything a human *heard* is the
scarcest and most valuable signal when chasing dropouts** — the instruments
cannot detect a silent satellite on their own, so a timestamped "I heard it
drop" is what makes a latency spike scoreable. Record those first.

## Logs

Everything the tools record goes under `logs/`, which is gitignored: it stays
on the machine that wrote it. `interventions.jsonl` (every write),
`observations.jsonl` (what people heard), `relay.jsonl` and the daemon's log
are the ones that matter.

## Layout

Under `src/twiddle/`:

| File | Role |
|---|---|
| `devices.py` | discovery, per-speaker state, SOAP, PHY + link stats |
| `topology.py` | groups, bonds, satellites, `VanishedDevices` |
| `monitor.py` | GENA subscriptions + polling; JSONL; rotation |
| `report.py` | findings, severities, hardware-vs-setup discriminators |
| `daemon.py` | launchd agent install/status |
| `household.py` | speakers, groups, name resolution, snapshot/restore |
| `control_cli.py` | the room-naming control commands |
| `play.py` | HTTP file server + transport control (**writes**) |
| `relay.py` | live audio -> paced PCM -> MP3 -> HTTP fan-out (no speaker writes) |
| `relay_cli.py` | `relay doctor/login/measure/start` (**start writes**) |
| `supervisor.py` | keeps a detached relay alive; PID file, SIGTERM, restore |
| `spotify.py` | Spotify Web API: PKCE auth, search, devices, transport |
| `spotify_cli.py` | `spotify auth/devices/search/now/play/...` (**play writes**) |
| `discover_cli.py` | `spotify discover` — interactive: search an artist, sample tracks, play one (**writes**) |
| `stations/` | the station catalog and now-playing. `catalog/<key>.toml` = one file per station (the only place stream URLs live; format in `model.py`), `tags.py` = the tag vocabulary, `fetchers/` = one module per now-playing mechanism (ICY, Spinitron, SomaFM, Radio France, NTS, KEXP, KQED, WMBR, WFMU, Rainwave, Airtime Pro, streamabc; table in its `__init__`), `icy.py` = ICY parsing incl. iHeart's `tidy_title` and a per-station `encoding` (Shift-JIS), `directory.py` = `stations search`, `probe.py` = `stations probe`. A bad catalog file is skipped with a warning, never raised (the daemon imports this); `test_catalog.py` is strict. Skill `add-radio-station` |
| `stations_cli.py` | `stations [--tag]`, `stations tags`, `stations search`, `stations probe` (all read-only) |
| `streaminfo.py` | what a stream really is (codec/bitrate/rate/channels) via `ffprobe`, cached a week in `~/.cache/twiddle/streams.json`; the dial shows it |
| `lookup.py` | who is this artist / album — MusicBrainz identity, Wikipedia via Wikidata, Discogs members/profile, Discogs → Bandcamp fallbacks; throttled, cached in `~/.cache/twiddle/lookup.json` |
| `radio_cli.py` | `tune` (**writes**), `np [-i] [-d]`, `info` |
| `spotify_ops.py` | Spotify playback logic with no CLI attached: raises `PlaybackError`, reports via `notify`. `spotify_cli`'s helpers are thin wrappers over it |
| `comedy.py` | pick and play something to fall asleep to: classify comedians out of `/me/top/artists` via `lookup.py`, find new releases, build a multi-album queue, arm the native Sonos sleep timer (**`start_queue` writes**) |
| `comedy_cli.py` | `comedy artists/refresh/new/sleep/rate` — `sleep` **writes**, the rest read-only |
| `scenespec/` | the published dataset's shape, the contract between a producer and a client: `dataset.py` = the file's format + atomic publish + reader (`Snapshot`), `model.py` = `Show` + `dedupe`, `band.py` = `BandProfile` + the confidence grades, `profiles.py` = BandProfile ↔ record (+ `apply_pin`), `venue.py` = `Venue`/`VenueInfo` + name matching, `genre.py` = tags → genre families (no AI). **Imports neither `scene` nor `scenedata`, no Textual/network (tests enforce it)** |
| `scenedata/` | the producer (everything bespoke): `builder.py` = `scene build`, the only collector (fetch → enrich → publish, locked, incremental); `sources/` = where shows come from (The List; Yoshi's, Gilman (ShowSlinger), Gray Area, Make-Out Room (CalendarWiz), KALX, and per-platform tables: See Tickets ×5, TicketWeb ×7, VenuePilot ×2, Squarespace, Simple Calendar, which add flyers + ticket links; `model.dedupe` merges them; skill `add-venue-source`); `bands.py` = identity enrichers + `assess` + the Bay Area `REGION` (one serialized lane for `lookup`, which isn't thread-safe); `bandcamp.py` = Bandcamp search + band choice (the throttle and songs are top-level `bandcamp.py`); `venues.py` + `venue_info.py` = the stock venue list, hand-kept addresses/sites, Wikipedia summaries; `cache.py` = aliases and the producer's stores. **Never imports `scene`** |
| `scene/` | `twiddle scene` client TUI (Textual): it only *reads* a dataset (the TUI, `scene list` and `scene venue`) and plays music; **imports no `scenedata` code** except `cli.py` starting a build lazily. `book.py` = profiles seeded from the dataset + the only background work (`PinEnricher` applies your pin, `TrackEnricher` fetches song lists for the band on screen: both need your own sign-in); who a band *is* comes from the dataset, so a band it lacks shows as unlooked until the next build. `cache.py` = pins + UI state, `instagram.py` = a venue's profile picture (no login), `pictures.py` = which band photo (Bandcamp, then Spotify for ✓/~, then Wikipedia, else a monogram; drawing via `dial/art.py`), `players.py` = playback incl. relay safety, `local.py` = this Mac's speakers via its own librespot, `app.py` = UI only. See README → Scene |
| `dial/` | `twiddle dial` radio TUI (Textual + `textual-image`). `feed.py` = polls the listed stations (a tag narrows it), `art.py` = covers/photos/colour, `enrich.py` = one `lookup` lane, `output.py` = Sonos room / This Mac (`ProcessOutput` for more), `bluetooth.py` = paired headphones via ffmpeg `audiotoolbox` incl. relay safety, and `Outputs` keeps one stream playing at a time, `app.py` = UI only. See README → Dial |
| `viz/` | the `v` visualizer in `dial`/`scene` and `twiddle viz`. `tap.py` = ffmpeg `-re` decode of a URL into a PCM ring (always drained, killed on close/exit), `source.py` = what to tap for what's playing (or why nothing: scene's local librespot, phones, native Sonos Spotify, an old relay), `analysis.py` = PCM → `Frame` (cava-style bands, gravity, beats), `canvas.py` = braille/blocks/shades + heat→colour palettes, `base.py` = the contract + registry + `~/.config/twiddle/viz` plugins, `modes/` = one visualizer per file, `screen.py` = the Textual screen. numpy lives only here. `tests/test_viz_contract.py` holds every visualizer to the contract. Skill `add-visualizer` |
| `dial/splash.py`, `viz/splash.py` | dial's opening card (screen / picture): Twiddle waving beside big "dial" letters while startup runs behind it; see `CHARACTERS.md` |
| `tone.py` | soak-test signal generator |
| `cli.py` | command line |
| `tools/observe.py` (repo root) | append-only observation log (**use this, not doc edits**) |

| `scripts/radio.zsh` (repo root) | one-word shell names over the CLI: one per catalog file, `kalx`/`kexp`/... (`tune`), `dial`, `np`, `discover`, `artist` (`info`), `stations`, `scene` (and `shows`, the older name) |

`uv sync` then `uv run pytest` (about 1000 tests). Tests encode the traps above —
several exist specifically to stop a fixed bug from returning, so if one fails,
read what it is asserting before changing it.
