# scene — architecture

How `twiddle scene` works inside: where its data comes from, what a build
does, what the app does, and the rules that keep the two from stepping on
each other. For keys, badges and everyday use, read [SCENE.md](SCENE.md);
this is the map for someone changing the code.

---

## 1. The shape: a producer, a file, and consumers

```
   the network                                   your Mac
 ─────────────────                ─────────────────────────────────────────────
  The List, venue                  ┌────────────┐   publishes   ┌─────────────┐
  calendars, Wikipedia,   ───────► │  builder   │ ────────────► │ dataset.json│
  MusicBrainz, Bandcamp,           │ (scene     │  (atomic)     └──────┬──────┘
  Spotify identity                 │  build)    │                      │ reads
                                   └────────────┘        ┌─────────────┼───────────────┐
                                     ▲ launchd / cron    ▼             ▼               ▼
                                     │ or `r`        scene TUI    scene list /     any other
                                                          │       scene venue      client
                                                          ▼
                                    only per band on screen: song lists, missing identity,
                                    then playback (Spotify / Bandcamp / Sonos relay)
```

- **One producer.** `twiddle scene build` is the only code that scrapes venues
  or bulk-enriches bands. It runs as a one-shot process (no daemon), on a timer
  or on demand.
- **One boundary.** A single versioned JSON file, published atomically.
- **Many readers.** The TUI, `scene list`, `scene venue`, and anything else
  that wants the same view, without importing the UI or any collector.

