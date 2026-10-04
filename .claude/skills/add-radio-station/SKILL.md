---
name: add-radio-station
description: Add an internet radio station to `twiddle` (tune / np / dial), tag it, and give it a now-playing source — or fix one whose now-playing stopped working. Use when asked to add a station or a batch of stations ("add some dub techno stations", "add KZSU"), find stations by genre, tag stations, or write a fetcher for a station's own playlist feed.
---

<!-- No dollar-digit sequences in this file: the skill loader substitutes them with arguments. -->

# Adding a radio station

A station is **one TOML file** in `src/twiddle/stations/catalog/<key>.toml`.
`tune`, `np`, `dial` and the `radio.zsh` words all pick it up from there;
nothing else needs editing. The format is documented at the top of
`stations/model.py`.

What makes a station more than a stream is its **now-playing source**, named
by `fetch`. Most stations are on a platform we already read, so adding them is
data only. A station with its own one-off feed needs a fetcher written.
That's the only part needing judgement, and it is Section 4.

Two read-only commands do the legwork. **Use them, don't hand-roll curl
calls**:

```bash
uv run twiddle stations search <words> [--genre G] [--country CC] [--source S]
uv run twiddle stations probe <stream-url> [--callsign KXYZ] [--homepage URL]
```

## The fetchers we have

| fetch | platform | fetch_args | stations on it now |
|---|---|---|---|
| `icy` (default) | the stream's own ICY title | `titles = "<shape>"` when titles aren't "Artist - Song" but name an artist in a shape of their own (`stations/titles.py`, step 2a); `music = false` only when there's no artist to recover; `note`; `encoding = "cp932"` for a Shift-JIS server (never guessed) | kcrw, dublab, comedy247, koreancitypop, sleep stations |
| `talk` | ICY, shown as a segment and never taken for an artist | | bbc, wnyc |
| `spinitron` | spinitron.com/CALLSIGN (college/community DJs log spins there) | `callsign` if it isn't the `name` | kalx, wkcr, kspc, kxlu, kdvs, kxsf, kzsc, whrb |
| `somafm` | somafm.com/songs/CHANNEL.json (channel read from the URL) | | groovesalad, beatblender, ... |
| `radiofrance` | api.radiofrance.fr/livemeta/pull/ID | `rf_id` | fip |
| `nts` | nts.live/api/v2/live (the show, never the song) | `channel` ("1"/"2") | nts |
| `kexp` / `kqed` / `wmbr` / `wfmu` | that station's own feed | | one each |
| `rainwave` | rainwave.cc/api4/info (channel read from the URL) | `sid` to override | rainwavegame, rainwaveocr, rainwavechip |
| `airtime` | Airtime Pro: `<id>.airtime.pro/api/live-info-v2` (id read from `<id>.out.airtime.pro`): the show and schedule; never falls back to ICY | `id` to override | ottava, fmsetagaya, shonanbeach |
| `streamabc` | streamabc/regiocast (German decade stations): `api.streamabc.net/metadata/channel/<key>.json`. The key is `audiotheque_channel_external_id` in the site's Nuxt payload; the response's `channel` equals the stream's ICY name | `channel` (required) | eighties, nineties, sunshine90s, and the 80s80s/90s90s genre channels (grunge90s, darkwave80s, ...) |

The table in `stations/fetchers/__init__.py` is the canonical copy. Keep the
two in step when you add a fetcher.

## 1. Find it

```bash
uv run twiddle stations search "kzsu"                 # radio-browser + SomaFM
uv run twiddle stations search --genre "dub techno" --limit 20
uv run twiddle stations search "kzsu" --source tunein # when the others lack a working stream
```

- **`ALREADY IN CATALOG`** means stop, unless the task is to replace that
  station's URL.
- radio-browser **finds** stations, but don't trust it for the details. Its
  tags are often empty, and its first URL may be a low-bitrate one (its top
  KEXP is 64k). Prefer the station's own site's stream, then the
  highest-bitrate plain-http one.
