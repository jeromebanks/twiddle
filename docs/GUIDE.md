# The twiddle guide

Every command, and how the Sonos side measures things. New here? Start with
the [README](../README.md) and [QUICKSTART.md](QUICKSTART.md).

The Sonos tools were built for a specific question: **when a speaker
disconnects, is that a broken speaker or a broken setup?** Everything in them
is aimed at answering that with measurements rather than guesses. The
measurement traps below each produced a wrong answer first.

## Install

```bash
uv sync
```

## Just here for the local-shows app? (no Sonos needed)

`twiddle scene` lists upcoming Bay Area shows, tells you who each band is,
and plays them through your Mac's speakers; `twiddle dial` does the same
for radio. **Setting up on your own Mac: [QUICKSTART.md](QUICKSTART.md).**
The guide to `scene`, with every key: [SCENE.md](SCENE.md).

The rest of this guide is mostly the Sonos tools; skip it if you have no
Sonos.

## The idea

Two observers watch the same speakers at the same time:

| Observer | Question it answers |
|---|---|
| Household view (UPnP topology + GENA events from a mains-powered speaker) | Does Sonos still think this speaker is present? |
| Direct view (ICMP + HTTP from this Mac) | Is the speaker actually powered up and reachable? |

The diagnosis lives in the disagreement:

* **absent from household, still reachable** — it did not crash or lose power.
  It lost the Sonos sync heartbeat. That is a radio/congestion problem.
* **absent and unreachable** — it fell off the network entirely.
* **`BootSeq` changed, or uptime went backwards** — the speaker *restarted*.
  Spontaneous restarts on mains power point at power or hardware.

`BootSeq` is the reboot counter Sonos publishes per device; watching it is what
separates "rebooted" from "still running but disowned".

## Commands

The command line has two halves. **Control** verbs name a room and do one
thing; **`diag`** holds everything that measures the network and the speakers.

```bash
uv run twiddle rooms                # every name a command can be given
uv run twiddle status               # what each group is playing, and at what volume
uv run twiddle diag scan            # inventory + findings + measured RF error rates
uv run twiddle diag watch --duration 40
```

The old top-level spellings (`twiddle scan`, `twiddle watch`, ...) still
work and are not going away: the installed launchd agent invokes them by
absolute argv from a plist on disk, and `tests/test_cli_contract.py` builds
that plist and feeds its own arguments back through the parser to prove it.

### Control: name a room, not an IP

```bash
uv run twiddle play    --room "Living Room"
uv run twiddle volume 40 --room roam          # absolute; `vol` also works
uv run twiddle vol +4 --room roam             # or ++4: relative step up
uv run twiddle vol --8 --room roam            # or -4, -, --: relative step down
uv run twiddle vol mute --room roam           # or `mute on --room roam`
uv run twiddle mute on --room roam
uv run twiddle sleep --room roam              # read the sleep timer (read-only)
uv run twiddle sleep 45 --room roam           # stop in 45 min; also 90m, 1h, 1h30, 1:30
uv run twiddle sleep off --room roam          # cancel it
uv run twiddle bass -4 --room roam            # -10..10; ++2/--2 steps, bare ++/-- steps by 2
uv run twiddle treble ++2 --room roam         # same grammar as bass
uv run twiddle balance +3 --room roam         # -10 full left .. +10 full right
                                                #   (Sonos stores this as an LF/RF volume pair,
                                                #    not a signed slider; see play.set_balance)
uv run twiddle loudness on --room roam        # loudness compensation; omit state to read
uv run twiddle shuffle on --room roam         # omit state to read
uv run twiddle repeat all --room roam         # off/all/one; omit state to read
uv run twiddle stream http://stream.kalx.berkeley.edu:8000/kalx-128.mp3 --room roam
uv run twiddle group --room roam --to "Living Room"
```

Names are matched loosely — case-insensitive, partial, by room, by zone name
or by model — so `roam`, `Sonos Roam` and `Sonos Roam (R)` all land on the
same group. IPs work too, but they are DHCP here and will move.

Four things make this usable by an agent rather than only by a person:

| | |
|---|---|
| `--json` | every command answers in one envelope: `ok`, plus the payload or `error` **and** a `hint` naming valid alternatives |
| `--dry-run` | resolves the target and prints the plan without touching a speaker |
| redirect reporting | see below |
| `snapshot` / `restore` | put a room back exactly as it was found |

### Alarms: every alarm in the household

```bash
uv run twiddle alarm list           # every alarm, grouped by room (read-only)
uv run twiddle alarm list --json    # the same, in the ok/error envelope
```

Alarms are household-wide, so any speaker answers for all of them: one
`ListAlarms`, plus `GetTimeNow` and `GetFormat` for the household's own time
and how it writes it (a format of `INV`, i.e. unset, prints 24-hour). Each
alarm shows its time, days, on/off, volume, duration (the auto-stop), play
mode, the source's title from the alarm's own metadata, and when it next goes
off in the household's local time, and its ID (`#34`), which the writing
verbs below take.

An alarm aimed at a bonded follower (the right Roam, a surround), at a speaker
that has vanished from the household, or at a UUID no speaker owns is listed
with a `!` line saying so, never hidden. `alarm list` writes nothing: no
`AlarmClock` or `AVTransport` write, no journal entry.