The reason for the split (issue #7): launching used to mean scraping, so
`scene` was slow to start, dead offline, and hammered third-party sites from a
UI thread. Now launching is reading a file. The split also gives future work
(sharing a dataset between machines, say) a boundary to build on. None of that
is implemented.

---

## 2. Modules

```
src/twiddle/scene/
  model.py         Show; dedupe(). No UI, no network.
  dataset.py       THE BOUNDARY: file format, atomic publish, reader (Snapshot).
                   No Textual, no collectors, no network (a test enforces it).
  builder.py       `scene build`: collect → publish → enrich → publish.
  profiles.py      BandProfile <-> dataset band record (incl. the parts JSON has
                   no type for: nested AlbumInfo, genre.Guess's Counter and set).
  sources/         WHERE SHOWS COME FROM: one module per site or platform.
  venues.py        the watched venues (config) and matching the sources' spellings.
  venue_info.py    hand-kept address/site/description per venue; Wikipedia summary.
  bands.py         WHO A BAND IS: enrichers, assess() (identity grading), BandBook.
  bandcamp.py      throttled, cached Bandcamp search; band choice; tracks; stream URLs.
  genre.py         tags -> genre families (no AI): a band's guess, a show's.
  cache.py         per-user state: pins, aliases, device/theme/window.
  cli.py           scene, scene list, scene venue, scene build, scene schedule.
  app.py / .tcss   the Textual UI: presentation only, everything injected.
  players.py, local.py, pictures.py, instagram.py    playback and images.
```

Dependency rule: `dataset.py` imports only `model` and the standard library.
`builder.py` and `app.py` both depend on `dataset.py`; neither depends on the
other (the app starts the builder as a subprocess, never imports it).

---

## 3. Where the data comes from

### 3.1 Events: 21 sources, merged

Each source is an `EventSource` (`name`, `fetch() -> list[Show]`, raises
`SourceError`). Registered in `sources/base.py`; `fetch_all` runs them in
order and merges with `model.dedupe`.

| Source (`name`) | From | Adds |
|---|---|---|
| `thelist` | foopee.com/punk/the-list (Steve Koepke's Bay Area guide) | bands, age, price, times; covers most rooms |
| `yoshis` | yoshis.com/events/calendar | Yoshi's (The List doesn't carry it) |
| `gilman` | app.showslinger.com | 924 Gilman: flyers, tickets |
| `grayarea` | grayarea.org/visit/events | Gray Area |
| `makeoutroom` | makeoutroom.com + CalendarWiz | Make-Out Room: lineups, flyers |
| `storkclub`, `gamh`, `chapel`, `hotelutah`, `rickshaw` | each venue's WordPress calendar (See Tickets) | flyers, ticket links |
| `independent`, `brickandmortar`, `neckofthewoods`, `bimbos`, `augusthall`, `crybaby`, `cafedunord` | each venue's site (TicketWeb) | flyers, ticket links |
| `ivyroom`, `ashkenaz` | venuepilot.co GraphQL | flyers, tickets, prices |
| `soundroom` | soundroom.org (Squarespace events JSON) | Sound Room |
| `deluxe` | thedeluxesf.com (Simple Calendar) | The DeLuxe |

**Merge (`dedupe`).** Sources spell rooms differently, so rooms are compared
loosely and mapped to the watched venue's name. Two listings of one night in
one room are the same show when their lineups share a band (ignoring a leading
"The", and splitting "A & B"), or when each source lists exactly one show
there that night. The first source's facts stand (The List's price and age
beat a ticketing site's); later sources fill gaps (`flyer`, `tickets`, `title`)
and are recorded in `also`. **Order in the registry therefore matters.**

**Failure isolation.** `fetch_all` catches every exception per source. A failed
source keeps its rows from the previous dataset (`stale=`), so one site's outage
doesn't blank its shows. A source whose markup changed can break its own parser
and nothing else.

### 3.2 Venues

Watched venues come from `venues.py` (defaults plus the user's
`~/.config/twiddle/scene.toml`). Addresses, websites and descriptions are
hand-kept in `venue_info.py`. The build adds only Wikipedia's summary for the
~19 venues that have an article (`en.wikipedia.org/api/rest_v1/page/summary`,
cached 30 days).

### 3.3 Bands: identity, genre (and, in the app, song lists)

For bands playing watched venues in the next `--days` (default 31; ~1,100
bands at the default venues), the builder runs the enrichers in `bands.py`,
lookup first:

| Enricher | Asks | Fills |
|---|---|---|
| `LookupEnricher` (`lookup.py`) | MusicBrainz (~1 req/s), then Wikipedia via Wikidata, Discogs, Bandcamp autocomplete | `ArtistInfo`: origin, genres, members, links, bio; how identity was settled |
| `BandcampEnricher` | Bandcamp search (throttled, 15-min back-off on 429) | the band's Bandcamp page, location, tags (→ genre), photo |
| `SpotifyEnricher` | Spotify search, **only if already signed in** | the Spotify artist and candidates (identity only in a build) |

When a billed name finds nothing ("Mindi Abair Christmas Show"), the lookup
tries trimmed names and keeps one only if the databases *uniquely* identify it
(`alias`, cached 14 days).

**Identity is graded, never silently guessed** (`assess`): `corroborated`
(pinned by hand, or MusicBrainz links this exact Spotify artist, or unique on
Spotify and MusicBrainz *and* placed in the Bay Area), `name_only`,
`uncertain` (several same-named artists), `none`, plus `pending` (a lookup is
running) and `unlooked` (nobody has asked yet). The UI shows the grade and the
reason.

**Not collected by the build:** song lists (2–4 more requests per band, only
useful on play); photos, flyers and Instagram icons (the dataset stores their
URLs; the app fetches images for display).

---

## 4. The builder (`builder.py`)

```
take build.lock ─► load previous dataset ─► fetch_all(stale=previous rows)
      │                                          │
      │                    ┌─────────────────────┘
      ▼                    ▼
 (exit 75 if held)   band records: carried over, or minimal (cache-only genre)
                           │
                           ▼
              PUBLISH #1  (shows + carried bands + last time's venue summaries)   ← seconds
                           │
                           ▼
              Wikipedia summaries for venues, then per band (soonest show first):
                 reuse if enriched within its TTL, else lookup → spotify → bandcamp
                 (checkpoint publish every 25 bands)
                           │
                           ▼
              PUBLISH #final  (complete: true)
```

Properties, each with a test:

- **Locked.** `build.lock` (flock) next to the dataset: a scheduled run and `r`
  never overlap; the loser exits 75 ("already running").
- **Two-phase.** Listings are visible in seconds even when enrichment takes
  most of an hour, and a killed build keeps everything it had published.
- **Incremental.** A band's record is reused for 3–4½ days (the exact time is
  spread by band, deterministically, so a first build's bands don't all expire
  together). Re-enrichment mostly hits MusicBrainz's (30-day) and Bandcamp's
  (14-day) caches.
- **Never loses knowledge.** An enricher that was skipped (signed out,
  `--no-spotify`) or failed keeps its answer from the previous record.
- **Never destroys data it can't read.** A dataset from a newer version, or
  another schema, makes the build stop; only a damaged (non-JSON) file is
  replaced.
- **Rate-limit aware.** A Bandcamp back-off or a Spotify 429 pauses that
  enricher for the rest of the run (`enrichers.<name>: paused: rate-limited`)
  and the build still publishes. Spotify lookups are paced 0.5 s apart.
- **Non-interactive.** Spotify identity is used only if a sign-in already
  exists; the build never opens a browser, so launchd can run it.
- **Personal state stays out.** Confidence is graded without anyone's hand
  pins, and a build without Spotify says "not checked" rather than publishing
  "not on Spotify" for bands it never asked about.
- **Idempotent.** The same inputs produce the same file, timestamps aside.

Measured 2026-09-29: cold bands cost ~2 s each (MusicBrainz's 1 req/s is the
limit), so a cold first build of ~1,100 bands takes 35–40 minutes.

