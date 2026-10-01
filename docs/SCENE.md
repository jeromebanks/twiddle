# scene — who's playing locally, and what do they sound like?

A terminal app for deciding whether a show is worth going to. It lists
upcoming shows at your venues (by default about forty East Bay and SF
rooms, from Thee Stork Club and 924 Gilman to Yoshi's, the Fox and the
Fillmore; see *Your venues*), tells you who each band is, and plays
their music from Spotify: out of this Mac's own speakers, on your phone, or
on the Roams through the relay. No Sonos is needed; see
[Running it without the Roams](#running-it-without-the-roams).

It is the first piece of a larger music discovery app, so it is built as
three swappable layers. See [Extending it](#extending-it).

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

Prerequisites (on a new Mac, [QUICKSTART.md](QUICKSTART.md) walks through them):
- `twiddle spotify auth` and `twiddle scene login` have been run once.
- **This Mac (speakers)** needs no Spotify app. The app runs its own small
  Spotify player (librespot) the first time you play to it, and stops it
  when you quit, so the music stops with the app.
- The **Roams (relay)** device works whether or not the relay is running;
  picking it starts one. It only appears on a Mac that has run the relay.

Without the TUI:

```bash
shows list                    # this week's shows at your venues, as text
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
| `r` | refresh listings now |
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
MusicBrainz users tag artists, and `scene/genre.py` folds those tags into
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

**Coverage, measured 2026-09-25:** 64 of 83 shows in the next 30 days at
your venues got a genre, 48 of them without a `?` (158 of 272 bands).

**How it gets there.** At launch, a background scan asks Bandcamp about
every band in the window, one request a second. The first run over a month
of listings took **4½ minutes**; the table fills in as it goes, and the
Shows border shows the progress. Answers are cached for two weeks (misses
for three days) in `bandcamp.json`, so later launches are instant. The scan
uses Bandcamp alone: no MusicBrainz and no Spotify, so it can't touch the
Spotify quota. A 429, or three failures in a row, pauses scene's Bandcamp
requests for 15 minutes rather than hammering an endpoint that `dial` and
`lookup` use too (their own Bandcamp calls don't go through this throttle).
Visiting a band refines its guess with MusicBrainz's tags and the Bandcamp
page MusicBrainz links.

**Only your venues, next 30 days, go to the network.** `a` (every venue)
and "All upcoming" would mean thousands of requests to an endpoint that
isn't ours, so those views show what the cache already knows. Any band you
open is looked up as usual.

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

Listings are cached for 6 hours. They show instantly at launch and refresh
in the background; `r` forces a refresh. A pane left open refreshes itself
and moves past midnight on its own.

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
fetched when you open the venue (`i`, or `shows venue <name>`) and cached
for 30 days. The card under the venue list shows the street.

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

Watching a venue means its next 30 days of bands are looked up in the
background. The 2026-09-25 additions brought in ~550 bands, about 9 minutes
of Bandcamp searches at its 1 request/second the first time, then cached
for 14 days. Trim the list in `scene.toml` if that's more than you want.

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
cached shows, so a brief outage at either site doesn't blank the other.
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

Everything lives in `~/.cache/twiddle/scene/`, and all of it is safe to
delete:

| File | Holds |
|---|---|
| `listings.json` | the last fetch (6h) |
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
src/twiddle/scene/
  model.py         Show -- the domain type; no UI, no network
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
  venues.py        watched venues + matching The List's spellings
  venue_info.py    each venue's address, website, description, Instagram; Wikipedia summaries
  instagram.py     a venue's Instagram profile picture, no login
  bands.py         WHO A BAND IS
                     Enricher protocol; LookupEnricher (MusicBrainz → Wikipedia/Discogs/Bandcamp),
                     SpotifyEnricher, BandcampEnricher; assess() grades identity;
                     BandBook runs enrichers off-thread
  bandcamp.py      Bandcamp search (throttled, cached, backs off), band choice, releases, stream URLs
  genre.py         tags -> genre families; a band's guess, a show's
  pictures.py      which photo a band gets; fetching + tiles are dial/art.py's
  players.py       WHERE AUDIO GOES
                     Player protocol; SpotifyConnectPlayer (incl. relay safety + handback)
  local.py         this Mac's speakers: the app's own librespot (rodio -> CoreAudio)
  cache.py         listings, pins, state
  app.py/.tcss     the Textual UI -- presentation only, everything injected
  cli.py           `scene`, `scene list`
```

**A new listings source** is one module in `sources/`. The
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
class in `bands.py`:
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

**A new screen** (recommendations, "bands like ones I've played", a
calendar export) can reuse `fetch_all`, `BandBook` and a `Player` without
touching `app.py`: none of them import Textual.

Playback logic shared with the CLI lives in `spotify_ops.py`. It raises
`PlaybackError(message, hint)` and never prints, so a UI can show the
errors. Tests: `tests/test_scene_*.py`. The UI tests drive the real app
with Textual's pilot over fakes, so they touch no network, Spotify account
or speaker.