An alarm twiddle can't read (a recurrence it doesn't know, a volume over 100,
a flag that isn't 0/1, a missing attribute) doesn't hide the others: `alarm
list` shows every alarm it can read, then a `can't read` section naming each
one it couldn't, its room and why (`--json`: an `unreadable` list beside
`alarms`, each `{id, room, status, room_uuid, reason}`; `id` is null for an
alarm with no ID). `alarm status` reads the same way. Every other alarm verb
(`add`, `edit`, `rm`, `enable`, `disable`, `try`, `snapshot`, `restore`)
**refuses while one is unreadable**, even for a readable alarm, and writes
nothing: its checks compare whole lists, and an alarm missing from both sides
could change unseen. Fix or delete it in the Sonos app. `alarm stop` and
`snooze` never read the list, so they still silence a ringing alarm.

```bash
uv run twiddle alarm snapshot              # -> logs/snapshots/alarms.json (read-only)
uv run twiddle alarm restore --dry-run     # every change it would make; writes nothing
uv run twiddle alarm restore               # WRITES: make the alarms match the snapshot
```

`alarm snapshot` saves the `CurrentAlarmList` exactly as `ListAlarms` sent it,
with its version; like `alarm list` it writes nothing to a speaker or the
journal. `alarm restore` makes the household match it again: alarms the
snapshot lacks are destroyed, deleted ones recreated, changed ones updated.
A recreated alarm gets a new ID from the speaker, so alarms are compared by
everything but their ID: time, days, duration, enabled, room, volume, play
mode, include-grouped-rooms, and `ProgramURI`/`ProgramMetaData` as exact
strings. An equal alarm under another ID is left alone, so restoring twice
does nothing the second time. The old-to-new ID mapping is printed and
journalled (`alarm_restore`).

Every alarm write (`CreateAlarm`, `UpdateAlarm`, `DestroyAlarm`) first re-reads
the list and is **refused if its version moved** since twiddle read it: someone
changed an alarm in the Sonos app meanwhile, and that edit is not overwritten.
AlarmClock has no compare-and-swap, so the list read back after each write must
differ only in the alarm written; if anything else moved in that window, the
write is reported done and everything after it stops. Each write is journalled
as `alarm_create`/`alarm_update`/`alarm_destroy` with the whole alarm before
and after (or the error, if it failed), so a deleted or clobbered alarm can be
recreated from `logs/interventions.jsonl`. A write whose answer is lost (a
timeout after the speaker acted) is judged from the list read back: `written`
in the journal is true, false, or null when that can't be told. A restore
stops at the first write that fails or is refused, reads the list again and
reports everything still different, exiting 1. Some Spotify alarms carry a
`<Content>` child that `CreateAlarm` can't set: a recreated one comes back
without it, and restore says so in a `note:` rather than failing.

```bash
uv run twiddle alarm disable 34 --dry-run   # the room and alarm it resolves to; writes nothing
uv run twiddle alarm disable 34             # WRITES: switch alarm 34 off
uv run twiddle alarm enable 34              # WRITES: and on again
uv run twiddle alarm rm 34                  # WRITES: delete it, after asking twice
```

`enable` and `disable` send one `UpdateAlarm` that changes `Enabled` and
nothing else: every other field goes back exactly as `ListAlarms` gave it, so
an alarm whose source twiddle doesn't recognise (an iHeart station, say) keeps
its `ProgramURI` and `ProgramMetaData` byte-for-byte. An alarm already in the
state asked for is left alone, with nothing written or journalled. That is
what is *sent*: whether a real speaker keeps a Spotify alarm's `<Content>`
child through an `UpdateAlarm` is unverified, and the journal entry's
`before`/`after` children show it.

`rm` asks twice, on the terminal: `y`, then the alarm's ID typed out. Anything
else, or no terminal to ask (answers piped in, a script), deletes nothing. The prompts go
to stderr, so `--json` still prints one envelope. If someone changed an alarm
in the Sonos app while it was asking, the delete is refused. The
`alarm_destroy` journal entry keeps the whole alarm (every attribute and child
element), so a deleted alarm can be made again from it, with a new ID
(refused if an equal alarm is already there):

```bash
uv run python -c "from twiddle.alarms import clock; ip = '<any speaker>'; \
  print(clock.recreate(ip, '34', clock.list_alarms(ip).version))"
```

That is a write too (journalled `alarm_create`); `alarm restore` from a
snapshot taken before the delete brings it back as well. Every field
`CreateAlarm` takes comes back exactly; what it has no argument for can't: a
Spotify alarm's `<Content>` child (see above) and any attribute twiddle's
model doesn't know. `recreate` returns a list of what it couldn't bring back.

```bash
uv run twiddle alarm add --room roam --time 07:15 --days weekdays --dry-run   # the alarm it would create
uv run twiddle alarm add --room roam --time 07:15 --days weekdays --volume 20 --duration 1h
uv run twiddle alarm edit 34 --volume 15 --mode shuffle --dry-run               # the change; writes nothing
uv run twiddle alarm edit 34 --days sat,sun --off                               # WRITES
```

`add` sends one `CreateAlarm` and prints the ID the speaker gave it; `edit`
sends one `UpdateAlarm` that changes only the fields named on the command line
and sends every other one back exactly as `ListAlarms` gave it. Between them
every field `AlarmClock` takes is settable:

