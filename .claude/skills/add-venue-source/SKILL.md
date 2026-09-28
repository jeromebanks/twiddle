---
name: add-venue-source
description: Add a venue to `twiddle scene`, or give a venue its own listings source with flyers and ticket links (a venue's calendar, ticketing widget or API). Use when asked to watch a new venue, add flyers/tickets for a venue, cover a venue The List misses, or fix a venue source that stopped parsing.
---

<!-- No dollar-digit sequences in this file: the skill loader substitutes them with arguments. -->

# Adding a venue, and a venue source with flyers

`scene` lists shows from **The List** (foopee.com) plus a venue's own pages.
The List gives bands, age and advance/door price for ~150 rooms. A venue's
own page adds **flyers, ticket links, and nights The List skips** (DJ nights,
karaoke), or covers a room The List lacks entirely (Yoshi's).

Read `docs/SCENE.md` → *Sources* and *Extending it* first. Existing sources are
the worked examples:

| File | Platform | Reusable for another venue on it? |
|---|---|---|
| `scene/sources/seetickets.py` | See Tickets WordPress plugin (HTML list view, `?list1page=N`) | **yes: one line in `VENUES`** (5 venues) |
| `scene/sources/ticketweb.py` | TicketWeb WordPress plugin (`tw-section`, one page, several themes) | **yes: one line in `VENUES`** (7 venues) |
| `scene/sources/gilman.py` | ShowSlinger widget (HTML, public *with* the URL token) | yes: change `WIDGET`/`VENUE` |
| `scene/sources/venuepilot.py` | VenuePilot (public GraphQL, `venuepilot.co/graphql`) | **yes: one line in `VENUES`** (account id from `venuepilotSettings`) |
| `scene/sources/squarespace.py` | Squarespace events collection, `?format=json` | **yes: one line in `VENUES`** (the collection's URL) |
| `scene/sources/simplecal.py` | WordPress "Simple Calendar" (a Google Calendar, server-rendered) | **yes: one line in `VENUES`** |
| `scene/sources/makeoutroom.py` | CalendarWiz (cookie session, month grid) + Weebly blog flyers | the CalendarWiz half, with a new `crd` |
| `scene/sources/grayarea.py` | venue's own WordPress cards | no, bespoke |
| `scene/sources/yoshis.py` | venue's own HTML calendar + detail pages | no, bespoke |
| `scene/sources/thelist.py` | The List | — |

See Tickets, TicketWeb, VenuePilot, Squarespace and Simple Calendar are
one class over a `VENUES` table each. If a second venue turns up on
ShowSlinger or CalendarWiz, do the same there rather than copying the
module. VenuePilot's API answers for any account
id; the Bay Area ones found on 2026-09-26 were Ivy Room (992) and
Ashkenaz (1228).

**Spotting the platform:** grep the venue's page for `seetickets-list-event-container`
(See Tickets), `tw-section` (TicketWeb), `venuepilotSettings` (VenuePilot,
`accountIds`), `showslinger.com/e1/` (ShowSlinger), `simcal-event` (Simple
Calendar), `calendarwiz.com` (CalendarWiz), `squarespace` (then try the
events page with `?format=json`). A new theme of a known
plugin can move fields around: match on the whole class token (TicketWeb's
`tw-event-date` vs `tw-event-date-time`), never on position.

## 1. Just watching a venue (no new source)

1. `uv run twiddle scene list --all-venues | grep -i <name>` for The List's
   spelling(s).
2. Add a `Venue(name, (match substrings,), icon_url)` to
   `DEFAULT_VENUES` in `scene/venues.py`. Pick the icon by hand and check it
   loads. A venue's favicon is often wrong, and no icon is fine: it gets a tile.
3. Add its `VenueInfo` (address, website, one-line description) to
   `scene/venue_info.py`. Check the address against the venue's own site.
4. Add its Instagram handle to `venue_info.INSTAGRAM`, taken from a link
   on the venue's own site. Then confirm `instagram.com/<handle>/` has an
   `og:image` and the right name, because a guessed handle can be someone
   else's (`@bottomofthehill` was). The app uses the profile picture when
   no logo is hand-picked, and a double-click opens the page.
5. Run `uv run pytest -q tests/test_scene_*.py`.

## 2. Finding a source: measure, don't guess

Record what you tried with the date in the module docstring, *including
what failed*. Earlier sessions wrote venues off
wrongly twice: the Stork Club was "parked", and Gilman had "no source"
because ShowSlinger's tokenless URL redirects to login.

Check these in order:

- **The venue's site.** Use `curl -sL -A "Mozilla/5.0"`. Look for a ticketing
  widget in the HTML: `grep -oiE 'venuepilot|seetickets|showslinger|eventbrite|dice\.fm|tixr|etix|ticketweb|wix'`.
- **A widget that renders with JS.** Download its JS bundle and look for
  the API it calls:
  - `/graphql` with a `query (...)` template literal;
  - `admin-ajax.php` with an `action=`;
  - a JSON URL.
  Replaying that call with `requests` is usually easier and sturdier than
  scraping HTML. VenuePilot needed only `accountIds` from `window.venuepilotSettings`.
- **HTML pagination.** Check whether a plain GET does the same job as the
  site's AJAX pager: `?list1page=2` did for See Tickets. Then no nonce is needed.
- **Pages linked from the homepage.** Try them too. Gilman's public
  ShowSlinger link was only there.
- **Aggregators.** Songkick, Bandsintown and RA block scripts (403 /
  Cloudflare). DoTheBay had nothing for Stay Gold.
- **Instagram.** Without a login it gives only the profile picture (see
  step 1.4). Every posts API answers 401 without a session, and flyers
  there are images with no structured date. Don't build a source on it.

A usable source must be public with no login or key, stable, and give at
least a date and a name per event. Everything else is a bonus.

## 3. Writing the source

Create `scene/sources/<name>.py` with a class that has `name = "<name>"` and
`fetch() -> list[Show]`. Register it in `sources/base._registry()`, **after**
`thelist`, because order decides whose facts win in a merge.

Fill `Show` (`scene/model.py`) like this:

| Field | Rule |
|---|---|
| `day`, `venue` | venue as a string `venues.find` matches ("Ivy Room, Albany") |
| `bands` | acts only, headliner first. **`[]` for a night with no bands** (DJ night, karaoke, happy hour) |
| `title` | the night's name when `bands` is empty. Strip dates ("Freakyoke 10/5" → "Freakyoke") |
| `age` | "21+", "a/a", "16+" |
| `price` | The List's style: advance/door with dollar signs ("12/15" dollars), or "free". Leave it `None` if the site's number isn't the ticket price (Gilman's flat five dollars) |
| `times` | The List's style: "7:30pm/8pm" (doors/show) |
| `notes` | presenter, subtitle, genre, "sold out", "record release" |
| `flyer` | the **largest** image URL (Gilman: drop `thumb_`; VenuePilot: `cover`) |
| `tickets` | the ticket page |
| `source`, `source_url` | the source name; the event's page (the ticket page will do) |

Rules the existing sources learned the hard way:

- **Present every piece of information the source gives.** Keep band-less
  nights with a `title`. Put genre, presenter and blurb facts into `notes`.
  Drop only private hires and non-shows (VIP add-ons).
- **Structured fields beat titles.** Titles get truncated (See Tickets cuts
  at ~60 characters) and carry dates.
- **Structured fields can be wrong too.** VenuePilot's `artists` was copied
  between events, so compare against the name before trusting it.
- **Split lineups conservatively.** "Make Do and Mend" and "Tori Roze and
  the Hot Mess" are single bands, and so is "Salt +". Split on commas and
  " + ". Split on "and" only if the site uses it that way.
- **Get the year from the date text** or, failing that, the weekday
  (`seetickets._day`). Never assume the current year.
- **Raise `SourceError`** for network failures, non-200 responses, zero
  shows, or a failed page partway through pagination. Don't return partial
  results, or the stale cache won't cover the gap. `fetch_all` also catches
  any other exception, but a clear `SourceError` message is better.
- **Cap pagination** (`MAX_PAGES`), and stop if the site serves page 1 again.
- **Keep bands out of lookups when there are none.** Band-less nights are
  skipped by lookups and genre scans automatically, because both iterate `bands`.

## 4. Merging with The List (automatic)

`model.dedupe` treats two listings as one show when they have the same day
and the same watched venue, and either:
- they share a band (compared loosely, ignoring a leading "The"), or
- each source lists exactly one show there that night.

The List's bands, age and price stay. The venue source fills `flyer`,
`tickets` and `title`, and `also` records that it contributed. You shouldn't
need to touch this. **Do verify it on live data:**

```bash
uv run python - <<'EOF'
from twiddle.scene.sources import fetch_all
shows, errors = fetch_all()
rows = [s for s in shows if "<venue>" in s.venue.lower()]
print(errors, len(rows), "merged", sum(1 for s in rows if s.also),
      "flyers", sum(1 for s in rows if s.flyer))
for s in rows: print(s.day, s.source, "+" if s.also else " ", "F" if s.flyer else "-", s.billing[:60], s.price)
EOF
```

Look for **duplicate rows on the same night**. Most are real: karaoke,
open mic or comedy beside the gig, or a pre-show. Some are missed merges,
often a joint billing; `_band_keys` already splits "A & B" for matching.
Some are not shows at all ("4-Day Passes"); drop those in the parser.
Also look for a band show wrongly absorbed into another. If you change
`dedupe`, re-run the old-vs-new row count over every venue. The last change
altered 0 of 1,179 rows outside the new venues.

## 5. Tests

Save the real response, trimmed, under `tests/fixtures/` (HTML: just the
events part; JSON: a few events chosen for their quirks). Name the date in
a comment. Add tests to `tests/test_scene_venue_sources.py` for:

- one fully populated show (bands, age, price, times, flyer, tickets);
- each quirk you handled (truncated title, band-less night, dropped
  private event, year boundary, bad structured field);
- pagination: stops at the end, and a failed page raises `SourceError`;
- a merge with a List-style `Show` (`_list(...)` helper), if the spellings
  differ.

Run `uv run pytest -q`. Update the test count in `CLAUDE.md`.

## 6. Docs

- `docs/SCENE.md` → *Sources*: add a table row and the measured quirks, and
  update the `sources/` tree under *Extending it*.
- `CLAUDE.md`: the `scene/` row in *Layout* and the `scene list` row in
  *Read-only vs writing* (sources are read-only; they never touch a speaker).
- Tell the user what was measured, what failed, and anything that
  overturns an earlier note (say so explicitly).

The app needs no change: the flyer replaces the venue icon under the venue
list, `f` opens it, and `c` / `scene list --links` include the ticket and
flyer links.