---

## 5. The dataset (`dataset.py`)

One JSON file: `~/.local/share/twiddle/scene/dataset.json`
(`$TWIDDLE_SCENE_DATASET` overrides). Top level:

```
schema, version, generated_at, builder, complete
sources    {name: {ok, fetched_at, count, error}}
enrichers  {lookup|spotify|bandcamp: "ok" | "skipped: …" | "paused: rate-limited"}
venues     [{id, name, match, address, url, about, wikipedia, wikipedia_summary, instagram, map_url}]
shows      [{id, venue_id, day, venue, bands, age, price, times, notes, flags,
             source, also, source_url, tickets, flyer, title}]
bands      {<normalised name>: {name, updated_at, status, identifiers, info,
             lookup_candidates, spotify_artist, spotify_candidates, bandcamp, alias,
             searched, genre, confidence, why, tracks, bc_tracks}}
```

- **Stable identities.** A show's `id` hashes its night, room and headliner (or
  its title and times when no bands are billed, so two untitled nights differ;
  true repeats get `-2`). A venue's `id` is its slug; a band's key is its
  normalised name, and the record carries MusicBrainz, Spotify and Bandcamp ids.
- **Provenance.** `source` + `also` per show; `sources` says when each source
  last succeeded; each band has `updated_at` and per-enricher `status`.
- **Atomic publication.** Write a temp file (private name per process) in the
  same directory, `fsync`, `os.replace`. Readers see the old file or the new
  one, never a mix, and need no lock.
- **Compatibility.** Unknown keys are ignored, a malformed row is skipped, a
  newer `version` is refused with a message. Adding a key doesn't bump the
  version. Indexes (by day, by venue) are built in memory by the reader;
  nothing derived is stored.
- **Not in the file:** hand pins, the chosen output device, theme, window.

Reader API: `dataset.load() -> Snapshot | None`; `Snapshot.shows`,
`.shows_on(day)`, `.shows_at(venue)`, `.venue(name)`, `.band(name)`,
`.generated_at`, `.age_s()`, `.stale()`, `.complete`, `.sources`, `.mtime`.

---

## 6. The app (`app.py`)

The app is a reader with two small exceptions.

**Load.** On mount it calls `dataset.load()`, shows the shows, seeds the
`BandBook` with a `BandProfile` per band record (`profiles.from_record`), and
takes genre guesses for the table from the records. Nothing is scheduled.
Every 20 s it compares the file's mtime and reloads if it changed, so a
scheduled build or a checkpoint of a running one appears by itself.

**Reload without disruption.** If only band records changed (same shows), the
app updates genre cells and lineup badges in place, and redraws the open band
card only if something it shows changed. Cursor, focus and the selected track
stay put. A new show list re-renders the table.

**Freshness.** The subtitle reads `dataset built 3h ago`, `· enriching…` while
a build is publishing checkpoints, `· stale -- r rebuilds` after a day, or
`no dataset yet`. With no dataset at all the app runs the builder once itself; a
stale dataset is flagged but never rebuilt unasked.

**`r`.** Runs `python -m twiddle scene build` as a subprocess in its own
session, then reloads. A held lock is reported, not an error. Quitting doesn't
wait for a build: it finishes and publishes on its own.

**The two things the app still looks up itself, for the band on screen only:**

1. *Song lists* (`TrackEnricher`): Bandcamp and Spotify tracks.
2. *Missing identity*: a band outside the build's window, Spotify on a Mac
   that was signed out at build time, or a band whose Spotify pin disagrees
   with the dataset's pick.

### The BandBook and profile states

Each enricher on a `BandProfile` is in one state:

```
idle ──(band selected: get(urgent=True))──► pending ──► running ──► done | error: …
```

- `from_record` marks an enricher `done` if the record says so, else `idle`
  (errors are retried, not trusted; `tracks` is always `idle` from a record).
- Only the band on screen is woken. There is no lineup prefetch.
- `enrich` results can change each other (MusicBrainz landing after Spotify may
  name a different Spotify artist), so an enricher can declare `wants_rerun`;
  `TrackEnricher` also re-checks itself if identity landed while it ran.
- **Reload merge (`seed`).** A profile mid-lookup isn't replaced; the new record
  is deferred and applied when the lookup ends. Otherwise the new record
  wins, except that identity answered locally and missing from the record is
  kept, and song lists are kept only if they belong to the same artist.
- **Pins.** A hand pin (`m`) that disagrees with the dataset's Spotify pick
  makes the app forget the dataset's artist and tracks and look up the pinned
  one. Pins live in `cache.py`, never in the dataset.

### Threading

