# Diagnosis: WFMU artist and song, and other stations' titles (#47)

Approved revision 3, signed off by @jeromebanks on 2026-10-03 ([diagnosis](https://github.com/jeromebanks/twiddle/issues/47#issuecomment-5971797152), [sign-off](https://github.com/jeromebanks/twiddle/issues/47#issuecomment-5971829825), [approval record](https://github.com/jeromebanks/twiddle/issues/47#issuecomment-5971831948)). The text below is rev 3 exactly as approved.

## Your reply, answered

- **Can the RSS be repaired?** It can't, because it isn't a now-playing feed. `playlistfeed.xml` is *"WFMU's most recent playlists, updated every time a DJ publishes a new playlist"*. It covers every WFMU channel (Rock'n'Soul Radio and others are in it too), and its first item is whichever DJ last saved a playlist. That's why the show was wrong. WFMU does have two live sources with the fields you want, so this revision **replaces** the RSS with them (task 2):
  - `wfmu.org/currentliveshows.php` (about 650 bytes) gives the current song and artist, the show with its host, and the **ID of the live show's playlist**.
  - `wfmu.org/playlists/shows/<id>` is that show's playlist, one row per song: **artist, track, album, label, year, format, the DJ's comments**, WFMU's own song ID, and an "approx. start time" column (empty in both shows I checked).

  Those fields go into what twiddle keeps for each song, so a later liked-songs or play-history feature can use them. Building that feature is a separate issue.
- **Are we extracting songs for "just played"? Is there a bespoke approach?** Not for WFMU, not today. Stations whose fetcher publishes its own history (KEXP, Spinitron, SomaFM, Radio France, Rainwave) fill "just played" with `NowPlaying.recent`. Every ICY station, WFMU included, falls back to what dial itself saw change while it was open (`dial/feed.py:146-152`), stored as raw titles. That's why your screenshot's list is raw sentences that only go back as far as dial has been open. With this fix, WFMU joins the stations that publish their own history: the live playlist fills "just played" with the whole show so far, parsed and with album and label. comedy247, koreancitypop and kcrw have no published history, so theirs stays dial-built, but with artist and song parsed.
- **Scope:** confirmed as wfmu, comedy247, koreancitypop and kcrw.

## What is wrong

WFMU's stream already says exactly what is playing, but twiddle doesn't read it. The ICY title is `"Song" by Artist on Show on WFMU`. twiddle only understands `Artist - Song`, so WFMU shows the whole sentence as a raw title, "About the artist" says *Nothing is naming an artist*, there's no info lookup or `discover`, and "Just played" lists the raw sentences. Meanwhile the show line comes from WFMU's playlist RSS (`(latest published)`), which is a list of recently *published* playlists across all of WFMU's channels, not what's on air. In the screenshot it says *Michael Shelley's show* while the stream itself says *Fool's Paradise*.

In the code, `split_title` (`src/twiddle/stations/icy.py:48`) is the only title parser. It accepts nothing but `" - "`, and `fetchers/wfmu.py` passes the ICY result through untouched. The only per-station shaping is `tidy_title`, and that's hard-coded for one iHeart variant. Stations with a different shape get `music = false` in the catalog, which throws the artist away. Nothing lets a station declare how its titles are built, and `add-radio-station`'s `stations probe` sorts every title into `artist-song`, `iheart`, `show-like` or `blank`, so it never prompts anyone to write one.

## Reproduction

Read-only: I read each ICY-based station's live `StreamTitle` once with `icy.icy_title` (45 stations, 2026-10-03, one sample each, no speaker involved). tokyogroove returned HTTP 502 and wasn't sampled.

| station | live ICY title | what twiddle does with it | broken? |
|---|---|---|---|
| **wfmu** | `"Man From Mars" by Butch Paulson with "The Motations" on Fool's Paradise on WFMU` | no `" - "`, so no artist or song; the show comes from stale RSS | **yes: the issue** |
| **comedy247** | `BOBCAT GOLDTHWAIT - text="The Game Of Love" song_spot="M" … amgArtworkURL="https://i.iheart.com/…" …` | `tidy_title` misses this iHeart variant (space-separated, no `artist=`), so the **song becomes the whole attribute blob** | **yes: wrong data shown and looked up** |
| **koreancitypop** | `Namee · Scarecrow (1985)` | `music = false` (its catalog comment documents the shape), so the artist is discarded | **yes: parseable, but thrown away** |
| kcrw | `Good Food-Evan Kleiman-join.kcrw.com` | `music = false`: shown raw | partly: it's show + host, not a song |
| kaaoschip | `Kaaosradio 24h - YONHIRVE` / `Kaaosradio 24h - Artist - Song` | `music = false` | inconsistent shape; leave as is |
| gamesboro, retropc, ksdt, nectarine, comedy181, dublab | game/track, archive labels, jingles, blank | `music = false` or no split | correct as is |
| wsm | `AIM - CROOK N CHASE 40 H3S2` (automation label) | split into a nonsense artist | minor, intermittent; out of scope |

In this one sample, the other ~30 ICY stations sent a clean `Artist - Song`. A single read can't rule out occasional other shapes (wsm's catalog comment says Opry broadcasts name the show). comedy247 is the catalog's only iHeart (`revma.ihrhls.com`) stream. Stations with their own feed (Spinitron, KEXP, SomaFM, …) don't go through `split_title` at all.

## Evidence

- `src/twiddle/stations/icy.py:48` `split_title`: `if not title or " - " not in title: return None, None`. Its docstring even names WFMU as an example of a title not to trust.
- `src/twiddle/stations/icy.py:61` `tidy_title`: matches only `key="value",` comma-separated fields with both `artist=` and `title=`. comedy247's variant has neither the commas nor `artist=`.
- `src/twiddle/stations/fetchers/wfmu.py:12-18`: `icy(station)` and then `np.show = <RSS title> + " (latest published)"`. The RSS is the last playlist *published* on any WFMU channel, not the show on air. On 2026-10-03 its top three items were Michael Shelley, Jukejoint Gold and Rock'n'Soul Radio while Fool's Paradise was on air.
- `wfmu.org/currentliveshows.php`, fetched 2026-10-03: `<span class="nowplayingtext">"Bury Me Face Down (So I Can See Where I'm Going)" by Cowboy Copas</span>`, `<p class="nowplayingreload">Fool's Paradise with Rex</p>`, and `See Playlist` → `/playlists/shows/169127`, the live show.
- `wfmu.org/playlists/shows/169127`: `<table id="drop_table">`, one `<tr id="drop_N">` per song with `td.song.col_artist`, `col_song_title`, `col_album_title`, `col_record_label`, `col_year`, `col_media`, `col_comments`, `col_live_timestamps_flag`, plus a `KDBsong-<id>` WFMU song ID. Pages are 130-630 KB, so they get fetched only when the playlist ID or the current song changes, never on every poll.
- `src/twiddle/dial/feed.py:146-152`: for ICY stations, "Just played" is built from successive `NowPlaying`s. Each row stores `artist` and `song or raw_title`, and a change is detected on `(artist, song, raw_title)`. Parsed songs show up as `Artist — Song` with no extra work. A show-change title (`Fool's Paradise with Rex`) has no song, so it **must keep `raw_title`**, or its row renders blank and the change goes undetected.
- `src/twiddle/dial/app.py:536-543`: *This station names the show, not each song* appears whenever there's a `raw_title` and no artist. It's generic, not WFMU-specific, so it goes away once WFMU's artist is parsed and still shows correctly on show changes. No change needed.
- Fetchers that already publish history (`NowPlaying.recent`): `kexp.py`, `spinitron.py`, `somafm.py`, `radiofrance.py`, `rainwave.py`. WFMU will be the sixth.
- `src/twiddle/stations/fetchers/__init__.py:20`: the fetcher table describes wfmu as "ICY + wfmu.org playlist RSS (the DJ)"; that description changes with the fix.
- `src/twiddle/stations/probe.py:193` `title_shape`: no category for "parseable, but not `Artist - Song`", so the skill's guidance for those is `music = false`.
- WFMU details seen in the screenshot and the samples: songs can contain `—`, parentheses and their own text; artists can be inverted (`Pretenders, The`) or carry a `with "…"` credit; a bare `Fool's Paradise with Rex` (no quotes) appears at show changes. `split_title`'s docstring also records WFMU sending `Your DJ speaks over ...` titles: not a song and not a show.

## Proposed fix

1. **A per-station title pattern** (no dependency). Stations get an optional, declarative way to say how their ICY title is built, which yields artist/song, and show/host where the title carries them. It lives in the catalog entry or in a named parser that the fetcher uses. `Artist - Song` stays the default, and `music = false` still means "never an artist". `tidy_title`'s existing iHeart rule moves into the same mechanism. Unit-tested with the captured titles above.
2. **WFMU** (needs 1). Parse `"Song" by Artist on Show on WFMU` into song, artist and show. Treat a bare `Show with Host` title as a show change: show and host, no artist, and **`raw_title` kept** so "Just played" and change detection still see it. A `Your DJ speaks over ...` title is neither song nor show: no artist and no show override, shown raw. Normalise `Name, The` to `The Name` for the artist. A `with "…"` credit stays in the artist shown, and the lookup uses the part before it. ICY stays the real-time fallback whenever the site is unreachable.
3. **WFMU's live sources replace the RSS** (needs 2). Use `currentliveshows.php` for the song, artist, show and host. Use the live playlist (`/playlists/shows/<id>`) for the **album** of the current song (so lookups get more exact), and for `NowPlaying.recent`: the show so far, newest first. Each row carries artist, song, album, label, year, format, the DJ's comment, the playlist URL and WFMU's song ID, so a future liked-songs or play-history feature has them. Fetch the playlist only when its ID or the current song changes, with parse functions tested against captured pages. The RSS fetch and its `(latest published)` label are removed, and the fetcher table row is updated.
4. **comedy247** (needs 1). Read this iHeart variant: comedian and track, and `amgArtworkURL` as `art_url`. Correct `catalog/comedy247.toml`'s comment, which says `tidy_title` already handles its titles.
5. **koreancitypop** (needs 1). `Artist · Song (year)` becomes artist and song (year dropped), and `music = false` is removed.
6. **kcrw** (needs 1). `Show-Host-join.kcrw.com` becomes show and host (still never an artist).
7. **`stations probe` and the `add-radio-station` skill** (needs 1). `probe` reports titles that are consistent but not `Artist - Song` as their own shape instead of `show-like`. The skill gains a step: capture a few titles, write the station's pattern and its test, and use `music = false` only when there's no artist to recover. WFMU becomes its worked example.

## Verification

- Offline tests: each pattern against the captured titles above, including the WFMU edge cases (a quoted song containing ` by `/` on `, an inverted artist, a `with "…"` credit, a bare show-change title that keeps `raw_title`, a `Your DJ speaks over ...` title) and comedy247's blob. Every catalog station whose titles are `Artist - Song` today still splits the same way.
- `tests/test_catalog.py` stays strict, so a malformed pattern fails the catalog test, and the daemon import skips it with a warning, as today.
- Offline tests for WFMU's two pages, against captures of `currentliveshows.php` and a live playlist: a song row with album, label and comment; a row with empty fields; an off-air or no-playlist page that falls back to ICY.
- Read-only by hand: `uv run twiddle np wfmu -i` names the artist and song, shows the live show and host (not the RSS), and the info lookup finds the band. `uv run twiddle np comedy247` shows the track name, not the blob. In `dial`, WFMU's "About the artist" fills in, and "Just played" lists the live show's playlist from its start (with album), even right after dial opens.
- **Poster:** open `dial` on WFMU and confirm the panel looks like the screenshot, but parsed.

## Open question (default if you don't say)

1. **Where the extra fields live.** If you don't say, label, year, format, comment, playlist URL and WFMU song ID go in each `recent` row, plus the current song's album. `NowPlaying` itself gains no new fields until a liked-songs or history issue needs them.

## Safety class

**Read-only.** This changes how now-playing text is parsed and adds two read-only fetches of wfmu.org pages (the same kind of read as the Spinitron and KEXP fetchers): `np`, `np -i`, `info`, `dial` display, `stations probe`. Fixing and verifying it never writes to a speaker, needs no `--dry-run`, and journals nothing. The only path to a write stays as it is today: `np -d` / `discover` once a track is picked, and that now receives a correct artist.