| Flag | Field | Values (`add`'s default) |
|---|---|---|
| `--time` | `StartLocalTime` | 24-hour `HH:MM` or `HH:MM:SS`, the household's time (required on `add`) |
| `--days` | `Recurrence` | `once`, `daily`, `weekdays`, `weekends`, days like `mon,wed,fri` or `mon-fri`, or the speaker's own `ON_<days>` (Sunday 0); written the speaker's way, so `sat,sun` is `WEEKENDS` (`daily`) |
| `--duration` | `Duration` | the auto-stop: `1h`, `30m`, `1h30`, `HH:MM:SS`; `none` sends an empty `Duration`, as soco does for no auto-stop (unverified on a real speaker) (`2h`) |
| `--volume` | `Volume` | 0-100 (25) |
| `--mode` | `PlayMode` | `normal`, `repeat`, `repeat-one`, `shuffle`, `shuffle-repeat`, `shuffle-repeat-one`, or the speaker's own value. The speaker's `SHUFFLE` is shuffle *and* repeat (`shuffle-repeat`); plain `shuffle` is `SHUFFLE_NOREPEAT` (`normal`) |
| `--include-grouped-rooms` / `--no-...` | `IncludeLinkedZones` | also play in the rooms grouped with it when it fires (no) |
| `--room` | `RoomUUID` | a room by name (required on `add`) |
| `--on` / `--off` | `Enabled` | (on) |
| `--source` | `ProgramURI`, `ProgramMetaData` | `chime`; on `edit`, `keep` (the default) leaves the source byte-for-byte (`chime`) |

So editing anything about an alarm whose source twiddle doesn't recognise (an
iHeart or Spotify alarm) sends its `ProgramURI` and `ProgramMetaData` back
untouched; as with `enable`, whether a real speaker keeps a Spotify alarm's
`<Content>` child through that `UpdateAlarm` is unverified (see above). An edit
that would change nothing writes nothing. In `--json`, `room` is where the
alarm was and `to_room` where `--room` moves it.

The room is named, never addressed. An alarm belongs to a room, so it goes on
the room's primary unit: naming a bonded follower (`"Sonos Roam (R)"`, a
surround, or its IP) puts the alarm on the left Roam or the soundbar, and the
output says so in a `note:` (`redirected_from`/`reason` in `--json`). Naming
the room itself is never a redirect. It is never the group coordinator: a
room grouped with another today keeps its own alarm. For the same reason a
name that matches two rooms is refused as ambiguous even when they are
grouped (`--room Den` with Den North and Den South), where transport commands
would act on the group. `edit` without `--room`
leaves the alarm where it is, even on a follower (`alarm list` labels those).
`--dry-run` resolves the room and prints the whole alarm it would write;
like every alarm write, the real thing is refused if the list moved since it
was read, and journalled (`alarm_create`/`alarm_update`) before and after.

```bash
uv run twiddle alarm status                          # is one going off, and where (read-only)
uv run twiddle alarm try 34 --dry-run                # the room and alarm it would fire; writes nothing
uv run twiddle alarm try 34                          # WRITES: fire alarm 34 now, to hear it
uv run twiddle alarm snooze --room roam              # WRITES: the speaker's own snooze, 10 minutes
uv run twiddle alarm snooze --room roam --minutes 5  # 5, 10, 15 or 30
uv run twiddle alarm stop --room roam                # WRITES: stop it
```

Ringing belongs to a group's transport, so these go to `AVTransport` on a
group coordinator, not to `AlarmClock`. `status` asks every group's
coordinator `GetRunningAlarmProperties`, which names the alarm going off
(`AlarmID`, `GroupID`, `LoggedStartTime`) and answers UPnP error 800 when
none is. It also takes one GENA event from each (the subscription the daemon
keeps on its anchor) for `AlarmRunning` and `SnoozeRunning`, which no action
returns; without an event it still answers from `GetRunningAlarmProperties`.
An event that says plainly neither is running wins over an alarm ID
`GetRunningAlarmProperties` still names: `stop` acts on this answer, and
refusing wrongly costs less than stopping ordinary playback.
The alarm is named from `ListAlarms` by its ID, under its own room. `status`
writes nothing and journals nothing.

`try` is the speaker's `RunAlarm` with every field of the alarm (time aside),
on the coordinator of the alarm's room, so a bonded follower's alarm fires on
its pair's coordinator. It is refused for an alarm aimed at a speaker that
isn't here. `LoggedStartTime` is sent as the household's local time
`YYYY-MM-DD HH:MM:SS`, from `GetTimeNow`. Tried on the Roam (2026-10-04,
alarm 34): it rang, `GetRunningAlarmProperties` named alarm 34 with that
`LoggedStartTime` and the group's ID, LastChange had `AlarmRunning=1`, and
`alarm stop` ended it (`status` then read nothing ringing).

`stop` and `snooze` resolve the room the way transport commands do (a bonded
follower goes to its coordinator, and the output says so) and send the
group's own `Stop` or `SnoozeAlarm` (`Duration` `00:10:00`). Both are
**refused when no alarm is going off there**: a bare `Stop` would silence
whatever the room is playing, and `twiddle stop --room` is the command for
that. A snoozed alarm (`SnoozeRunning`) can still be stopped. Whether a snoozed
alarm also answers `GetRunningAlarmProperties`, and whether `Stop` ends a
snooze, are unverified.

A tried alarm stopped early leaves its duration-stop span in the journal: a
few minutes wrongly discounted two hours on, the cheaper mistake (as with a
cancelled sleep timer).

All three writes are journalled (`alarm_run` with the whole alarm,
`alarm_stop`, `alarm_snooze` with its minutes). The speaker acts again later
on its own when a tried alarm's duration runs out or a snooze ends, so a span
is journalled around each of those moments (`alarm_run_stop_*`,
`alarm_snooze_ring_*`, a minute before to three after) for `analyse` to
discount, as with the sleep timer. Each takes `--dry-run`, which reads
whether anything is ringing but writes and journals nothing.

### Relay: play anything on this Mac, including Spotify

`serve` plays files off disk. `relay` widens the same pipe to *live* audio and
pushes it down the identical `x-rincon-mp3radio://` route — so Spotify can play
on a Sonos without Sonos's Spotify integration and without Spotify Connect
handing off to the speaker.