- SomaFM results are always data only (`fetch = "somafm"`), and always
  tagged `somafm` (Section 3).
- For a batch ("five dub techno stations"), shortlist on votes, bitrate and
  `ok`, probe each, and keep only those with a real now-playing source or
  good ICY titles. A station the dial can only show as "(no title)" is a
  weak add. Say so rather than padding the list.

## 2. Probe it

```bash
uv run twiddle stations probe http://stream.example.org:8000/live --callsign KXYZ --homepage https://kxyz.org
```

Always pass `--homepage` (and `--callsign` for a US station) when you know
them. The homepage is where Spinitron links and widget platforms show up.
Read every line of the report:

- **`sonos`**: must say `plain http OK`. `play_radio` hands the speaker
  `x-rincon-mp3radio://`, which a Sonos fetches over http. With no
  plain-http stream, the station plays on the Mac and **not on the Roam**.
  Still add it if it's worth it, but put that in a TOML comment and tell
  the user.
- **`titles`**: its value decides what the dial shows.
  - `artist-song` is enough on its own.
  - A shape's name (`wfmu`, `iheart-attrs`, `iheart-space`) means the
    titles were in that registered shape (beside, at most, titles it reads
    as nothing: an iHeart spot or show name, WFMU's "Your DJ speaks over
    ..."), and the draft already says
    `titles = "<shape>"`. Only the anchored, specific shapes are ever
    guessed (`DETECTABLE` in `probe.py`); a station-anchored one like `kcrw`
    never is.
  - `blank` means ICY shows nothing, so the station needs a feed.
  - `show-like` means the titles fit no shape we know. Read them: if they
    name an artist in some consistent form, write a shape for them (step
    2a). Only if they're show names, with no artist to recover, use
    `music = false`, which the draft carries until then.
- **`problem ICY title didn't arrive`**: the stream stalled while the probe
  was reading its metadata. `icy.icy_title` has no wall-clock bound in the
  dial, `np` or `dial list`, and NTS's https edge once blocked it for over
  100s. **Don't give such a station an ICY-based fetcher** (`icy`, `talk`,
  or a fallback to `icy`). Use a feed, or leave the station out.
- **`problem ... redirects to ...`**: a load balancer handing out edges.
  Keep the station's own URL, which the draft already does.
- **`VERDICT`** has four possible outcomes:
  - **`known platform ... data only`**: go to step 3.
  - **`needs a custom fetcher`**: go to step 4.
  - **`ICY titles in the '<shape>' shape`**: data only; step 3.
  - **`ICY only`**: the titles fit no shape. Step 2a if they name an artist,
    else step 3 with `fetch_args = { music = false }`; either way, first
    spend a few minutes on step 4's search. A playlist feed is worth having.

## 2a. Titles in a shape of their own

`music = false` throws the artist away, so it is the last resort, not the
answer to "titles aren't Artist - Song". WFMU is the worked example: its
stream sends `"Song" by Artist on Show on WFMU`, which split on " - " gives
nothing (or worse, splits inside a quoted song), and which once got
`music = false`. Now `titles.wfmu` reads it. The steps:

1. **Capture.** Probe two or three times, a few minutes apart, so you see a
   show change and the filler as well as songs. Copy the titles exactly,
   with the date. WFMU's stream, 2026-10-03:
   `"At War With Satan" by Venom on Marty McSorley's show on WFMU` and
   `"Orgies - A Tool Of Witchcraft" by Louise Huebner with Louis and Bebe Barron on Marty McSorley's show on WFMU`
   (the second is why " - " can't be trusted: it's inside the song). Its
   tests add the other forms it sends: a bare `Show with Host` at a show
   change, and filler like `Your DJ speaks over "..." on <show> on WFMU`.
