# scene — who's playing locally, and what do they sound like?

A terminal app for deciding whether a show is worth going to. It lists
upcoming shows at your venues (by default about forty East Bay and SF
rooms, from Thee Stork Club and 924 Gilman to Yoshi's, the Fox and the
Fillmore; see *Your venues*), tells you who each band is, and plays
their music from Spotify: out of this Mac's own speakers, on your phone, or
on the Roams through the relay. No Sonos is needed; see
[Running it without the Roams](#running-it-without-the-roams).

It is the first piece of a larger music discovery app, so it is built as
three swappable layers. See [Extending it](#extending-it), and
[SCENE-ARCHITECTURE.md](SCENE-ARCHITECTURE.md) for how the pieces fit: what
is pulled from where, the builder, the dataset, and the app.

---

## Quick start

```bash
cd twiddle && uv sync                # once (first time on a new Mac: QUICKSTART.md)
source scripts/radio.zsh              # once per shell (or add it to ~/.zshrc)

shows                                 # open the app
```

1. **Pick a show.** The middle pane lists the next 30 days at your venues.
   Move with `j`/`k` or the arrow keys. `t` changes how far ahead it looks
   (All upcoming reaches about five months). `a` switches to every venue on
   The List (~150). Both choices are remembered.
2. **Press `enter`** to jump into the lineup. Each band carries a badge:
   **✓** means this is confirmed to be the right Spotify artist; for the
   others, see [the badges](#the-badges).
3. **Read about them.** The Band pane shows where they're from, their
   genres, a Wikipedia or Discogs bio, their members and their links. Press
   `o` to open their Bandcamp or website in a browser.
4. **Press `enter` again** to go to their tracks, then **`p`** to play one.
   The first time, you'll be asked which device to use (`d` changes it
   later). `space` pauses and `n` skips; you move through that band's
   tracks, not a random radio queue.
5. `q` quits. `?` shows every key at any time.

**First run.** `scene` reads a local *dataset* that `scene build` compiles
(see [The dataset](#the-dataset-and-how-it-stays-fresh)). With none yet, the
app runs the builder once for you: listings appear in seconds, the band
details fill in over the following minutes. Then put the build on a timer
(`twiddle scene schedule`) and the app never has to wait for the network.

Prerequisites (on a new Mac, [QUICKSTART.md](QUICKSTART.md) walks through them):
- `twiddle spotify auth` and `twiddle scene login` have been run once.
- **This Mac (speakers)** needs no Spotify app. The app runs its own small
  Spotify player (librespot) the first time you play to it, and stops it
  when you quit, so the music stops with the app.
- The **Roams (relay)** device works whether or not the relay is running;
  picking it starts one. It only appears on a Mac that has run the relay.

Without the TUI:

```bash
shows build                   # compile the dataset scene reads (network; the only thing that does)
shows schedule                # print a launchd agent that runs the build every 6h
shows list                    # this week's shows at your venues, as text (reads the dataset)
shows list --venue stork      # one venue (partial names work)
shows list --days 3 --json    # for scripts
shows list --venue greek --links   # each show's listing link under it
shows venue buzzard           # where it is, what it is, its website
shows venue                   # every watched venue with its address
shows --venue ivy             # open the app on one venue
shows --dry-run               # everything works, but nothing plays
```

(`shows` is `uv run twiddle scene`; the long form works from the repo.)

---

## Keys

| Key | What it does |
|---|---|
| `j` `k` / arrow keys | move |
| `tab` / `shift+tab` | next / previous pane |
| `enter` | go in a level: venue → shows → lineup → tracks → play |
| `p` | play the highlighted track (or the band's first track): Bandcamp's on this Mac or the Roams, Spotify's anywhere |
| `space` | pause / resume Spotify; stops a Bandcamp track |
| `n` | next track (the band's next Bandcamp song, when one is playing) |
| `d` | choose where to play; a Bandcamp track playing moves there (and stops where it was) |
| `R` | put the Roams back on what they were playing before you previewed something |
| `m` | choose which Spotify artist this band is; your choice is remembered |
| `o` | open the band's Bandcamp, website or Wikipedia page |
| `c` | copy the show to send someone: bands, date and time, venue and street, price, and the listing, ticket and flyer links (`pbcopy`; elsewhere the terminal's clipboard) |
| `l` | open where the show was listed: The List's page at that venue, or Yoshi's event page |
| `f` | open the show's flyer full size in the browser (the Stork Club's and Gilman's shows have them) |
| `i` | the venue (highlighted in the venue list, else the show's): address, website, what it is, Wikipedia's paragraph; `w` opens the site, `g` a map |
| `t` | Next 30 days → All upcoming (~5 months) → Tonight → Next 7 days → Next 2 weeks (remembered) |
| `/` | filter by band, venue or genre (`metal`, `reggae`; `esc` clears) |
| `a` | your venues only ↔ every venue on The List (~150 rooms; remembered) |
| `v` | visualize: the whole window becomes the music (GUIDE.md → Visualizer); `←/→` picture, `c` colours, `,`/`.` delay, `v` back |
| `r` | run `scene build` and reload (a scheduled build appears by itself within ~20s) |
| `ctrl+t` | next colour theme (remembered) |
| `?` | help |
| `q` | quit |

---

## Running it without the Roams

For a friend with a Mac and no Sonos. Everything except the Roams works;
**This Mac (speakers)** plays through the Mac's current audio output
(speakers, headphones or AirPods, whatever macOS is set to).

Spotify tracks need **Spotify Premium**: Spotify only lets Premium accounts
play on third-party players and control playback from other apps. Without
any Spotify sign-in, This Mac is still offered, and a band's Bandcamp tracks
play there; its badge reads "Spotify: not signed in".

```bash
brew install librespot uv
git clone https://github.com/jeromebanks/twiddle.git && cd twiddle && uv sync
uv run twiddle spotify auth --client-id <your client id>   # once
uv run twiddle scene login                                  # once
uv run twiddle scene
```

Step-by-step, including creating the client id and a troubleshooting table:
[QUICKSTART.md](QUICKSTART.md).

- **Client id.** Create a free app at
  <https://developer.spotify.com/dashboard>, with the Web API and redirect
  URI `http://127.0.0.1:5588/login`, and use its client id. Spotify
  commands won't run without one. (A friend's app works too if they add
  your Spotify email under *User Management* in its dashboard: a
  Development-mode app only allows users on that list.)
- **`scene login`** opens a browser to sign in the Mac's player. It stores
  the credentials in `~/.cache/twiddle/librespot-local/`. If you've
  already run `relay login`, this step isn't needed: that sign-in is copied.
- Then press `p` on a track and choose **This Mac (speakers)**. It appears
  in Spotify as "*<the Mac's name>* (scene)", including in the phone app,
  which can also control it while the app is open.

---

## The badges

The hardest part is *not* finding music; it's making sure it's **the right
band**. Local bands are often on no database at all, and when they are, the
name often belongs to someone bigger too. This week's List has bands called
"Shape", "Eraser" and "Sleeves". A Development-mode Spotify app
also hides popularity, followers and genres, so a single same-named result
proves very little.

| Badge | Meaning | What to do |
|---|---|---|
| ✓ | Confirmed. Either you chose it with `m`, or MusicBrainz links that exact Spotify artist, or the name is unique on Spotify *and* MusicBrainz/Bandcamp places the band in the Bay Area | just play |
| ~ | Exactly one Spotify artist has this name, and nothing confirms it | listen with some suspicion; `m` if it's wrong |
| ? | Several Spotify artists have this exact name | `m` to choose |
| ✗ | Not on Spotify under this name | `o` for their Bandcamp; `m` to search by hand |
| … | still looking them up (MusicBrainz allows 1 request a second) | wait a second |

The Band pane always says *why* it chose what it did, in grey italics under
the name.

Measured on 2026-09-22 against real services:

| Band | Result |
|---|---|
| Chuck Johnson | ✓ (Spotify has two, and MusicBrainz's link picks the right one) |
| Maria BC, Kathryn Mohr | ✓ (MusicBrainz link) |
| Holybasil909 | ✓ (Spotify spells it `HOLYBASIL_909`; unique, and Bandcamp says Oakland) |
| Eraser | ? (three artists share the name) |
| Shape, Girl Chow, Totalna Tama | ✗ |

**A billing isn't always a band name.** Venue calendars bill shows: "Mindi
Abair Christmas Show", "A Tribute To Grover Washington Jr.", "Christian
Sands Trio". When the billed name finds nothing, the app tries up to three
trimmed names (show words like *Christmas*, *Show*, *Trio*, *Tribute To*
removed) and uses the first one MusicBrainz, Discogs or Bandcamp
identifies *uniquely*. Spotify and Bandcamp then search that name, and the
*why* line says "searched as “Mindi Abair”". A trim that finds nothing, or
several artists, is never used. A billing the databases already know is
never trimmed. Measured 2026-09-25: Mindi Abair, Grover Washington Jr. and
Christian Sands were found this way; "Cherronda G I'm Every Woman Show" is
in no database and stays ✗.

---

## What it sounds like

The **Sounds like** column is a best guess at each show's genre: metal,
punk, hardcore, rock, indie, pop, electronic, hip-hop, reggae/ska, jazz,
folk/country, soul/funk, latin, experimental, blues, classical. No AI is
involved. Bands tag themselves on Bandcamp ("doom", "sludge", "post-metal"),
MusicBrainz users tag artists, and `scenespec/genre.py` folds those tags into
families:

- **Tags are matched whole, never as substrings.** So "post-punk" counts as
  indie, not punk, "dubstep" as electronic, not reggae, and "folk punk" as
  punk. Tags that aren't sounds ("San Francisco", "soundtrack") count for
  nothing.
- **Each band's own Bandcamp genre counts double.**
- **Each band votes once, and the headliner's vote counts 1.5×.** A
  close runner-up is shown too ("punk/rock"); in the table, a pair too long
  for the column becomes "electronic+".
- **`?`, dimmed, means the guess is by name only:** the Bandcamp page was
  found by name, and the band isn't local. On 2026-09-25, "Inayah" at the
  Great American (an R&B singer) matched a French death-metal band by name.
  Same-named pages that all agree still give a `?` guess (Thelma And The
  Sleaze has two, both Nashville rock).
- **Blank** means no band on the bill is tagged anywhere.

`/` filters by it: type `metal` or `reggae`.

**Coverage, measured 2026-09-25** (before the venue list grew): 64 of 83
shows in the next 30 days at your venues got a genre, 48 of them without a
`?` (158 of 272 bands).

**How it gets there.** The *builder* asks Bandcamp about every band at your
venues in the next 31 days, one request a second (2026-09-25, 272 bands:
**4½ minutes**; the window is ~1,100 bands now, see *What a build does*), and stores each
band's guess in the dataset; the table just reads it. Answers are cached for
two weeks (misses for three days) in `bandcamp.json`, so later builds are
instant. A 429, or three failures in a row, pauses Bandcamp requests for 15
minutes rather than hammering an endpoint that `dial` and `lookup` use too;
the build records `bandcamp: paused: rate-limited` in the dataset, publishes
what it has, and the next build finishes the rest. Visiting a band refines
its guess with MusicBrainz's tags and the Bandcamp page MusicBrainz links.

**Only your venues, next 31 days, go to the network** (`--days`,
`--all-venues` change that). `a` (every venue) and "All upcoming" would mean
thousands of requests to an endpoint that isn't ours, so bands there carry
only what the cache already knew when the dataset was built. The app does not
look them up when you open one: that waits for a build that covers them.

---

## Pictures

The Band pane shows the band's photo, with where it came from underneath:

| Photo | When |
|---|---|
| their Bandcamp photo | first choice: the page the Bandcamp lookup chose (see [Bandcamp songs](#bandcamp-songs)) |
| Spotify's artist image | for ✓, and for ~ captioned "Spotify (name match)" so you can tell them apart. Spotify's Development mode still returns images (checked 2026-09-25) |
| Wikipedia's lead image | for the band MusicBrainz identified |
| a monogram tile | otherwise, including ? (several Spotify artists, none chosen) |

Under the venue list is the highlighted show's venue: its flyer when the
show has one (`f`, or a double-click on it, opens it full size), else the
venue's logo -- hand-picked, else its **Instagram profile picture** -- or a
tile in the venue's colour. Double-click the venue's picture or name to
open its Instagram (also `n` in the `i` screen).

Instagram, measured 2026-09-26: a profile page gives its picture to anyone
(the `og:image` tag, 100x100), and nothing else. Posts, and so flyers,
need a logged-in session: every API answers 401 without one. The pictures
are kept 30 days in `~/.cache/twiddle/scene/instagram/`. Handles are in
`venue_info.INSTAGRAM`, taken from each venue's own site. That colour is also the ● next to it in the venue
list and the shows table, so rooms stand out at a glance.

Images use the same renderer as `dial`: the kitty graphics protocol in
Ghostty, coloured half-blocks elsewhere. `TWIDDLE_DIAL_IMAGES=halfcell`
forces half-blocks, for example inside tmux if pictures draw badly. Photos
and logos share dial's disk cache (`~/.cache/twiddle/dial/art/`).

---

## Playing, and not stepping on other things

A Spotify account plays on **one device at a time**, so starting a track
here stops whatever Spotify is playing elsewhere. The app asks before it
does that: if something is playing on another device, the first `p` shows
a warning in the bottom bar, and a second `p` (within 20s) goes ahead.

- **A podcast on your phone.** You're warned that it will stop. Nothing is
  logged: this is just Spotify.
- **The Roams, through the relay.** You're warned, and then the handoff is
  written to `logs/interventions.jsonl` as `spotify_transfer_from_relay`.
  That way `analyse` doesn't mistake the silence that follows for a
  dropout. The app also remembers the album or playlist,
  track and position the Roams were on, so **`R` puts them back** exactly
  there. `R` is journalled too.
- **Playing to the Roams (relay).** The app checks that the room is actually
  pointed at the relay, and re-points it only if it has drifted (to a radio
  station, say). This is the same code path as `twiddle spotify play --room
  roam`.

`--dry-run` resolves everything and shows "would play …" instead.

### Bandcamp songs

Most bands on The List are on Bandcamp and not on Spotify, so the Tracks
pane lists their Bandcamp songs **first** (up to 8, newest release first),
then Spotify's. `p` plays the highlighted one, or the first on the list.

Which Bandcamp page is theirs: the one MusicBrainz or Discogs links; else
the only band with that exact name; else the only one of several same-named
bands that is in the Bay Area or California; else none. Labels never count.

A Bandcamp track plays without Spotify, on:

- **This Mac (speakers):** through `ffmpeg`. If the Mac's own Spotify player
  was playing, it's paused first. It stops when the app quits.
- **The Roams (relay):** the Roam fetches the track itself, as a file at its
  https URL. Not as a radio stream: Sonos fetches those over plain http,
  and Bandcamp's CDN only redirects http to https. If Spotify is playing through the relay, the first `p`
  asks; the second pauses Spotify, journalled as `spotify_paused_for_radio`
  with `source: scene`, and the Roam's own switch is journalled like any
  speaker write. The Roam **stays stopped** when the track ends. **`R`**,
  any Spotify play to the Roams, or resuming Spotify with `space` / `n`
  re-points it at the relay first. Without that, Spotify would play into a
  relay nobody is reading.
- **Any other device** (a phone): not possible, because that's Spotify's.
  The app says so and plays nothing.

While a Bandcamp track plays, `space` stops it, `n` plays the band's next
one, and the bottom bar says "Bandcamp, on …". Bandcamp's stream links
expire in about a day, so each is fetched fresh when you press `p`.

**On hardware (2026-09-25):** until then the Roam *refused* every Bandcamp
track -- UPnP error 714, "illegal MIME type": a Sonos guesses the type from
the URL's path, and Bandcamp's (`.../mp3-128/3766738436?...`) has no
extension. `play.track_didl` now names `audio/mpeg` in a `<res>`, and the
Roam accepts it and plays it as a track (position advancing; heard by
nobody -- the house was empty). The URL comes back from the speaker
unchanged, so moving the track to another output stops it here.

---

## Where the listings come from

[The List](http://www.foopee.com/punk/the-list/) is Steve Koepke's
volunteer-run Bay Area concert guide, updated daily since the '90s. It was
chosen after measuring the alternatives (2026-09-22):
- Stork Club's own domain is parked, and Stay Gold Deli's redirects to a
  running club.
- Ivy Room's calendar is a VenuePilot widget, and Eli's site is on Wix.
- The List covers all four venues, and about 150 others, in plain HTML.

Caveats:
- It can lag the venues.
- Its spelling of a band is a search term, not an identity. That is why the
  badges exist.

These are collected by `scene build`, not by the app; see the next
section.

## The dataset, and how it stays fresh

`scene` does not scrape anything. A separate, one-shot process,
`twiddle scene build`, does all the collecting (The List, every venue's own
listings, Wikipedia for venue summaries, and each band's MusicBrainz /
Bandcamp / Spotify lookups) and **publishes one file**:
`~/.local/share/twiddle/scene/dataset.json` (`$TWIDDLE_SCENE_DATASET`
overrides). The TUI, `shows list` and `shows venue` only read it. That makes
launching instant, makes browsing work with no network at all, and gives any
other program the same view without importing a line of `scene`'s UI:

```python
from twiddle.scene import dataset      # stdlib + Show; no Textual, no network
snap = dataset.load()                  # None if nothing has been built yet
snap.shows_on(date.today()); snap.shows_at("Ivy Room"); snap.band("Girl Chow")
snap.generated_at, snap.stale(), snap.sources   # freshness + per-source health
```

### Running it

```bash
twiddle scene build               # collect, enrich, publish (minutes the first time)
twiddle scene build --days 14     # enrich less far ahead
twiddle scene build --no-spotify  # skip Spotify identity even if signed in
twiddle scene build --dry-run     # do it all, publish nothing
twiddle scene schedule            # print the launchd agent (installs nothing)
```

**A practical schedule: every 6 hours** (what the old in-app cache used).
`twiddle scene schedule` prints a launchd agent plus the two commands to
install and remove it; save it to
`~/Library/LaunchAgents/com.twiddle.scene-build.plist` and
`launchctl bootstrap gui/$(id -u) <that file>`. It runs `uv run --project
<this checkout> twiddle scene build` at load and every `--hours`, at low
priority, logging to `~/Library/Logs/twiddle-scene-build.log`. cron works
too (`0 */6 * * * cd ~/dev/twiddle && uv run twiddle scene build`). No
daemon stays running. The build is never interactive: Spotify identity is
compiled only if this Mac is already signed in (`twiddle spotify auth`),
otherwise the dataset says `spotify: skipped: not signed in` and those bands
show as unlooked until a signed-in build.

### Dead letters: what a build could not identify

Most bands and venues resolve deterministically. The rest go to a queue,
`dead-letters.json` next to the dataset, with *what was already tried*, so an
AI (or you) starts from the evidence instead of repeating it:

```bash
uv run twiddle scene dlq                       # counts, then the busiest pending letters
uv run twiddle scene dlq export --kind band    # JSON lines: name, reason, evidence, sample shows
uv run twiddle scene dlq resolve "band:girl chow" --alias "Girl Chow" --by ai
uv run twiddle scene dlq resolve "venue:ritz-san-jose" --resolution '{"address": "...", "url": "..."}'
uv run twiddle scene dlq abandon "band:private event" --note "not a band"
uv run twiddle scene dlq scan                  # re-derive from the dataset (a build does this too)
```

Reasons: `unfound` (MusicBrainz, Bandcamp and, if it ran, Spotify all empty),
`ambiguous` (several same-named candidates, nothing to choose), and
`unwatched_venue` (a room in the listings that is on no venue list, so it has
no address, site or description). A band whose lookup *errored* or was
rate-limited is not a dead letter; the next build retries it. Each band letter
carries cheap deterministic clues (`several_names_joined`, `looks_like_a_night`,
`unbalanced_parenthesis`, `show_words`), because much of the tail is not a band
at all ("Astrozombies SF , Rusty Chains" unsplit). Before a billing becomes a
letter, rules clear what is certainly not an act (`bands.non_band`: "Private
Event", "Membership Meeting", karaoke, "... Nights", "Salsa Crazy Mondays"), and
a source no longer splits a billing inside a bracket ("Black Flag (Greg Ginn,
...)" is one band with a note). Rooms are merged by `venue_names.cluster`: one
letter per room, not per spelling ("Hopmonk, Novato" / "Hopmonk Tavern, Novato"),
with the city, state and street address read out of the listing and a mistyped
city ("Memlo Park") corrected. An ambiguous band with exactly one candidate
described as local ("pop duo from Oakland, CA") is settled by rule
(`resolved_by: rule`; `reopen` undoes it) and the next build looks that artist up
by its MusicBrainz id.

A letter stays `pending` until resolved; a later build marks it `resolved`
(found) or `expired` (no longer billed), and never reopens one a person or an AI
closed. What a build applies from a resolution: a band's `alias` (searched
under that name from the next build) or `mbid` (that MusicBrainz artist among
same-named ones); either way the band is looked up again. A
venue's resolution is stored and exported but **not yet put into the dataset**:
that needs the client to tell watched rooms from merely known ones.

### Watching a build, and being polite

```bash
uv run twiddle scene status          # phase, bands done, bands/min, ETA, request rates vs caps
```

A running build writes `build-status.json` next to the dataset every couple of
seconds (read-only to read, safe any time; a build that stopped reporting says
*stalled*), and prints one line per checkpoint to its log:
`150/723 bands · 38.0/min · eta 15m · musicbrainz 52/60rpm · spotify 41/120rpm`.
The ETA is measured seconds per band over the last 50, times the bands left,
waits included: request counts would mislead, since reused and cached bands cost
almost nothing.

Each service has a cap, and they are not all the same kind: MusicBrainz's 1
request a second is *its* published limit; Bandcamp's is our own throttle;
Spotify publishes no number, so its 120 a minute is a budget of ours (a 429 is
the real signal, counted as "told us to slow down").

**Staying under the limit in the first place** is `ratelimit.py`: every
Spotify request goes through one governor that enforces a budget of several
windows at once (stock: 10 per 10 s, 100 a minute, 1,500 a day), shared by
every process on the machine through a small ledger, so the build, the TUI and
any other Spotify tool spend one allowance. The daily window is the one that
matters: Spotify answered the first full build with a `Retry-After` of 23
hours. When it does say stop, the lockout is written to the ledger, and
neither the next build nor any other tool sends the request that would
lengthen it (`scene build` records `spotify: skipped: rate-limited (retry
after Ns)` without loading a session). `twiddle limits` shows the budgets and
what is used; they are Spotify-unpublished numbers of ours, so calibrate them
from `scene status` over a few builds and override them in
`~/.config/twiddle/ratelimits.toml`.

**Backpressure.** A service that says "slow down" *with a time* (Spotify's
`Retry-After`, Bandcamp's own 15-minute back-off) is waited out in full, once,
and the band retried, so a long first build finishes unattended; the status
shows the wait and adds it to the ETA. It is never retried early (Spotify has
lengthened `Retry-After` for every early retry). It is paused for the rest of
the run, as before, when no time is given, the time is over 15 minutes, the
limit returns right after waiting, or three waits are spent; the dataset then
says `spotify: paused: rate-limited (retry after 600s)` and the next build
retries. Every wait also doubles the gap between bands for that service, and
every success narrows it back. A wait holds `build.lock`, so a scheduled build
that fires meanwhile exits 75 ("already running"), which is fine.

### What a build does

1. Takes `build.lock` next to the dataset, so a scheduled run and `r` never
   overlap (the second exits with status 75 / "already running").
2. Fetches every source. One that fails keeps its rows from the previous
   dataset and is marked `ok: false` (the app and `shows list` say so).
3. **Publishes shows and venues at once**, so `r` and a first run show
   listings in seconds.
4. Enriches the bands playing your venues in the next 31 days, one at a
   time: who they are (MusicBrainz, the Bandcamp page, the Spotify artist if
   this Mac is signed in) and what they sound like. **Not their song lists**:
   those are 2-4 more requests a band and only matter when you press play, so
   the app fetches them for the band on screen. A band enriched in the last
   3-4½ days (spread by band, so they don't all expire together) is reused,
   and the underlying MusicBrainz (30 d) and Bandcamp (14 d) caches still
   apply. It republishes every 25 bands (a killed build keeps its work).
   Spotify lookups are paced (0.5 s), and a Spotify or Bandcamp rate limit
   pauses that source for the rest of the run (`enrichers.spotify: paused:
   rate-limited`) while the build still publishes.

   **How long:** measured 2026-09-29, the default month at the default venues
   is ~1,100 bands, and cold bands (nothing cached) cost about 2 s each,
   bound by MusicBrainz's 1 request/second. So the *first* build is ~35-40
   minutes (it publishes listings first, so the app is usable meanwhile).
   After that, a band's record is rebuilt every 3-4½ days from warm
   MusicBrainz (30 d) and Bandcamp (14 d) caches, which is quick; only bands
   new to the lineup are cold.
5. Publishes the finished dataset (`complete: true`).

Every billed band gets a record, enriched or not. Publishing is atomic
(write a temp file, `fsync`, rename), so a reader sees the old dataset or
the new one, never half of one.

### What the app does about freshness

- The subtitle says `dataset built 3h ago`; `· enriching…` while a build is
  still working through bands; `· stale -- r rebuilds` after a day.
- A build that finishes while the app is open shows up on its own (the app
  checks the file's mtime every ~20 s). `r` runs `twiddle scene build` in a
  subprocess and reloads when it ends.
- **No dataset yet:** the app builds one, once, and says so. **A stale one:**
  the app only says so; it never starts a build you did not ask for.
- The only work the app still does itself is for the one band on screen: its
  song lists (Bandcamp and Spotify) and your own pin. It never looks up who a
  band is: a band outside the build's window, or Spotify on a Mac that was
  signed out at build time, shows a dim `·` badge until the next build covers
  it. No lineup prefetch, no venue scraping, no genre scan.
- A build that finishes (or checkpoints) while you are browsing updates the
  genre column and badges in place; it does not move your cursor or focus.
- Quitting during a build does not wait for it: the build runs in its own
  session and finishes and publishes on its own.
- Still fetched for display only, fine to fail offline: flyers, band photos
  and venue icons.

### The file

```
{"schema": "twiddle.scene.dataset", "version": 1, "generated_at": "...", "complete": true,
 "id": "bay-area-music", "name": "Bay Area live music", "region": "...", "kind": "music",   (optional)
 "sources":   {"thelist": {"ok": true, "fetched_at": "...", "count": 412, "error": null}, ...},
 "enrichers": {"lookup": "ok", "spotify": "skipped: not signed in", "bandcamp": "ok"},
 "venues": [{"id": "ivy-room", "name": "Ivy Room", "address": "...", "wikipedia_summary": {...}, ...}],
 "shows":  [{"id": "9f2c…", "venue_id": "ivy-room", "day": "2026-10-06", "bands": [...],
             "source": "thelist", "also": ["ivyroom"], "source_url": "...", "tickets": "...",
             "flyer": "...", ...}],
 "bands":  {"girl chow": {"name": "Girl Chow", "updated_at": "...", "status": {...},
             "identifiers": {"mbid": "...", "spotify_id": "...", "bandcamp": "..."},
             "info": {...}, "genre": {...},   # song lists ("tracks", "bc_tracks") are the app's
             "confidence": "name_only", "why": "..."}}}
```

- **Stable identities.** A show's `id` hashes its night, room and headliner
  (its title and times when it bills no bands); a venue's is its slug; a
  band's key is its normalised name, and its record carries the MusicBrainz,
  Spotify and Bandcamp ids it settled on.
- **Provenance.** `source` + `also` name every source that listed a show,
  with their links; `sources` says when each was last fetched successfully.
- **Confidence is graded without anyone's pins.** Your `m` choices stay in
  `band_pins.json`; the app applies them on load (a band you pinned to a
  different artist than the build found has that artist fetched when you
  open it). The published data holds evidence, not one person's clicks.
- **Compatibility.** Unknown keys are ignored, malformed rows skipped, and a
  `version` newer than the reader knows is refused with a message. Adding a
  key does not bump the version. There are no stored indexes; readers build
  theirs in memory from the file.
- Out of scope here, by design: any network sharing or multi-publisher
  scheme. This is the local producer/consumer boundary those would build on.

### Your venues

The defaults, in `venues.py`:

- **East Bay:** Thee Stork Club, Ivy Room, Stay Gold Deli, Eli's Mile
  High, 924 Gilman, Yoshi's, Greek Theatre, Fox, Paramount, UC Theatre,
  Cornerstone, Freight & Salvage, Starry Plough, Crybaby, First Church of
  the Buzzard.
- **SF:** Great American Music Hall, the Midway, Bottom of the Hill (closes
  after New Year's Eve 2026), Independent, Rickshaw Stop, Fillmore,
  Kilowatt, Chapel, Regency Ballroom, August Hall, Warfield, Masonic,
  Bimbo's, Brick & Mortar, DNA Lounge, Castro Theatre, Great Northern, Cafe
  du Nord, Swedish American Hall, Knockout, Hotel Utah, Neck of the Woods,
  4 Star, Black Cat, Civic Auditorium.

The later additions (2026-09-25) were picked from The List's own venue
counts, with a web check that each is still open. Yoshi's comes from its own
calendar; the Stork Club and Gilman come from The List *and* their own pages,
which add flyers and ticket links (see *Sources* below); the rest from The
List. The Fox matches
Oakland's only: The List also has one in Redwood City.

Two more were added on request and are **not on any source yet**: **Oakland
Secret** (577 5th St, West Oakland; it lists shows on Instagram and RA,
neither of which lets a script in) and **Pussy Palace** (a punk house on
34th St, West Oakland, with monthly Sunday backyard shows; The List has
carried it before). They show up whenever The List lists them.

### Venue details

Every default venue has an address, a website and a line about what it is
(`venue_info.py`); 19 also have a Wikipedia article, whose summary is
fetched by the build and stored in the dataset (cached 30 days), so
opening the venue (`i`, or `shows venue <name>`) works offline. The card under the venue list shows the street.

They are kept by hand, like the icons, because nothing machine-readable
covers small rooms. Checked 2026-09-25: every address matched the venue's
own site, its Wikipedia article or listings sites. Where a venue has no
site of its own (the Buzzard, Oakland Secret, Pussy Palace, Stay Gold,
whose domain now redirects to a running club), the link is its Facebook or
Instagram. Pussy Palace is a private house, so only its street is recorded.

`scene.toml` takes `address`, `url`, `about` and `wikipedia` (an article
title) per venue; a venue named like a built-in one keeps the built-in
details for any field you leave out. An unwatched venue has no details, but
`g` still searches the map for The List's "Name, City".

Watching a venue means its next 30 days of bands are looked up by the
build. Measured 2026-09-29 the default month is ~1,100 bands, ~2 s each
the first time (MusicBrainz's 1 request/second is the limit), then cached
for 14-30 days. Trim the list in `scene.toml` if that's more than you want.

Not yet covered: **SFJAZZ** is on no source (its site refuses scripts), and
**Freight & Salvage** is only partly on The List (its own calendar is drawn
by JavaScript). Each would need its own source, like Yoshi's. The List spells some rooms several ways
(Gilman appears as "924 Gilman Street, Berkeley", "924 Gilman St.,
Berkeley", "924 Gilman, Berkeley" and more), so a venue matches on a
substring, and the venue pane groups every spelling into one row. To change
the list, create
`~/.config/twiddle/scene.toml`:

```toml
[[venue]]
name  = "Stork Club"
match = ["stork club"]

[[venue]]
name  = "Cornerstone"
match = ["cornerstone, berkeley"]      # a substring of The List's venue name
icon  = "https://…/logo.png"           # optional; without it, a coloured tile
```

Icons are hand-picked rather than scraped from venue sites; the newer rooms have none yet and get tiles. On 2026-09-25,
`staygolddeli.com` redirected to a running club, Gilman's favicon was Wix's
generic one, and the Stork Club's site had none. The defaults have logos
for Ivy Room, Eli's, Gilman and GAMH; the Stork Club and Stay Gold get tiles.

A `[[venue]]` list replaces the defaults, so list every venue you want.
`shows list --all-venues` shows The List's own spellings.

### Sources

| Source | Covers | How |
|---|---|---|
| `thelist` | ~150 rooms, not Yoshi's | foopee.com's by-club pages |
| `yoshis` | Yoshi's, Oakland | yoshis.com/events/calendar, plus each multi-night run's detail page |
| `storkclub`, `gamh`, `chapel`, `hotelutah`, `rickshaw` | Stork Club, Great American, Chapel, Hotel Utah, Rickshaw Stop | See Tickets' WordPress plugin: each calendar's list view, `?list1page=1..N` (`sources/seetickets.py`, one line per venue) |
| `independent`, `brickandmortar`, `neckofthewoods`, `bimbos`, `augusthall`, `crybaby`, `cafedunord` | those rooms, plus the Swedish American Hall (on Cafe du Nord's page) | TicketWeb's WordPress plugin, one page each (`sources/ticketweb.py`) |
| `gilman` | 924 Gilman, Berkeley | its public ShowSlinger widget, `app.showslinger.com/e1/460/924-gilman/8c96699fd6` |
| `ivyroom`, `ashkenaz` | Ivy Room, Ashkenaz | VenuePilot's public GraphQL (`venuepilot.co/graphql`, accounts 992 and 1228; `sources/venuepilot.py`) |
| `soundroom` | Sound Room, Oakland | its Squarespace events collection as JSON (`?format=json`; `sources/squarespace.py`) |
| `deluxe` | The DeLuxe, S.F. | its Google Calendar as rendered by WordPress "Simple Calendar" (`sources/simplecal.py`); no flyers |
| `grayarea` | Gray Area, S.F. | its events page; courses, workshops, talks and exhibitions dropped |
| `makeoutroom` | Make-Out Room, S.F. | CalendarWiz (months ahead, needs a cookie session) plus flyers from its blog (next night or two) |
| `kalx` | ~75 rooms, East Bay and S.F. (incl. Sweetwater Music Hall and Hillside Club, on no other source) | KALX 90.7's weekly "Events" post via the site's WordPress REST API (`sources/kalx.py`); see below. Last in the registry |

**KALX** (kalx.berkeley.edu, measured 2026-09-29) posts one page a week,
"Events: September 28 – October 4, 2026": a day heading with the year, a
region (East Bay / San Francisco), then a line per room, `Venue: act, act`.
The site's REST API (`/wp-json/wp/v2/event`) lists the recent posts with their
HTML in one request, so nothing guesses next week's URL. It is the widest
calendar here (about 75 rooms across two weeks) and the only one carrying
Sweetwater Music Hall and the Hillside Club, but it has **no times, prices,
flyers or links**, and only the current week or two. So it is registered last:
where another source knows a night its facts win and KALX only confirms it.
Two guesses are made and can be wrong: an entry that reads like a night
("Open Mic", "Irish Céili Dance with live band", "Karaokiki") is kept by its
name rather than split into bands; and a room's city comes from a small table
(KALX drops it, and files Sweetwater, in Mill Valley, under "San Francisco").
The `Name: a, b, c` form is read as a named night with a lineup. Not covered:
anything beyond the posted weeks.

The Stork Club, Gilman and Ivy Room are on The List already; their own pages add
**flyers and ticket links**, plus nights The List skips. Measured
2026-09-26:

- **Stork Club.** See Tickets' WordPress plugin. Its list view is read
  page by page with plain GETs (the site's own pager uses AJAX; no need).
  Bands come from the `headliners` field, split on commas only, because
  titles are cut at ~60 characters ("… Artificial Muscl") and "Make Do and
  Mend" is one band. DJ, karaoke and queer dance nights bill no bands:
  they're listed in *italics* under their name, with a flyer, and nothing
  looks them up. Private hires are dropped. Dates have no year; the
  weekday picks it.
- **Gilman.** Its own calendar is a dead Wix widget, and ShowSlinger's venue
  page wants a login *without* the token in the URL above; with it, it's
  public. Every price there read "$5" against The List's $10-$30, so
  ShowSlinger's price is ignored. A benefit weekend is one event over
  several nights with different lineups, so it carries only its name.
- **See Tickets venues** share one parser; the Stork Club's quirks below
  are the plugin's. GAMH lists festival passes as events ("4-Day Passes");
  those are dropped.
- **TicketWeb venues.** Seven sites, one plugin, four themes: the date alone
  is written "Sat 9.26", "Sat, Sep 26", "September 28 Mon" or "September 29,
  2026", so fields are found by class, never by position. The image is the
  promoter's upload -- a tour admat as often as a press photo -- and is
  shown as the flyer either way. Theme-dependent: age and price (Brick &
  Mortar, Neck of the Woods), door times.
- **Ivy Room.** Every event has a flyer. Bands come from the event name
  ("SLEEPBOMB + THEYA + OMINESS"), because VenuePilot's `artists` field was
  copied between events. The price is read from the blurb ("$18 Door").
  "genre - …" becomes a note. Weekly nights (happy hour, line dancing) are
  kept by name. Private parties are dropped.
- **Added 2026-09-26 on request:** Ashkenaz, the Sound Room, Spats, The
  DeLuxe, Gray Area and the Make-Out Room. Only Gray Area (1 show) and Spats
  (1) were on The List, so these rows come from the venues' own pages.
  **Spats** has no website or calendar; it's watched for The List, with its
  Instagram picture. **The DeLuxe** posts no flyers (walk-in, pay at the
  door). **Make-Out Room** flyers only exist for the next night or two,
  because that is as far ahead as its blog goes. Most of its nights are
  named DJ nights. **Gray Area** events are kept by name, not split into
  bands.
- **Stay Gold Deli has no source.** Its domain redirects to a running club,
  DoTheBay lists nothing, RA and Bandsintown refuse scripts, and its flyers
  are on Instagram, which wants a login. The List still covers its shows.

Adding another: see the `add-venue-source` skill (`.claude/skills/`).

Yoshi's quirks, measured 2026-09-25: a run ("10.9 to 10.16") is not every
night in between, so its detail page supplies the real nights and set
times; VIP packages and meet & greets ("SHOW TICKET NOT INCLUDED") are
dropped; titles in capitals are softened and split into headliner and
guests ("… W/ …", "… FEAT …") with subtitles kept as notes. What the
title rules can't trim is left to the badges' trimmed-name search (see
*The badges*).

The same show from two sources is listed once, **merged**: the first
source's facts stand (The List's bands, age and advance/door price) and the
others fill the gaps (flyer, tickets). Two listings are one show when they
share a date, a room (the watched venue's name, so "Yoshi's, Oakland" and
"Yoshis Jazz Club, Oakland" are one), and either

- a band, compared without case, punctuation or a leading "The" (The List
  writes "Well" for The Well, and bills openers in a different order), or
- nothing else: each source lists just that one show there that night.
  That catches a themed night under two names ("Hell Comes to Oakland IV"
  / "Halloween tribute band Extravaganza"), and it can't merge Gilman's
  members' meeting into the gig the same evening.

Against the live pages on 2026-09-26 this merged all 15 of Gilman's
ShowSlinger shows and all 26 Stork Club shows The List also had, with no
duplicate rows. Rooms you don't watch only merge on the same spelling, so
Oakland's Fox and Redwood City's stay apart.

**Every show links to where it was listed.** A Yoshi's show links to its
event page (tickets, set times, the blurb). A show from The List links to
its venue's section of The List's by-club page. That page is renumbered as
shows are added, so a List link is good for sharing this week, not for
keeping: after a while it can land on the wrong page, where the venue's
section is one or two pages over. `c` in the app copies a show with its
link, ready to text.

If one source fails, the others still refresh and the failed one keeps its
shows from the last dataset, so a brief outage at either site doesn't blank the other.
That includes a merged row: if the Stork Club's site is down, The List's
row keeps the flyer it had.

---

## tmux

It's built to live in a tmux pane:
- **Below 160 columns** the venue list (and its icon) hides, the band pane
  stacks under the shows, and the photo shrinks. Side by side with the
  Sounds like column, the Lineup column had no room at 118 columns and
  about 10 characters at 145.
- **Colour:** for true colour, add this to `~/.tmux.conf`:

  ```
  set -g default-terminal "tmux-256color"
  set -as terminal-overrides ",*:RGB"
  ```

- **A dedicated window:** `tmux new-window -n shows 'zsh -ic shows'`

---

## What it remembers

The published dataset is `~/.local/share/twiddle/scene/dataset.json` (see
above; delete it and the next build recreates it). Everything else lives in
`~/.cache/twiddle/scene/`, and all of it is safe to delete:

| File | Holds |
|---|---|
| `../librespot-local/` | the Mac's own player: its Spotify sign-in, and `librespot.log` if it misbehaves |
| `bandcamp.json` | Bandcamp's answer for each band name: page, location, genre, tags, photo (14 days; misses 3) |
| `venue_wiki.json` | Wikipedia's paragraph for each venue that has an article (30 days) |
| `aliases.json` | which trimmed name each billing was found under, or that none was (14 days) |
| `band_pins.json` | your `m` choices: "this band is *that* Spotify artist", or "not on Spotify" |
| `state.json` | last device, theme, time window, all-venues toggle, and what the Roams were playing before a preview (for `R`) |

Band lookups share `np -i`'s cache (`~/.cache/twiddle/lookup.json`, 30
days).

---

## Extending it

```
src/twiddle/scenespec/   THE CONTRACT -- what a producer writes and a client reads
  dataset.py       the published file's format, atomic publish, the reader (Snapshot);
                     no Textual, no collectors, no network (a test enforces it)
  model.py         Show -- the domain type; dedupe()
  band.py          BandProfile and the confidence grades
  profiles.py      BandProfile <-> dataset band record; apply_pin()
  venue.py         Venue, VenueInfo, matching The List's spellings, from_rows()
  genre.py         tags -> genre families; a band's guess, a show's

src/twiddle/scenedata/   ONE PRODUCER -- the bespoke part; never imports scene
  sources/         WHERE SHOWS COME FROM
    base.py          EventSource protocol, registry, fetch_all (one failing source ≠ blank app)
    thelist.py       The List scraper
    yoshis.py        Yoshi's own calendar (The List doesn't carry it)
    seetickets.py    See Tickets WordPress calendars (Stork Club, GAMH, Chapel, Hotel Utah, Rickshaw)
    ticketweb.py     TicketWeb WordPress listings (Independent, Brick & Mortar, Neck, Bimbo's, ...)
    gilman.py        924 Gilman's ShowSlinger widget: flyers, tickets
    venuepilot.py    VenuePilot's GraphQL (Ivy Room, Ashkenaz): flyers, tickets, prices
    squarespace.py   Squarespace events collections as JSON (Sound Room)
    simplecal.py     WordPress Simple Calendar pages (The DeLuxe)
    grayarea.py      Gray Area's events page
    makeoutroom.py   Make-Out Room: CalendarWiz + blog flyers
    kalx.py          KALX 90.7's weekly events post: ~75 rooms, no times/prices/links
  builder.py       `scene build`: collect (fetch_all), enrich, publish; the only writer
  bands.py         WHO A BAND IS
                     Enricher protocol; LookupEnricher (MusicBrainz → Wikipedia/Discogs/Bandcamp),
                     SpotifyEnricher, BandcampEnricher; assess() grades identity; REGION
  bandcamp.py      Bandcamp search (cached), band choice
  venues.py        the stock watched venues (+ scene.toml)
  venue_info.py    each venue's address, website, description, Instagram; Wikipedia summaries
  cache.py         aliases, Bandcamp and Wikipedia answers

src/twiddle/scene/       THE CLIENT -- reads a dataset, plays music; no scenedata code
  book.py          BandBook: profiles seeded from the dataset; PinEnricher, TrackEnricher
  instagram.py     a venue's Instagram profile picture, no login
  pictures.py      which photo a band gets; fetching + tiles are dial/art.py's
  players.py       WHERE AUDIO GOES
                     Player protocol; SpotifyConnectPlayer (incl. relay safety + handback)
  local.py         this Mac's speakers: the app's own librespot (rodio -> CoreAudio)
  cache.py         your pins and state (the shows are the dataset's)
  app.py/.tcss     the Textual UI -- presentation only: reads the dataset, injected services
  cli.py           `scene`, `scene list/venue` (readers), `scene build`, `scene schedule`

src/twiddle/bandcamp.py  shared: the throttle, a release's songs, fresh stream URLs
```

**A new listings source** is one module in `scenedata/sources/`. The
`add-venue-source` skill walks through it; `yoshis.py` is a small worked example:
- Give it a `name` and a `fetch() -> list[Show]` that raises `SourceError`
  on failure.
- Register it in `base._registry()`.
- Shows from different sources are merged by `model.dedupe` (same date and
  watched venue, plus a shared band or one show each); see *Sources*. A
  venue's own site belongs after The List in the registry, so The List's
  facts win and the site adds `flyer` / `tickets`.
- A night with no bands gets `bands=[]` and a `title`; nothing looks it up.

**New band knowledge** (Last.fm similar artists, YouTube, setlists) is one
class in `scenedata/bands.py`:
- Give it a `name`, a `serial` flag, and `enrich(profile)`.
- Set `serial = True` if it uses a rate-limited or non-thread-safe library.
  It then runs on the single lookup lane, one request at a time; the band
  on screen goes to the front of the queue.
- Implement `wants_rerun(profile)` if it should redo its work after another
  enricher lands. Spotify does this when MusicBrainz names a different
  artist.

**A new way to play** (Bandcamp streams for the ✗ bands, radio through
`tune`) is a class with the `Player` methods: `devices`, `play`, `pause`,
`resume`, `next`, `now`, `back_to_relay`, `close`. `local.py` shows how to
own a helper process: start it lazily, reuse one that's already running,
and on `close` stop only the one you started.

**A new screen or client** (recommendations, "bands like ones I've played", a
calendar export) reads `scenespec.dataset.load()` and never needs `fetch_all` or
`BandBook`; a `Player` is separate again. None of them import Textual.
**A new listings source or band enricher** takes effect at the next
`scene build`.

Playback logic shared with the CLI lives in `spotify_ops.py`. It raises
`PlaybackError(message, hint)` and never prints, so a UI can show the
errors. Tests: `tests/test_scene_*.py`. The UI tests drive the real app
with Textual's pilot over fakes, so they touch no network, Spotify account
or speaker.