```bash
uv run twiddle relay doctor                     # what is installed (read-only)
uv run twiddle relay login                      # once: Spotify OAuth, in a browser
uv run twiddle relay start --room roam --volume 30
```

Then cast to **Sonos Roam Relay** from the Spotify app. Ctrl-C puts the room
back to whatever it was playing.

| `--source` | What it is |
|---|---|
| `spotify` | librespot as a Connect target (default) |
| `tone` | a sine — proves the speaker path with no Spotify involved |
| `file` | an audio file, looped (`--source-arg PATH`) |
| `device` | an avfoundation input such as BlackHole (`--source-arg NAME`) |

The design rationale, what was verified on hardware and what was not, is in
[SPOTIFY.md](SPOTIFY.md). Two things there are
load-bearing rather than incidental:

* **The writer into the encoder is paced to the wall clock and fills with
  silence.** librespot stops writing when you pause; without this the stream
  stalls, Sonos hangs up, and pressing play in the app does not bring it back.
* **The HTTP server fans out to many listeners.** Sonos buffers ahead, closes
  the socket and reopens it, so a one-connection server yields one burst of
  audio and then silence.

`relay start` snapshots the room, restores it on exit (including on SIGTERM),
and brackets the session in `journal_span` so `analyse` discounts dropouts this
Mac caused. `relay measure` is the one command that validates a source's PCM
format, rate and channel count at once: real-time PCM must read 176400 B/s.

### Spotify, driven by a script or an agent

`relay` carries the audio; `spotify` chooses what plays. One command does both:

```bash
uv run twiddle spotify auth --client-id <YOUR_ID>    # once, in a browser
uv run twiddle spotify play kind of blue --room roam
```

`play` starts a relay if none is running, finds it among Spotify's devices and
plays to it. The rest are plain API calls, so a TUI or an MCP server can
compose them:

```bash
uv run twiddle spotify search "miles davis" --type album
uv run twiddle spotify now
uv run twiddle spotify pause | next | prev
uv run twiddle spotify queue "so what"
uv run twiddle relay up   --room roam     # detached; outlives your terminal
uv run twiddle relay down                 # stops it, restores the room
```

**`spotify discover` is the one interactive command** — search an artist,
see a track list, play one by number, sample another, all without leaving
the shell:

```bash
uv run twiddle spotify discover              # --room defaults to roam
uv run twiddle spotify discover Radiohead    # jump straight to an artist
```

`np -d` chains straight into it with whatever artist is currently playing,
so there's nothing to retype.

**Radio, and finding out who you're hearing:**

```bash
uv run twiddle stations                  # what `tune` knows
uv run twiddle tune kexp --volume 30     # play one on the Roam (WRITES)
uv run twiddle np                        # what's on: last station tuned, or Spotify
uv run twiddle np kalx -i                # ...plus who the artist is, what the album is
uv run twiddle np -d                     # ...then into `discover` with that artist
uv run twiddle info low --album "Things We Lost in the Fire"
```

`np -i` / `info` identify the artist on MusicBrainz from the strongest
evidence available — KEXP hands over MusicBrainz ids outright; otherwise
artist + album, then artist + song, and a bare name only when exactly one
artist has it (a shared name lists the candidates instead of guessing). The
Wikipedia paragraph is reached only through that entity's own Wikidata link,
never by searching Wikipedia for the name.

For obscure bands, which college radio plays a lot, it goes further. A
misspelled or abbreviated name ("Sabotage Q.C.Q.C.?" for MusicBrainz's
"Sabotage qu'est-ce que c'est?") is found by a loose search, but only
accepted if that artist actually recorded the song being played. If
MusicBrainz still has nothing, it tries **Discogs** (deepest catalogue for
small-press records, and the spelling DJs tend to log from), then
**Bandcamp** (through its site-search endpoint, which is unofficial and may
change). Discogs also supplies band members, and a profile when Wikipedia has
none. Discogs allows 25 requests/min without a key, 60 with a free personal token
(discogs.com/settings/developers). Save one once with
`twiddle info --save-discogs-token <TOKEN>`: like the Spotify tokens it goes
to `~/.cache/twiddle/` (mode 600), outside the repo. `DISCOGS_TOKEN` in the
environment overrides it. `scripts/radio.zsh` gives all of
this one-word names (`kexp`, `np`, `discover`, `artist`) — see its header. There is no ranked "most popular tracks" behind this:
Spotify's `/artists/{id}/top-tracks` returns a 403 for a personal
(Development-mode) app, so track lists come from search relevance instead,
filtered by artist id. See `SPOTIFY.md`, "Traps this
actually hit", for the measurements behind that.

**You need your own Spotify app.** The fallback client id is librespot's,
shared by every librespot user, and Spotify meters the Web API per client id —
so it returns `429` no matter how long you wait. Create one at
[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard),
tick **Web API**, add redirect URI `http://127.0.0.1:5588/login`, and pass
`--client-id` once. PKCE, so there is no client secret anywhere.

**Volume lives in exactly one place: the speaker.** librespot is pinned to
unity gain (`--volume-ctrl fixed`), so the Spotify app's slider does nothing
and `twiddle volume --room roam` is the control. Two attenuations in series
once made real music inaudible while every status field said `PLAYING` — see
SPOTIFY.md, "Traps this actually hit".

`relay up` runs the venv's `twiddle` directly rather than `uv run`, because
`uv run` does not forward SIGTERM — measured, after it left the Roams pointed
at a dead URL. See [SPOTIFY.md](SPOTIFY.md).

### Scene: who is playing locally, and what they sound like