Textual's loop never blocks. Network work runs in worker threads and reports
back with `call_from_thread`. `lookup.py` isn't thread-safe (a module-level
throttle, a read-modify-write cache), so every MusicBrainz call goes through one
lane (a priority queue with a single thread; the on-screen band jumps the
queue). Spotify and Bandcamp run on a small pool.

---

## 7. Playing

Unchanged by the dataset. `players.py` (Spotify Connect, with relay safety and
hand-back), `local.py` (this Mac's own librespot) and dial's `Outputs` (Bandcamp
tracks on this Mac via `ffplay`, or on a Roam). Bandcamp stream URLs expire in
about a day, so each is resolved fresh at play time. `SCENE.md → Playing` has the
rules for not interrupting what's already playing.

---

## 8. Files and who writes them

| Path | Written by | Holds |
|---|---|---|
| `~/.local/share/twiddle/scene/dataset.json` | builder | the published dataset |
| `…/scene/build.lock` | builder | the lock (empty) |
| `~/.cache/twiddle/lookup.json` | builder, app, dial, `np` | MusicBrainz/Wikipedia/Discogs answers, 30 days |
| `~/.cache/twiddle/scene/bandcamp.json` | builder, app | Bandcamp search answers, 14 days (misses 3) |
| `~/.cache/twiddle/scene/aliases.json` | builder, app | billing → trimmed name that identified it |
| `~/.cache/twiddle/scene/band_pins.json` | app (`m`) | your Spotify choices |
| `~/.cache/twiddle/scene/state.json` | app | device, theme, window, all-venues |
| `~/.cache/twiddle/scene/instagram/`, `dial/art/` | app | venue icons, images |
| `~/Library/Logs/twiddle-scene-build.log` | launchd | the scheduled build's output |

The builder and the app are separate processes writing the same cache files, so
every JSON cache write goes to a private temp name and is renamed into place
(`cache._write`, `lookup._cache_save`, `dataset.publish`). A torn read of
`lookup.json` used to read as `{}` and could have been saved back, wiping the
30-day cache.

---

## 9. Running it

```bash
twiddle scene build [--days N] [--all-venues] [--no-spotify] [--dry-run]
twiddle scene schedule [--hours 6]     # prints a launchd agent; installs nothing
twiddle scene                          # the app
twiddle scene list | venue             # plain text, read the dataset
```

Exit codes for `scene build`: 0 done, 1 nothing built / refused, 75 another
build holds the lock. `schedule` prints the checkout it is run from as
`--project`, so install the agent from your main checkout.

---

## 10. Failure modes

| What happens | Result |
|---|---|
| A source is down | its rows from the last dataset are kept; `sources.<name>.ok = false`; the app and `scene list` warn |
| Every source is down and there's no previous dataset | nothing published, exit 1 |
| MusicBrainz / Bandcamp slow or erroring | that band's answer is retried next build |
| Bandcamp rate-limits, or Spotify 429 | that enricher pauses for the run; the build still publishes |
| Spotify not signed in | `enrichers.spotify: skipped`; the app looks up the band on screen |
| Build killed midway | the last checkpoint stands |
| No network at all | the app browses the last dataset; only song lists, playback and images need the network |
| Dataset file damaged | the app reports it once and keeps what's on screen; the next build replaces it |
| Dataset from a newer twiddle | readers refuse it with a message; the build won't overwrite it |

---

## 11. Tests

`tests/test_scene_*.py`; nothing touches the network, a speaker or Spotify.

- `test_scene_dataset.py`: format, stable ids, atomic publish (including a
  crash mid-write and a reader during a write), version gate, reader import
  purity, profile round-trips, pin behaviour, lookup-cache atomicity.
- `test_scene_builder.py`: merge and provenance, stale fallback, two-phase
  publish, incremental reuse, locks, rate limits, no Spotify, pins excluded, no
  song lists, never overwriting an unreadable dataset.
- `test_scene_app.py`: the TUI driven by Textual's pilot over fakes, including
  offline browsing with every network function set to raise, checkpoints not
  moving the cursor, `r`, bootstrap, staleness, quitting mid-build.
- `test_scene_cli_dataset.py`: the CLI readers, `build` and `schedule`.
- `tests/conftest.py` redirects the dataset path and the caches, so a test run
  never touches your real ones.

---

## 12. Extending

- **A new listings source:** one module in `sources/`, registered in
  `base._registry()` (order matters); skill `add-venue-source`. It takes
  effect at the next `scene build`.
- **New band knowledge:** an `Enricher` in `bands.py` (`name`, `serial`,
  `enrich`, optionally `wants_rerun`); add it to the builder's list and to
  `profiles.ENRICHERS`.
- **A new client of the data:** `dataset.load()`; nothing else is needed.
- **Changing the file:** adding a key is free; changing what a key means bumps
  `dataset.VERSION`.