2. **Write the shape** in `stations/titles.py`: a pure `str -> dict` of
   NowPlaying fields (`artist`, `song`, `show`, `hosts`, `art_url`) that
   returns `{}` for anything not in its shape. Filler must give `{}`, never
   an artist: a wrong artist goes on to `discover` and `np -i`. Anchor it on
   what only this shape has (`" by "` after a leading quote, `on WFMU`, a
   `join.kcrw.com` suffix). Add it to `SHAPES`.
3. **Test it** in `tests/test_stations.py`, with the captured titles inline:
   - a parametrized parse test, like `test_wfmu_titles` / `test_kcrw_titles`,
     including the filler and a generic `A - B` giving `{}`;
   - add the name to the pinned set in
     `test_music_false_never_yields_an_artist_whatever_the_shape` (and a row
     there if it yields an artist);
   - a row in `test_shaped_stations_read_their_titles` once a catalog file uses it.
4. **Name it** in the catalog file: `fetch_args = { titles = "<shape>" }`.
5. **Detectable?** Add it to `DETECTABLE` in `stations/probe.py` (most specific
   first, with the mark only its titles carry: WFMU's is a trailing
   ` on WFMU`, since its parser alone reads any `"Song" by Artist`) only if
   no other station's titles could match it. A shape that
   splits any separator, or only means something for its own station
   (`kcrw`), stays out; `tests/test_station_finding.py` pins that.

## 3. Write the catalog file

Start from the probe's draft (`probe --key <key>` names it), then:

- **`key`**: lowercase letters and digits only. It becomes a shell word and
  a CLI argument. Use the call sign for broadcast stations (`kzsu`), and the
  channel id for SomaFM.
- **`name`**: what the station calls itself, short ("KZSU", "Secret Agent").
  It is also the default Spinitron call sign.
- **`blurb`**: **`City -- what it is`**, always with ` -- `. The dial splits
  it into the city and the description, and the test fails without it.
  Match the house style: "Stanford -- freeform college radio", "SomaFM, San
  Francisco -- lounge and spy jazz". Mention quirks the listener would care
  about ("names the show, not the song", "AAC stream", "96k mono").
- **`tags`**: only from `uv run twiddle stations tags`. Pick 1–3 that
  someone filtering would expect it under. **Don't invent a tag for one
  station.** If a real group is forming (say 3+ reggae stations), add it to
  `stations/tags.py` with a one-line meaning, and mention it to the user.
  Most tags describe the sound; a few name the network instead. **Every
  SomaFM channel also gets `somafm`**, on top of its 1–3 genre tags, so
  `--tag somafm` lists them all.
- **`logo`**: square, at least ~150px. The probe measures its candidates
  and the draft takes the first square one. Look at it: a favicon.ico or a
  wide banner is worse than no logo, which gets a monogram tile.
- **`order`**: leave it out (default 1000, listed by name after the
  curated ten) unless the user wants it on a number key.
- Keep URL explanations as TOML comments, like the ones in `kxsf.toml` and
  `nts.toml`.

## 4. No known platform: find the real now-playing source

Measure, don't guess, and record what you tried (with the date) in the
fetcher's docstring, including what failed. Check these in order:

1. **Spinitron under another name.** Try `spinitron.com/<CALL>` by hand, and
   grep the homepage for `spinitron`. Some stations use a different call
   sign there; that is just `fetch_args = { callsign = "..." }`.
2. **The site's own player.** Fetch the homepage and listen page
   (`curl -sL -A "Mozilla/5.0"`), then grep for
   `now.?playing|live-info|current|nowplaying|playlist|\.json|/api/`. A
   JS player usually polls a JSON URL; find it in the bundle, then replay
   it with `curl`.
3. **Known platforms without a fetcher yet**, which the probe flags:
   - Airtime Pro: now has a fetcher (`airtime`), so it is data only
   - Radio.co: `https://public.radio.co/stations/<id>/status`
   - RadioKing
   - Creek
   Write these as **platform** fetchers that take the station id in
   `fetch_args`, so the next station on that platform is data only.