**Full guide and quick start: [SCENE.md](SCENE.md).**

```bash
uv run twiddle scene                       # the TUI (`shows` via scripts/radio.zsh)
uv run twiddle scene list --venue stork    # plain text, read-only
```

A terminal app that lists upcoming shows at your venues (from The List, plus
21 venues' own pages, most with flyers),
says who each band is (MusicBrainz, Wikipedia, Discogs, Bandcamp, Spotify),
and plays them on any Spotify device: this Mac's own speakers (no Spotify
app needed), the Roams' relay, or a phone. Every
band's Spotify match is graded ✓/~/?/✗, because playing the wrong
same-named band is the failure that matters. Before `p` interrupts anything
playing elsewhere, it asks. A handoff away from the relay is journalled, and
`R` puts the Roams back where they were.

### Dial: every station at once, and one key to tune

```bash
uv run twiddle dial                 # the TUI (`dial` via scripts/radio.zsh)
uv run twiddle dial --output mac    # play here with ffplay instead of a Sonos room
uv run twiddle dial --dry-run       # everything works, nothing is tuned or changed
uv run twiddle dial list            # every station's now-playing as text, read-only
uv run twiddle dial --tag electronic # only one tag's stations (also `dial list --tag news`)
uv run twiddle dial --no-splash     # skip the opening card
```

- **The splash.** It opens on "dial" in big letters with Twiddle, the
  project's mascot, waving next to them (who he is: `CHARACTERS.md`). The
  station feed, the output's state and the logos are already loading
  behind it; it counts the stations as they answer. Any key or a click
  goes to the app, and that key does nothing else (`enter` won't tune,
  `q` won't quit). After 20 s it goes on its own. `ctrl+w` (for
  "wave") brings it back any time, without restarting.
