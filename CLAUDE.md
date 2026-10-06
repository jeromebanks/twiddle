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
| Playing Spotify through a relay instead of Sonos's cloud | `docs/SPOTIFY.md` |
| Twiddle and any other characters | `CHARACTERS.md` |
| Adding a station / a venue / a visualizer | skills `add-radio-station`, `add-venue-source`, `add-visualizer` |
| Handling a GitHub issue (triage, PRD, planning, building slices, milestone demos) | `SDLC.md` (`docs/sdlc.html` for people), skills `triage-issue` / `plan-issue` / `work-slice` / `milestone-demo`, `tools/sdlc.py` (`state N` prints what to run next) |

`uv sync` then `uv run pytest` (about 1220 tests, a minute). Nothing in the
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
| `scene list`, `scene venue`, `scene login` | **read-only — The List (foopee.com) + 21 venues' own listings / venue details + Wikipedia / a Spotify sign-in for the Mac's own player; no speaker** |
| `scene` (TUI) | **writes only on `p`/`R`/`d`** — Spotify always; a speaker only when the device is the relay. A **Bandcamp** track needs no Spotify: on the relay device it re-points the Roam itself (journalled; asks twice and pauses Spotify if music is playing through the relay), on This Mac it's `ffmpeg` (`ffplay` on Linux). The genre scan is read-only: throttled Bandcamp searches, cached 14 days. Interrupting anything playing elsewhere asks twice; taking Spotify *off* a live relay also journals `spotify_transfer_from_relay` and saves what was playing for `R`. A Bandcamp track follows `d` to the new output and stops on the old; any Spotify play stops one left elsewhere |
| `stations [--tag]`, `stations tags/search/probe` | **read-only — the catalog files / radio-browser.info, SomaFM, TuneIn / one stream + the station's site; no speaker, and nothing written (adding a station is writing a catalog file: skill `add-radio-station`)** |
| `dial list [--tag]` | **read-only — stations' sites only, no speaker** |
| `v` in `dial` / `scene`, `viz list/snapshot/demo` | **read-only — no speaker.** Its own ffmpeg decode of the playing stream (a second listener); on the relay only as an *observer* (`?observer`), never counted in `listeners`/`clients_total`/`dropped_chunks`; an older relay without `X-Twiddle-Observer` is not tapped at all. Shows the stream, **not** the speaker: never evidence that sound came out |
| `dial` (TUI) | **writes only on `enter`/`s`/`D`/`R`/`d`/volume/mute** (`t` filters by tag: display only) — to the chosen Sonos room (journalled) or this Mac / a Bluetooth device (`ffmpeg` with dial's own live volume and mute, not the system's; Linux the same through `ffmpeg -f pulse`; only an ffmpeg without PulseAudio falls back to `ffplay` + the sink volume. The volume keys also work inside `v`). Spotify is consulted only when the room is on the relay; if music is playing through it, tuning asks twice, pauses Spotify, journals `spotify_paused_for_radio`; `R` puts it back. `d` moves a playing station to the new output and stops it on the old (only if that still plays what dial started; never the relay) |
| `diag scan/ping/watch/analyse/baseline/baseline-diff`, `daemon status` | **read-only — safe any time** |
| `alarm list`, `alarm snapshot`, `alarm status` | **read-only — `ListAlarms` + the household's time and format from any speaker; no `AlarmClock`/`AVTransport` write, no journal entry** (`list` marks ⌁ an alarm whose source needs this Mac; `snapshot` saves the list to `logs/snapshots/alarms.json`; `status` asks each group coordinator `GetRunningAlarmProperties` and takes one GENA event for `AlarmRunning`/`SnoozeRunning`) |
| `alarm sources [<name> [<query>]]` | **read-only — the source registry (`alarms/sources/`) and the station catalog; no speaker, no network** (`alarm sources spotify <words>` searches Spotify with your sign-in) |
| `play`, `pause`, `stop`, `next`, `prev`, `volume`/`vol`, `mute`, `bass`, `treble`, `balance`, `loudness`, `shuffle`, `repeat`, `stream`, `restore`, `group`, `ungroup`, `sleep <duration|off>` | **writes to a speaker** (bare `sleep` only reads the timer; setting one journals a span around when it will stop the room) |
| `diag serve`, `diag radio`, `diag soak`, `relay start/up/down`, `tune`, `spotify play`, `spotify discover` | **writes transport + volume to a speaker** |
| `alarm restore` | **writes alarms — a deferred transport + volume write**: `CreateAlarm`/`UpdateAlarm`/`DestroyAlarm` until the household's alarms match `alarm snapshot`. Each write is refused if the alarm list's version moved since it was read, and journalled with the alarm before and after; the old-to-new ID map is journalled too |
| `alarm enable <id>`, `alarm disable <id>`, `alarm rm <id>` | **writes alarms — a deferred transport + volume write**: one `UpdateAlarm` changing only `Enabled` (every other field, the source included, sent back byte-for-byte), or one `DestroyAlarm` after asking twice (a `y`, then the ID typed out; no terminal means no). Refused if the list moved since it was read; journalled with the whole alarm, so `clock.recreate` can make a deleted one again from `logs/interventions.jsonl` (every `CreateAlarm` field; not the child elements or unknown attributes it has no argument for) |
| `alarm add`, `alarm edit <id>` | **writes alarms — a deferred transport + volume write**: one `CreateAlarm`, or one `UpdateAlarm` changing only the fields named (time, days, duration, volume, play mode, include-grouped-rooms, room, on/off, source); `--source chime`, `station:<key>` or any registered source (a station is its own stream, played by the speaker with no Mac involved); on edit the source stays byte-for-byte unless `--source` names one. The room is named: a bonded follower's alarm goes on its room's primary (never the group coordinator), and the output says so. Refused if the list moved since it was read; journalled before and after; `--dry-run` resolves the room and prints the alarm |
| `alarm try <id>` | **writes transport + volume now** — the speaker's own `RunAlarm` with the alarm's fields, on the coordinator of the alarm's room; journalled `alarm_run` with the whole alarm, plus a span around when its duration will stop it |
| `alarm stop --room`, `alarm snooze --room [--minutes 5/10/15/30]` | **writes transport** — the group's own `Stop` / `SnoozeAlarm` (default 10 minutes), **refused unless an alarm is going off (or snoozed) there**, so neither silences ordinary playback; journalled `alarm_stop`/`alarm_snooze`, a snooze with a span around when it rings again |
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