4. **Playlist/RSS feeds** (like WFMU's): fine for the show or DJ, and
   rarely real-time. Say "(latest published)", as `wfmu.py` does.

A usable source is public, needs no key or login, and gives at least the
current song or show.

**Writing the fetcher** means creating `stations/fetchers/<name>.py`,
modelled on `somafm.py` (JSON) or `spinitron.py` (HTML):

- Split it into a **pure `parse_<name>(data_or_page) -> ...`** and the
  fetcher `def <name>(station, **fetch_args) -> NowPlaying`. Fetch with
  `net.get` / `net.get_json`, always through the module (tests patch
  `stations.net.get`).
- Fill what the source gives:
  - `artist`, `song` and `album` for music.
  - `show` and `hosts`.
  - `raw_title` for a show or segment name that is not a song.
  - `art_url` for a cover.
  - `recent`: dicts of time/artist/song/album/art_url, newest first. The
    dial shows these as "Just played".
  - `schedule` for program-schedule stations (see `kqed.py`).
  - MusicBrainz ids when the source has them.
- **Never put a show or segment name in `artist`.** The dial and `np -i`
  look `artist` up on MusicBrainz, and a wrong guess is worse than none.
- If the feed fails or has nothing, fall back to `icy(station)` when that
  gives something (see `spinitron.py`). Let real network errors raise:
  the dial shows "couldn't reach" and backs off.
- Register it in `FETCHERS` and the docstring table in
  `stations/fetchers/__init__.py`, and in the table above.

## 5. Tests

- Put the real response, **trimmed**, inline in `tests/test_stations.py`
  (see `SPINITRON_PAGE` and the KQED/NTS tests), with the date in a
  comment. Test the pure parser, plus one `STATIONS["<key>"].now_playing()`
  with `monkeypatch.setattr(stations.net, "get_json", ...)`.
- `tests/test_catalog.py` already checks every catalog file in strict mode:
  known tags, a known fetcher, the blurb shape, and a key that works as a
  shell word. You don't need a new test for a data-only station.
- Run `uv run pytest -q tests/test_catalog.py tests/test_stations.py tests/test_station_finding.py`,
  then the full suite. Update the test count in `CLAUDE.md`.

## 6. Check it for real, without touching a speaker

```bash
uv run twiddle np <key>                       # the fetcher, live
uv run twiddle dial list --tag <tag>          # it's in its tag, with a title
uv run twiddle tune <key> --dry-run           # resolves; writes nothing (needs the home LAN)
rtk proxy curl -sL -A "Linux UPnP/1.0 Sonos/85.0-64200 (ZPS27)" --max-time 8 -o <scratch>/a.mp3 <url>
ffmpeg -hide_banner -i <scratch>/a.mp3 -af volumedetect -f null - 2>&1 | grep mean_volume
```

Write to a file, and go through `rtk proxy`: the RTK hook rewrites a bare
`curl` inside a pipe and hands ffmpeg garbage ("FAILED: curl"). Send a
Sonos User-Agent, since that is who will really ask: servers filter on it,
in opposite directions. On 2026-09-27 SomaFM's ice1 gave plain curl an empty
reply, and regiocast (80s80s, 90s90s) gave `VLC/3.0` one; both served the
Sonos agent. An empty file is a refusal, not silence.

The last line matters. **Transport saying PLAYING is not evidence that
sound exists** (CLAUDE.md). A mean volume around -50 dB or lower means
silence.

**Don't `tune` a real speaker** unless the user asks. It writes to a speaker
someone may be listening to, or running a measurement on.

## 7. Tell the user

For each station, give one line: key, tags, fetcher, bitrate/codec, and
whether it will play on the Sonos (plain http). Also list what you
considered and rejected, and why ("blank ICY, no feed found", "https only").
Don't add a doc paragraph per station; the catalog file is the record.