- **What it shows.** The left pane is the stations in `stations/catalog/`, with
  what each one is playing now, refreshed in the background. The right
  shows the highlighted station in full:
  - the album cover, as Spinitron and KEXP publish it, else the Cover Art
    Archive, else the station logo;
  - the song, the artist, and the album and year;
  - the show and its DJ;
  - the artist's photo and bio (the same lookup as `np -i`). The photo is
    Wikipedia's, else the artist's Bandcamp band photo (used only when their
    linked Bandcamp page, or a name exactly one Bandcamp band has, says it
    is them), else their initials;
  - what the station just played -- or, for a station on a fixed program
    schedule (KQED), today's lineup with the current show marked;
  - the stream's real format under the panel, e.g. `MP3 128k · 44.1kHz
    stereo`. It is measured with `ffprobe`, because `icy-br` headers lie
    (KEXP's `kexp128.mp3` is 96k), and cached for a week in
    `~/.cache/twiddle/streams.json`. The tuned station's format also sits
    by the volume, in yellow when it's under 96k or mono.
- **Moving and tuning.** `j`/`k` preview a station without tuning it, and
  `enter` (or a double-click) tunes it.
- **Tags.** `t` picks a tag (`electronic (6)`, `news (3)`, `sleep (6)` …, or
  `all`) and the list shows only those stations, numbered 1, 2, 3 … for
  the number keys. The choice is remembered. Only listed stations are
  polled, so a long catalog behind a tag costs nothing; the tuned station
  keeps updating even when the filter hides it. `twiddle stations tags`
  lists the vocabulary (`stations/tags.py`).
- **Sound.** `+`/`-` change the volume, or click a point on the volume bar.
  A held key or a run of clicks becomes one journalled write, not twenty.
  `m`, or clicking the `♪`, mutes. `z` sets a sleep timer, stepping
  15 → 30 → 45 → 60 → 90 → 120 min → off (`Z` turns it off); the countdown
  sits in the header. On a Sonos room it is the speaker's own timer, so it
  fires with dial closed; on This Mac, dial holds it.
- **Output.** `d` picks the output: any Sonos room, or **This Mac**. On the
  Mac the stream plays through `ffmpeg` (`audiotoolbox`), and so does a
  Bluetooth device. Volume and mute there are dial's own, applied live to the
  stream: the Mac's system volume and its other apps are untouched. On Linux (a Chromebook's
  container) it is `ffmpeg -f pulse` the same way; only an ffmpeg built without
  PulseAudio falls back to `ffplay` and the system sink's volume. Inside the
  visualizer (`v`), `+`/`-`/`[`/`]` and `m` still work in dial, and the overlay
  shows the level.
  `s` stops the output and leaves the speaker's last station as its resume
  point. **`D` disconnects**: it stops, then clears a Sonos room's transport
  URI so the Sonos app shows nothing, not a paused station (journalled; never
  the relay; if the speaker refuses the clear it says so and stays stopped).
  On This Mac and Bluetooth it is the same as `s`. A handoff only stops, never
  clears. Quitting dial leaves a Sonos room playing; the Mac's own stream stops.
  **The station moves with you**: it starts on the new output, then stops on
  the old one -- so `d` writes. Only a station dial can see is moved, and an
  output is stopped only while it still plays what dial put there: a room on
  the relay, or on something you chose elsewhere, is left alone. If the new
  output is the relay room with Spotify playing, it asks first and the old
  output plays on until you press enter. **Bluetooth**: every paired
  headphone/speaker is listed too (`dial --bluetooth "Bose QC45"` picks one
  directly). It plays from this Mac via `ffmpeg -f audiotoolbox` to that
  device only, leaving the Mac's own output alone; no volume keys -- use the
  headphones'. If they're off, it connects them with `blueutil` when that's
  installed. See `dial/bluetooth.py`. Other kinds of output (a socket) go in
  `dial/output.py`: an `Output`, or a `ProcessOutput` with its own `argv`,
  offered via `Outputs.register`.
- **Pictures.** Images use the kitty graphics protocol in Ghostty, as `np`
  does. Set `TWIDDLE_DIAL_IMAGES=halfcell` to force coloured half-blocks.

Spotify plays no part in the radio: the room fetches the station's stream
itself. **Only if Spotify is actually playing through the relay does
tuning ask twice**; going ahead pauses Spotify there and journals
`spotify_paused_for_radio`. A relay with nothing playing is tuned over at once. `R` re-points the
room at the relay and resumes Spotify. This is deliberately *not* a journal
span: radio on the Roam leaves this Mac out of the audio path, so a
dropout during it is organic evidence, and must not be discounted.


### Visualizer: `v` in dial and scene

`v` turns the whole window into a picture of the music, drawn in characters:
spectrum bars, a braille oscilloscope, a stereo goniometer, a scrolling
spectrogram, the Doom fire, a starfield, a troupe of cartoon pets
dancing together on the beat, Twiddle (the mascot, `CHARACTERS.md`) dancing
and singing along, colour-cycling Julia-set fractals and mandalas,
and three for techno: a club laser show, an outrun synthwave horizon and a
warp tunnel. `←`/`→` changes the picture, `c`
the colours, `,` and `.` slide it 0.25 s later or earlier, `i` pins the
overlay, and `v` or `esc` goes back. Mode, colours and each output's delay
are remembered.

**What it shows is the stream, not the speaker.** Neither TUI holds any
decoded audio. A Sonos fetches its own stream, and ffplay, ffmpeg and
librespot write straight to CoreAudio. So the visualizer makes its own
read-only connection to the same stream (`viz/tap.py`:
`ffmpeg -re -i URL -f f32le`) and draws that. A Roam that drops out keeps
dancing here, so **it is never evidence that sound came out**. A Sonos
also buffers seconds ahead of what it plays: slide the picture with `.`
until the kicks line up.

| Playing | Tapped |
|---|---|
| a station, on a room, this Mac or Bluetooth | the station's URL, a second connection |
| a Bandcamp track (scene) | the URL we started |
| Spotify through the relay | the relay's `/stream.mp3?observer` |
| Spotify on scene's own librespot, a phone, a Sonos's own Spotify | nothing: it says so, and doesn't animate |

**The relay keeps observers apart.** A visualizer reading the relay is
counted in `observers` / `observer_dropped_chunks` in `relay.jsonl`, never in
`listeners`, `clients_total` or `dropped_chunks`, which are the
evidence about the speakers. A relay started before this existed would count
it as a speaker. The tap checks for the relay's `X-Twiddle-Observer` header
first (a HEAD, which subscribes no one) and stays away if it's missing:
"restart the relay when it suits you". It never restarts it.

```bash
uv run twiddle viz list                          # the visualizers, built-in and plugin
uv run twiddle viz demo [NAME]                   # full screen on made-up music
uv run twiddle viz demo --station kexp          # a real station, tapped here only: plays nothing
uv run twiddle viz snapshot NAME --size 80x24    # one frame as text, and ms/frame
```

Writing one is a file in `viz/modes/` (or a personal one in
`~/.config/twiddle/viz/`). It is a function or class from a `Frame` (bands,
waveform, stereo, beat, energy) to two arrays, characters and heat. It never
returns a colour; palettes do that. `tests/test_viz_contract.py` checks every
one automatically. Skill: `add-visualizer`.

We looked for something to reuse first. **spektr** (MIT, Textual) is the
closest, but it is an app that captures system audio through a loopback
device, and its analysis is internal. We borrowed its plugin shape
(codes + heat) so its modes port across. **cava**'s band maths (log bands,
gravity, automatic sensitivity) is reimplemented in numpy, because cava is a
C binary with no Python binding. Chart widgets (textual-plotext, plotille)
are too slow and too axis-shaped for 30 fps. drawille's braille is ten lines
of numpy. So there's one new dependency: numpy, imported only inside `viz/`.


### Adding a station

A station is one file, `src/twiddle/stations/catalog/<key>.toml`: name,
stream URL, `City -- blurb`, tags, and `fetch`, the mechanism it tells us
what's playing by (format at the top of `stations/model.py`). `tune`, `np`,
`dial` and the `radio.zsh` words pick it up with no other edit.

```bash
uv run twiddle stations search "kzsu"               # radio-browser.info + SomaFM (--source tunein)
uv run twiddle stations search --genre "dub techno"
uv run twiddle stations probe <stream> --callsign KZSU --homepage https://kzsu.stanford.edu
```

`probe` answers what adding turns on: does a plain-http stream exist (a
Sonos needs one), what the ICY titles look like, and which fetcher applies.
It spots a SomaFM or NTS URL, a Spinitron page for the call sign, or a
widget platform on the homepage. It ends with a draft TOML. Most stations
are data only. One with its own feed needs a fetcher in
`stations/fetchers/`. The Claude Code skill `add-radio-station` has the
whole procedure.

### Why a command may act on a speaker you did not name

Bonded speakers are not independently addressable. Asking for the right Roam
gets you the pair's coordinator, and the answer says so rather than pretending
otherwise:

```json
{ "ok": true,
  "requested": "Sonos Roam (R)",
  "acted_on": "Sonos Roam (L) [192.168.1.4]",
  "redirected_from": "Sonos Roam (R) [192.168.1.8]",
  "reason": "... not independently addressable; commands go to the group coordinator" }