`snapshot` covers one room's transport and volume, not alarms, which belong to
the whole household. Before touching alarms, `uv run twiddle alarm snapshot`;
after, `uv run twiddle alarm restore` (`--dry-run` first shows every change).

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
| `alarms/` | Sonos alarms, no UI. `model.py` = an `Alarm` that round-trips every ListAlarms field (program URI/metadata opaque, unknown attributes and children kept) + `Recurrence` (ONCE/DAILY/WEEKDAYS/WEEKENDS/ON_<days>); `clock.py` = the `AlarmClock` reads (`ListAlarms`, `GetTimeNow`, `GetFormat`) with pure parsers beside them, and the writes (`create_alarm`/`update_alarm`/`destroy_alarm`: refused with `VersionChanged` if the list moved since it was read, journalled before and after, **write**); `deleted`/`recreate` = a deleted alarm back from its `alarm_destroy` journal entry (**recreate writes**); and the `AVTransport` side: `running_alarm` (`GetRunningAlarmProperties`; UPnP 800 = none), `last_change` (one GENA event: `AlarmRunning`/`SnoozeRunning`), `alarm_now`, and `run_alarm`/`snooze_alarm`/`stop_alarm` (journalled, **write**); `baseline.py` = `AlarmSnapshot` (the raw `CurrentAlarmList`), `plan` (alarms compared by `key`, not ID) and `restore` (**writes**); `sources/` = what an alarm plays: a `Source` (choices, build URI + DIDL, `needs_mac`, `fallback`, `owns`, `describe`, `sound_source`) per module and a registry the CLI reads at run time (`chime.py`, `station.py` = dial's catalog via `play.radio_uri`); a new source is one module + `register()`, no CLI edit; `soundsource.py` = where an alarm's sound comes from, from its `ProgramURI` alone (a registered source first, then the observed Sonos shapes: scheme + `sid` + item prefix, exactly; else `unknown`) |
| `alarm_cli.py` | `alarm list`, `alarm snapshot` (read-only): every alarm under its room's name, bonded-follower / vanished / unknown rooms labelled, next fire in the household's time; `alarm restore [--dry-run]`, `alarm enable/disable/rm <id> [--dry-run]`, `alarm add`, `alarm edit <id>`, `alarm try <id>`, `alarm stop/snooze --room` (**write**; `alarm status`, `alarm sources` read-only; `rm` asks twice; stop/snooze refuse when nothing is ringing; `add`/`edit` parse every field, and `room_target` = a room name to its primary unit's RoomUUID) |
| `play.py` | HTTP file server + transport control (**writes**) |
| `relay.py` | live audio -> paced PCM -> MP3 -> HTTP fan-out (no speaker writes) |
| `relay_cli.py` | `relay doctor/login/measure/start` (**start writes**) |
| `supervisor.py` | keeps a detached relay alive; PID file, SIGTERM, restore |
| `spotify.py` | Spotify Web API: PKCE auth, search, devices, transport |
| `spotify_cli.py` | `spotify auth/devices/search/now/play/...` (**play writes**) |
| `discover_cli.py` | `spotify discover` — interactive: search an artist, sample tracks, play one (**writes**) |
| `stations/` | the station catalog and now-playing. `catalog/<key>.toml` = one file per station (the only place stream URLs live; format in `model.py`), `tags.py` = the tag vocabulary, `fetchers/` = one module per now-playing mechanism (ICY, Spinitron, SomaFM, Radio France, NTS, KEXP, KQED, WMBR, WFMU, Rainwave, Airtime Pro, streamabc; table in its `__init__`), `icy.py` = ICY parsing incl. a per-station `encoding` (Shift-JIS), `titles.py` = the named shapes an ICY title is parsed by (`fetch_args = { titles = ... }`; default iHeart attributes, else `Artist - Song`), `directory.py` = `stations search`, `probe.py` = `stations probe`. A bad catalog file is skipped with a warning, never raised (the daemon imports this); `test_catalog.py` is strict. Skill `add-radio-station` |
| `stations_cli.py` | `stations [--tag]`, `stations tags`, `stations search`, `stations probe` (all read-only) |
| `streaminfo.py` | what a stream really is (codec/bitrate/rate/channels) via `ffprobe`, cached a week in `~/.cache/twiddle/streams.json`; the dial shows it |
| `lookup.py` | who is this artist / album — MusicBrainz identity, Wikipedia via Wikidata, Discogs members/profile, Discogs → Bandcamp fallbacks; throttled, cached in `~/.cache/twiddle/lookup.json` |
| `radio_cli.py` | `tune` (**writes**), `np [-i] [-d]`, `info` |
| `spotify_ops.py` | Spotify playback logic with no CLI attached: raises `PlaybackError`, reports via `notify`. `spotify_cli`'s helpers are thin wrappers over it |
| `comedy.py` | pick and play something to fall asleep to: classify comedians out of `/me/top/artists` via `lookup.py`, find new releases, build a multi-album queue, arm the native Sonos sleep timer (**`start_queue` writes**) |
| `comedy_cli.py` | `comedy artists/refresh/new/sleep/rate` — `sleep` **writes**, the rest read-only |
| `scene/` | `twiddle scene` TUI (Textual). `sources/` = where shows come from (The List; Yoshi's, Gilman (ShowSlinger), Gray Area, Make-Out Room (CalendarWiz), and per-platform tables: See Tickets ×5, TicketWeb ×7, VenuePilot ×2, Squarespace, Simple Calendar, which add flyers + ticket links; `model.dedupe` merges them; skill `add-venue-source`), `instagram.py` = a venue's profile picture (no login), `venues.py` + `venue_info.py` = watched venues, their addresses/sites/descriptions, `bands.py` = identity + enrichment (one serialized lane for `lookup`, which isn't thread-safe), `bandcamp.py` = throttled/cached Bandcamp search, band choice, tracks + fresh stream URLs, `genre.py` = tags → genre families (no AI), `pictures.py` = which band photo (Bandcamp, then Spotify for ✓/~, then Wikipedia, else a monogram; drawing via `dial/art.py`), `players.py` = playback incl. relay safety, `local.py` = this Mac's speakers via its own librespot, `app.py` = UI only. See README → Scene |
| `dial/` | `twiddle dial` radio TUI (Textual + `textual-image`). `feed.py` = polls the listed stations (a tag narrows it), `art.py` = covers/photos/colour, `enrich.py` = one `lookup` lane, `output.py` = Sonos room / This Mac (`ProcessOutput` for more), `bluetooth.py` = paired headphones via ffmpeg `audiotoolbox` incl. relay safety, and `Outputs` keeps one stream playing at a time, `app.py` = UI only. See README → Dial |
| `viz/` | the `v` visualizer in `dial`/`scene` and `twiddle viz`. `tap.py` = ffmpeg `-re` decode of a URL into a PCM ring (always drained, killed on close/exit), `source.py` = what to tap for what's playing (or why nothing: scene's local librespot, phones, native Sonos Spotify, an old relay), `analysis.py` = PCM → `Frame` (cava-style bands, gravity, beats), `canvas.py` = braille/blocks/shades + heat→colour palettes, `base.py` = the contract + registry + `~/.config/twiddle/viz` plugins, `modes/` = one visualizer per file, `screen.py` = the Textual screen. numpy lives only here. `tests/test_viz_contract.py` holds every visualizer to the contract. Skill `add-visualizer` |
| `dial/splash.py`, `viz/splash.py` | dial's opening card (screen / picture): Twiddle waving beside big "dial" letters while startup runs behind it; see `CHARACTERS.md` |
| `gain.py` | twiddle's own live volume/mute for anything that plays by running ffmpeg (`LiveGain`); `dial/output.py`'s `ProcessOutput` uses it, and a future podcast/Bandcamp player can too |
| `tone.py` | soak-test signal generator |
| `cli.py` | command line |
| `tools/sdlc.py` (repo root) | the issue SDLC's deterministic half, stdlib only: derives an issue's `sdlc:*` state from labels + marked comments, decides whose move it is, validates sign-offs and Codex plan verdicts, checks a plan's slices/dependencies/PRD coverage, posts agent comments, moves labels, creates an epic's sub-issues + `blocked_by` links, claims slices into git worktrees (`.worktrees/`), records test runs and Codex reviews bound to a PR's head SHA, gates the merge into the epic's branch `epic/N`, pauses new slices while a finished milestone awaits its demo or its release, posts/records milestone demos, and ships accepted milestones from `epic/N` to `main` (**`transition`/`reconcile`/`bootstrap-labels`/`plan-post`/`plan-review`/`plan-create`/`claim`/`release`/`test-record`/`pr-review`/`merge`/`escalate-slice`/`demo-post`/`demo-accept`/`demo-changes`/`epic-branch`/`sync`/`ship-review`/`ship`/`verify-main`/`revert-slice`/`plan-annotate`/`demo-request`/`file-issue` write to GitHub, never to a speaker; `claim`/`cleanup` also make/remove worktrees; `epic-branch`/`sync`/`revert-slice` push to `epic/N`, `ship` merges it into `main` through a PR; `demo-post` pushes the pictures to the public orphan branch `sdlc-demos` and refuses anything that looks like a device ID, an address or a secret**; `state`/`next`/`plan-validate`/`ready`/`slice-status`/`slice-check`/`demo-status` only read). Config `.sdlc/config.json`; skills `triage-issue`, `plan-issue`, `work-slice`, `milestone-demo`; see `SDLC.md` |
| `tools/demo_shot.py` (repo root) | a demo's pictures: a command's output or a twiddle TUI's screens (driven headless by keys) as SVG, identifiers masked. **Runs whatever command it's given**: only read-only ones and keys belong in a picture |
| `tools/observe.py` (repo root) | append-only observation log (**use this, not doc edits**) |

| `scripts/radio.zsh` (repo root) | one-word shell names over the CLI: one per catalog file, `kalx`/`kexp`/... (`tune`), `dial`, `np`, `discover`, `artist` (`info`), `stations`, `shows` (`scene`) |

`uv sync` then `uv run pytest` (about 1220 tests). Tests encode the traps above —
several exist specifically to stop a fixed bug from returning, so if one fails,
read what it is asserting before changing it.