```

This matters more than it looks. **`Invisible`, not `is_satellite`, is what
marks a speaker as unaddressable** — the Beam's surrounds are `<Satellite>`
elements, but a bonded *stereo pair* like the Roams are ordinary
`ZoneGroupMember`s, so the right Roam has `is_satellite=False` and still
cannot take a command. Sending it one anyway is accepted and ignored, which is
indistinguishable from a dropout. Relatedly, a group's coordinator comes from
the `Coordinator` attribute and **never** from the group ID's prefix; here
they genuinely differ.

### Restoring what you found

Snapshot captures **whatever is actually playing**, which is the point — no
doc can say what that is, because it changes. `logs/roam_state_before.json` is
a capture from an earlier session (KALX at volume 43) and is history, not a
target. Run `twiddle status` for the live answer:

```bash
uv run twiddle snapshot --room roam        # -> logs/snapshots/sonos-roam.json, and stdout
# ... do something that writes ...
uv run twiddle restore --room roam         # or --state '<the JSON from above>'
```

`restore` reports each step it took and any that failed, because a restore
that half-worked is worse than one that failed loudly — the next measurement
inherits the difference silently. A live stream has no seekable position, so
restoring one re-issues the URI rather than seeking into it.

Room snapshots don't cover alarms, which belong to the whole household:
`alarm snapshot` before touching them, `alarm restore` after (see *Alarms*).

### Playback and long-running tests (these **write**)

```bash
uv run twiddle stream <url> --room roam --volume 25   # returns immediately
uv run twiddle diag radio <url> --room roam --watch 60  # stays up, logs gaps
uv run twiddle diag serve ~/Music --room roam
uv run twiddle diag soak --room roam --duration 120
```

`rooms`, `status`, `snapshot`, and all of `diag scan|ping|watch|analyse` are
strictly read-only. Every write goes through `play.py`, or for alarms through
`alarms/clock.py`, and both journal it to `logs/interventions.jsonl` — see
*Telling real dropouts from ones you caused*.

## Reading PHY error rates

`/status/proc/ath_rincon/status` exposes a WiFi PHY error counter, which counts
frames that arrived too corrupted to decode — the standard proxy for how hostile
the airspace is.

Two traps, both handled in `devices.sample_phy_rate()`:

1. **The counter zeroes itself on every read.** Its label is "PHY errors since
   last reading/reset" literally. A single sample only means something relative
   to whenever something last polled it, so the sampler primes all speakers
   together, waits a known interval, then reads — the second read covers exactly
   that window.
2. **Only compare like with like — same model *and* same instrument.**
   Different Sonos hardware families use different WiFi chipsets and count
   these differently: a Beam's number is not comparable to a Roam's, while two
   Roams are directly comparable, and that comparison is what tests the "one
   bad unit" theory.

   The rule extends past models to *how you asked*. Timings from two SOAP calls
   (taking the max) are not comparable to a single plain HTTP GET; dividing one
   by the other once produced an apparent 130x difference that was entirely an
   artefact of the instruments. Compare a measurement only against one taken
   the same way.

The `at NNNNN` value in the same file is uptime in **seconds** (verified by
sampling it twice five seconds apart).

## Comparing two points in time

Cumulative counters average over a speaker's entire uptime, which for a
portable left on its charger can be months — so they say little about current
conditions, and nothing about a change you just made. `baseline` records a
stamped snapshot (including **serial numbers**, so a speaker stays identifiable
after it physically moves), and `baseline-diff` subtracts two of them:

```bash
uv run twiddle baseline --label "before swap"
# ... change something, wait ...
uv run twiddle baseline --label "after swap"
uv run twiddle baseline-diff
```

That yields an error rate over just that interval. It is how the swap test is
read: a rate that follows the **serial** indicts the unit, one that stays with
the **room** indicts the location.

Two things it refuses to do, both deliberate: it drops speakers whose counters
reset in between (a reboot makes the delta meaningless rather than zero), and
it never rates a speaker against a different model — see below.

## Transmit errors: the metric that actually travels

`/status/ifconfig` exposes cumulative driver counters, and its TX error
percentage is the most trustworthy cross-model number here — cumulative since
boot and counted identically everywhere, unlike the PHY counter above. A
transmit error is a frame the radio gave up on after retries.

Interface naming differs by hardware family, so `devices.link_stats()` picks
the busiest non-bridge interface rather than hard-coding a name:

| Model | Uplink interface |
|---|---|
| Roam (Atheros) | `ath0` |
| Beam (MediaTek) | `apcli0` |
| Play:1 surrounds | none exposed — only `br0`, which aggregates everything |

`br0` is deliberately **not** used as a fallback for error rates: it counts
every interface at once, so reporting it as radio errors would be wrong. Models
that expose nothing are reported as such and left out of comparisons — absent
data must never be rendered as "0% errors", which reads as a clean link.

A few percent is unremarkable on 2.4GHz. Double digits is not, and matters most
on whichever speaker coordinates a pair or group, since the other speakers'
sync traffic goes out through that same path.

## Running it unattended

The dropouts are intermittent and unpredictable, which makes an attended
session the wrong instrument: by the time you notice, the context is gone. Run
the monitor as a background agent instead, so it is already watching when it
happens.

```bash
uv run twiddle daemon install      # every 30s, rotating at 50MB
uv run twiddle daemon status
uv run twiddle daemon uninstall
```

`install` pins the agent to a mains-powered speaker as its anchor, because
portables sleep and move. `analyse` then reads the rotated logs as well as the
live one, so an event that happened two rotations ago is still found:

```bash
uv run twiddle analyse --log logs/daemon.jsonl
```

**macOS Local Network permission.** On macOS 15 a launchd agent cannot show the
LAN consent prompt, so its first run may die with `[Errno 65] No route to host`
even for plain unicast — the same request works from a terminal, which already
has the grant. Enable the agent's entry under *System Settings → Privacy &
Security → Local Network*; launchd retries every 60s, so collection starts
within a minute and no reinstall is needed. `daemon status` distinguishes this
from a healthy agent by checking whether the log is still being written, not by
reading stderr — launchd appends stderr across restarts, so an old traceback
outlives the problem it describes.

## Telling real dropouts from ones you caused

Re-pointing a stereo pair's coordinator makes the pair re-synchronise, and the
other half briefly leaves the household — indistinguishable from a fault if you
are not tracking it. So every write `play.py` makes is journalled to
`logs/interventions.jsonl`, and `analyse` discounts any vanish within 45s of one:

```
[ i] Sonos Roam (192.168.1.8) dropped 1x right after one of our own commands
     Discounted, not counted as a fault: set_uri on 192.168.1.4, 2s away.
```

Alarms re-point a speaker too, whoever set them, so the monitor also logs the
household's alarm schedule (`alarm_schedule`: ListAlarms plus the local−UTC
offset from GetTimeNow, both reads) whenever it or the offset changes, and copies
it to the top of each new file when it rotates. `analyse` discounts a vanish
within 45s of an enabled alarm's fire time, or of fire time + its duration, by
the schedule in force when it fired:

```
[ i] Sonos Roam (192.168.1.8) dropped 1x right after an alarm or one of our own commands
     Discounted, not counted as a fault: alarm 07:00 on Sonos Roam, 20s away. ...
```

A log written before this has no schedule, so the alarms in it still count.

An alarm the monitor can't read is named in the schedule (`unreadable`) and the
rest are still logged and discounted. Its own fires can't be, so `analyse`
says so once, as an info finding naming each one: a drop one of them caused
would otherwise count as a fault without a word.

Evidence about the speakers has to come from windows where nothing was sent to
them. `watch` on its own never writes, so its windows are clean by construction.

## Playing music without Spotify

Sonos will fetch and play any plain HTTP URL, so no cloud service is required:

* `serve` runs a small HTTP server over your LAN and queues local files onto a
  speaker. The path is **Mac → LAN → speaker**: no Spotify, no Sonos cloud
  queue, no Connect handoff.
* `radio` points a speaker straight at an internet radio stream via
  `x-rincon-mp3radio://`.

This is also the **control condition** for the diagnosis. `twiddle.tone`
generates a long pink-noise WAV with a tick once a minute — serve that for
30 minutes and any gap is both audible and locatable. If a locally served file
plays flawlessly while Spotify drops, the radio is not your problem. If both
drop, the service is not your problem.

## Other traps worth knowing

These bit during the investigation and are encoded in the code; honour them if
you add analysis.

1. **A satellite reporting `PLAYING` does not mean sound is coming out.** A
   bonded satellite is slaved to its coordinator via `x-rincon:` and reports
   `PLAYING` whenever it is *instructed to render* — there is no counter for
   actual audio output. This is an observed false negative, not a theory: on
   2026-09-18 a speaker reported `PLAYING`, with its pair intact, at the exact
   moment a listener in the room heard silence. The coordinator's `RelTime`
   likewise only describes the coordinator's own stream. **Use the affected
   speaker's control-port latency and ICMP loss as the dropout proxy instead.**
2. **SSDP discovery is lossy multicast.** A speaker missing from one discovery
   round is not offline — confirm with an HTTP request before concluding
   anything.
3. **`/status/perf` returns an empty document** on this firmware. Not useful.
4. **Polling at a fixed interval undercounts short faults.** 10-second polling
   missed at least one audible dropout that a listener reported. Every fault
   count produced this way is a **lower bound**, not a measurement.
5. **Serving audio from this Mac puts it in the audio path.** Treat the whole
   serving window as confounded, not just the instant a command was sent — and
   do not ping a speaker you are streaming to. `radio` does *not* have this
   problem: the speaker fetches the stream itself.

## Layout

| File | Role |
|---|---|
| `devices.py` | SSDP discovery, per-speaker state, SOAP, PHY-rate sampling |
| `topology.py` | Groups, bonds, satellites and `VanishedDevices` |
| `monitor.py` | GENA subscriptions + polling; logs transitions to JSONL |
| `report.py` | Findings, severities, hardware-vs-setup discriminators |
| `household.py` | Speakers, groups, name resolution, snapshot/restore |
| `control_cli.py` | The room-naming control commands |
| `alarms/` | Sonos alarms: `model.py` (an `Alarm` that round-trips ListAlarms, its `Recurrence`), `clock.py` (the `AlarmClock` reads, and the version-checked, journalled writes), `baseline.py` (`AlarmSnapshot`, the restore plan and restore) |
| `alarm_cli.py` | `alarm list`, `alarm snapshot` (read-only); `alarm restore` (writes) |
| `play.py` | HTTP file server and transport control (writes) |
| `tone.py` | Soak-test signal generator |
| `spotify_ops.py` | Spotify playback logic with no CLI attached (shared by `spotify_cli` and `scene`) |
| `scene/` | The `scene` TUI: `sources/` (listings), `bands.py`, `players.py`, `app.py` |
| `viz/` | The visualizer (`v`): `tap.py` (ffmpeg → PCM ring), `source.py` (what to tap), `analysis.py` (bands, beats), `canvas.py` (braille/blocks/palettes), `base.py` (the contract), `modes/` (one file each), `screen.py` |
| `cli.py` | Command line entry points |

Logs are JSONL under `logs/`, one record per event, so a long session can be
re-analysed later with `analyse` without re-running it.
