"""The `scene` TUI: venues -> shows -> lineup -> band -> play.

Only presentation lives here. Shows, venues and what is known about each band
are *read* from the local dataset that `scene build` publishes (`dataset.py`);
this app never scrapes a venue or bulk-enriches artists. `r` runs the builder
in a subprocess and reloads. The one lookup that stays is per band: the band
on screen, when the dataset has no answer for it (`BandBook`, seeded from the
dataset), plus playback from a `players.Player` -- all injected, so the tests
drive this with fakes.

Every network call runs in a worker thread and reports back with
`call_from_thread`; nothing here blocks the event loop.

Pictures (band photos, venue icons, show flyers) are drawn with `textual-image`, as in
`dial`. Its import asks the terminal what it can do, which only works before
Textual takes the screen over, so `cli.py` imports this module before
`run()`. In Ghostty that is the kitty graphics protocol; elsewhere,
coloured half-blocks.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from urllib.parse import quote
from collections.abc import Callable
from datetime import date, timedelta

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option
from textual_image import widget as _images

from .. import here, spotify_ops
from ..dial import art
from . import bandcamp, cache, dataset, genre as genre_mod, instagram, pictures, profiles
from . import venues as venues_mod
from .bands import (
    CORROBORATED,
    NAME_ONLY,
    NONE,
    PENDING,
    UNCERTAIN,
    UNLOOKED,
    BandBook,
    BandProfile,
)
from .model import Show
from .venue_info import VenueInfo
from .players import Device, NeedsConfirmation, Player

BUILD_BUSY = 75             # `scene build`'s exit status when another build holds the lock
BUILD_CANCELLED = -1        # the app quit while a build ran: it was left to finish on its own

# `t` cycles in this order. The List runs ~5 months ahead, so the default
# is a month: a week hid most of what's there (the first complaint).
WINDOWS = ("month", "all", "tonight", "week", "2weeks")
WINDOW_DAYS = {"week": 7, "2weeks": 14, "month": 31}
WINDOW_LABEL = {"tonight": "Tonight", "week": "Next 7 days", "2weeks": "Next 2 weeks",
                "month": "Next 30 days", "all": "All upcoming"}
DEFAULT_WINDOW = "month"
THEMES = ("tokyo-night", "gruvbox", "nord", "catppuccin-mocha", "dracula",
          "rose-pine", "flexoki", "textual-dark", "solarized-light")
LINK_ORDER = ("bandcamp", "official", "wikipedia", "spotify", "discogs", "musicbrainz")
CONFIRM_WINDOW_S = 20
GENRE_WIDTH = 13
NARROW_BELOW = 160          # columns: stack the panes, or the lineup column starves
MAC_OUTPUT = "mac"          # dial.output.MAC, without importing dial's outputs here

# Shared with `dial`. `TWIDDLE_DIAL_IMAGES=halfcell` forces coloured
# half-blocks if a terminal claims the kitty protocol but draws it badly
# (tmux, often); `tgp` forces the kitty path.
ImageWidget = {"halfcell": _images.HalfcellImage, "tgp": _images.TGPImage}.get(
    os.environ.get("TWIDDLE_DIAL_IMAGES", "").lower(), _images.Image)

BADGE = {
    CORROBORATED: ("✓", "bold green", "on Spotify"),
    NAME_ONLY: ("~", "bold yellow", "name match only"),
    UNCERTAIN: ("?", "bold dark_orange", "which one?"),
    NONE: ("✗", "bold red", "not on Spotify"),
    PENDING: ("…", "dim", "looking"),
    UNLOOKED: ("·", "dim", "not looked up yet"),
}


def badge(p: BandProfile | None) -> Text:
    sym, style, _ = BADGE.get(p.confidence if p else PENDING, BADGE[PENDING])
    return Text(sym, style=style)


def run_builder(cancelled: Callable[[], bool] = lambda: False,
                argv: list[str] | None = None) -> tuple[int, str]:
    """`twiddle scene build` in a subprocess: (exit status, what to tell the user).

    A subprocess, not a call: the UI then holds no collection code, and a
    build cannot stall or crash the event loop. It runs in its own session,
    so quitting the app (or Ctrl-C in its terminal) does not kill it: when
    `cancelled()` turns true this returns `BUILD_CANCELLED` and the build
    finishes and publishes on its own. A first build takes minutes; `q` must
    not wait for it.
    """
    import tempfile
    argv = argv or [sys.executable, "-m", "twiddle", "scene", "build"]
    with tempfile.TemporaryFile("w+") as out:
        proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        while proc.poll() is None:
            if cancelled():
                return BUILD_CANCELLED, ""
            time.sleep(0.25)
        out.seek(0)
        lines = out.read().strip().splitlines()
    errors = [ln for ln in lines if ln.startswith("error:")]
    return proc.returncode, (errors[-1] if errors else lines[-1] if lines else "")


class ChoiceScreen(ModalScreen):
    """A titled list; dismisses with the chosen option's id, or None."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel"),
                Binding("j", "down", show=False), Binding("k", "up", show=False)]

    def __init__(self, title: str, options: list[tuple[str, str]], note: str = ""):
        super().__init__()
        self.title_text = title
        self.options = options
        self.note = note

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.title_text, id="dialog-title")
            if self.note:
                yield Static(self.note, id="dialog-note")
            yield OptionList(*[Option(label, id=oid) for oid, label in self.options],
                             id="dialog-list")

    def on_mount(self) -> None:
        self.query_one("#dialog-list", OptionList).focus()
        self.call_after_refresh(self._fit_list)

    def on_resize(self, event: events.Resize) -> None:
        # Start from the stylesheet's cap again, so a taller terminal gets it back.
        self.query_one("#dialog-list", OptionList).styles.max_height = None
        self.call_after_refresh(self._fit_list)

    def _fit_list(self) -> None:
        """Shrink the list to the room the dialog has left, so it scrolls.

        The list's height is auto, so it asks for every option; when the
        dialog hits its own max-height it clips the bottom instead, the list
        never learns it has to scroll, and `j` walks the highlight off screen.
        """
        dialog = self.query_one("#dialog")
        options = self.query_one("#dialog-list", OptionList)
        room = dialog.region.bottom - dialog.styles.gutter.bottom - options.region.y
        if options.region.height > room > 0:
            options.styles.max_height = room
            options.call_after_refresh(options.scroll_to_highlight)

    @on(OptionList.OptionSelected, "#dialog-list")
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_down(self) -> None:
        self.query_one("#dialog-list", OptionList).action_cursor_down()

    def action_up(self) -> None:
        self.query_one("#dialog-list", OptionList).action_cursor_up()


class VenueScreen(ModalScreen):
    """A venue's address, website, what it is, and Wikipedia's paragraph."""

    BINDINGS = [Binding("escape,q,i", "app.pop_screen", "Close"),
                Binding("w", "website", "Website"),
                Binding("g", "map", "Map"),
                Binding("n", "instagram", "Instagram")]

    def __init__(self, name: str, info: VenueInfo, listed: str = "",
                 wiki: dict | None = None):
        super().__init__()
        self.venue_name = name
        self.info = info
        self.wiki = wiki        # the dataset's stored summary, if it has one
        # An unwatched venue has no details, but The List's "Name, City" is
        # a perfectly good map search.
        self.map_url = info.map_url or (
            "https://www.google.com/maps/search/?api=1&query=" + quote(listed) if listed else "")

    def compose(self) -> ComposeResult:
        i = self.info
        with VerticalScroll(id="dialog"):
            yield Static(self.venue_name, id="dialog-title")
            t = Text()
            if i.address:
                t.append(i.address + "\n")
            if i.url:
                t.append(i.url + "\n", style="underline")
            if i.instagram:
                t.append(f"@{i.instagram} on Instagram\n", style="dim")
            if i.about:
                t.append("\n" + i.about + "\n")
            if not i:
                t.append("Not a watched venue, so no details are kept.\n", style="dim")
            keys = [k for k, ok in (("w website", i.url), ("g map", self.map_url),
                                    ("n instagram", i.instagram)) if ok]
            t.append("\n" + "   ".join(keys + ["esc close"]), style="dim")
            yield Static(t)
            t = Text()
            if self.wiki and self.wiki.get("extract"):
                t.append("\n" + self.wiki["extract"] + "\n")
                t.append("— Wikipedia", style="dim italic")
            yield Static(t, id="venue-wiki")

    def _open(self, url: str, what: str) -> None:
        if not url:
            self.app.notify(f"no {what} known for {self.venue_name}", severity="warning")
            return
        webbrowser.open(url)
        self.app.notify(url, title="opened", timeout=3)

    def action_website(self) -> None:
        self._open(self.info.url, "website")

    def action_map(self) -> None:
        self._open(self.map_url, "address")

    def action_instagram(self) -> None:
        self._open(self.info.instagram_url, "Instagram")


HELP = """\
[b]Moving[/b]
  tab / shift+tab   next / previous pane      j k ↑ ↓   move
  enter             drill in: show → lineup → tracks → play
[b]Playing[/b]
  p        play the highlighted track (or the band's first)
           Bandcamp tracks play on This Mac or the Roams, no Spotify needed
  space    pause / resume (stops a Bandcamp track)    n   next track
  d        choose a device     R   put the Roams back on what they were playing
[b]Bands[/b]
  m        choose which Spotify artist this band is (remembered)
  o        open the band's Bandcamp / site / Wikipedia in a browser
[b]Shows[/b]
  t        30 days / All / Tonight / 7 days / 2 weeks (remembered)
  /        filter by band, venue or genre ("metal", "reggae")
  a        your venues ↔ every venue listed (~150; remembered)
  r        rebuild the dataset (runs `twiddle scene build`); a scheduled build shows up by itself
[b]Other[/b]
  i        the venue: address, website (w), map (g), Instagram (n), what it is
           double-click the venue's picture for its Instagram, a flyer for the poster
  f        open the show's flyer full size (shown under the venues when there is one)
  c        copy the show (bands, date, venue, price, links) to send someone
  l        open where the show was listed (The List's page, Yoshi's event)
  v        visualize: the whole window becomes the music (←/→ picture,
           c colours, , and . delay). It shows the stream, not the speaker;
           Spotify only through the relay -- see docs/GUIDE.md → Visualizer
  ctrl+t   next colour theme               q   quit

[b]Genre[/b]  a best guess from the bands' own Bandcamp tags and MusicBrainz's;
blank when none of the lineup is tagged anywhere.

[b]Badges[/b]  [green]✓[/green] confirmed   [yellow]~[/yellow] same name, unconfirmed \
[dark_orange]?[/dark_orange] several candidates   [red]✗[/red] not on Spotify

Listings are from The List (foopee.com), a volunteer-run guide: it can lag
the venues, and its spelling of a band is a search term, not an identity.
Yoshi's, which The List doesn't carry, comes from yoshis.com's own calendar.
Flyers and ticket links come from the Stork Club's own calendar and Gilman's
ShowSlinger page; a night listed in both keeps The List's bands and prices.
"""


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static("scene — keys", id="dialog-title")
            yield Static(HELP)


class SceneApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "scene"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "play", "Play"),
        Binding("space", "toggle_pause", "Pause"),
        Binding("n", "next_track", "Next", show=False),
        Binding("d", "device", "Device"),
        Binding("R", "back_to_relay", "→Roams"),
        Binding("m", "match", "Match"),
        Binding("o", "open_link", "Open"),
        Binding("i", "venue_info", "Venue"),
        Binding("l", "open_listing", "Listing", show=False),
        Binding("f", "open_flyer", "Flyer"),
        Binding("c", "copy_show", "Copy"),
        Binding("t", "cycle_window", "When"),
        Binding("a", "toggle_all_venues", "All venues"),
        Binding("v", "visualize", "Visualize"),
        Binding("slash", "search", "Filter"),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("ctrl+t", "cycle_theme", "Theme", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("j", "cursor('down')", show=False),
        Binding("k", "cursor('up')", show=False),
        Binding("escape", "clear_search", show=False),
    ]

    def __init__(self, *, book_factory: Callable[[Callable[[BandProfile], None]], BandBook],
                 player: Player, watched: tuple[venues_mod.Venue, ...] | None = None,
                 venue: str | None = None, all_venues: bool = False,
                 dry_run: bool = False, bootstrap: bool = True,
                 load_dataset: Callable = dataset.load,
                 dataset_mtime: Callable = dataset.mtime,
                 run_build: Callable | None = None,
                 today: date | None = None, load_image: Callable = art.load,
                 band_photo: Callable = pictures.band_photo,
                 bandcamp_stream: Callable = bandcamp.stream_url,
                 instagram_picture: Callable = instagram.profile_picture,
                 outputs=None):
        super().__init__()
        self.load_dataset = load_dataset
        self.dataset_mtime = dataset_mtime
        self.run_build = run_build or run_builder
        self.bootstrap = bootstrap      # build once, unasked, when there is no dataset at all
        self.snapshot: dataset.Snapshot | None = None
        self._snap_mtime: float | None = None
        self._told_failures_at: float | None = None
        self._building = False
        self.player = player
        self.book = book_factory(self._from_worker_band)
        self.watched = watched if watched is not None else venues_mod.watched()
        self.all_venues = all_venues or bool(cache.get_state("all_venues", False))
        self.dry_run = dry_run
        self._today = today            # fixed only in tests; otherwise the real date
        self._rendered_day: date | None = None
        saved = cache.get_state("window")
        self.window = saved if saved in WINDOWS else DEFAULT_WINDOW
        self.filter_text = ""
        self.shows: list[Show] = []
        self.fetched_at: float | None = None
        self.venue_choice: str | None = None     # a `_venue_key`, or None = all
        self._initial_venue = venue
        self.current_show: Show | None = None
        self.current_band: str | None = None
        self.device: Device | None = None
        self.now: dict = {}
        self._pending: tuple | None = None       # (uris, offset, device, label, at)
        self._rows: dict[str, Show] = {}
        self.load_image = load_image
        self.instagram_picture = instagram_picture
        self.band_photo = band_photo
        self._photo_sig: tuple | None = None     # what the band photo on screen is of
        self._venue_shown: str | None = None     # the venue key its icon is of
        self._venue_images: dict[str, object] = {}
        self._flyer_shown = ""                   # the flyer URL on the venue card
        self._flyer_images: dict[str, object] = {}
        self.bandcamp_stream = bandcamp_stream
        self.outputs = outputs          # dial's Outputs: where Bandcamp tracks play
        self.viz_options: dict = {}     # VizScreen keywords; the tests' fakes
        self._scan_guesses: dict[str, genre_mod.Guess | None] = {}   # by band key
        self.bc_now: dict | None = None  # the Bandcamp track playing, if one is

    @property
    def today(self) -> date:
        """Recomputed each time: a tmux pane outlives midnight."""
        return self._today or date.today()

    # ---- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield OptionList(id="venues")
                with Vertical(id="venue-card"):
                    yield ImageWidget(None, id="venue-icon")
                    yield ImageWidget(None, id="flyer")
                    yield Static(id="venue-name")
                yield Static(id="window")
            with Vertical(id="middle"):
                yield Input(placeholder="filter bands or venues…  (esc clears)",
                            id="search")
                yield DataTable(id="shows", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield OptionList(id="lineup")
                with Horizontal(id="band"):
                    with Vertical(id="photo-col"):
                        yield ImageWidget(None, id="photo")
                        yield Static(id="photo-credit")
                    with VerticalScroll(id="profile-scroll"):
                        yield Static(id="profile")
                yield OptionList(id="tracks")
        yield Static(id="nowbar")
        yield Footer()

    def on_mount(self) -> None:
        theme = cache.get_state("theme", THEMES[0])
        self.theme = theme if theme in self.available_themes else THEMES[0]
        self.query_one("#venues").border_title = "Venues"
        self.query_one("#shows").border_title = "Shows"
        self.query_one("#lineup").border_title = "Lineup"
        self.query_one("#band").border_title = "Band"
        self.query_one("#tracks").border_title = "Tracks"
        self.query_one("#search").display = False
        table = self.query_one("#shows", DataTable)
        # Age, price and times sit under the lineup pane instead: as columns
        # they pushed the table wider than its pane at ordinary widths.
        for label, key, width in (("When", "when", 10), ("Venue", "venue", 17),
                                  ("Sounds like", "genre", GENRE_WIDTH), ("Lineup", "lineup", None)):
            table.add_column(label, key=key, width=width)
        saved = cache.get_state("device")
        if saved:
            self.device = Device(**saved)
        self._reload(first=True)
        self._render_nowbar()
        self.set_interval(15, self.poll_now)
        self.set_interval(300, self._tick)
        self.set_interval(20, self._watch_dataset)
        self.query_one("#shows").focus()

    # ---- the dataset ------------------------------------------------------

    def _reload(self, first: bool = False) -> None:
        """Read the published dataset. Cheap and offline: no collection happens here."""
        try:
            snap = self.load_dataset()
        except dataset.DatasetError as exc:
            self.notify(str(exc), title="dataset", severity="error", timeout=10)
            snap = None
            self._snap_mtime = self.dataset_mtime()     # say it once, not every 20s
        else:
            # The mtime of what was read, so a publish landing meanwhile is
            # noticed by the next check instead of being mistaken for this one.
            self._snap_mtime = snap.mtime if snap else self.dataset_mtime()
        if snap is None:
            self._update_subtitle()
            if first and self.bootstrap and not self.shows:
                self.build_dataset()
            return
        same_shows = self.snapshot is not None and snap.shows == self.shows
        before = self._profile_sig()
        self.snapshot = snap
        self._seed_bands(snap)
        failed = [f"{n}: {v.get('error')}" for n, v in snap.sources.items() if not v.get("ok", True)]
        if failed and snap.complete and snap.generated_at != self._told_failures_at:
            self._told_failures_at = snap.generated_at
            self.notify("\n".join(failed), title="last build: sources failed",
                        severity="warning", timeout=10)
        if same_shows:
            # A checkpoint of a running build: only band records changed. Leave
            # the cursor, focus and lineup where they are.
            self.fetched_at = snap.generated_at
            self._refresh_bands_in_place(before)
        else:
            self._set_shows(snap.shows, snap.generated_at)

    def _profile_sig(self) -> str | None:
        """Everything the band card and track list show, so a rebuild that
        changes any of it re-renders them (and one that changes none does not)."""
        p = self._profile()
        if p is None:
            return None
        return repr((sorted(p.status.items()), p.confidence, p.why,
                     p.info.to_dict() if p.info else None, p.lookup_candidates,
                     p.spotify_artist, p.spotify_candidates, p.tracks,
                     p.bandcamp, p.bc_tracks, p.alias,
                     profiles.guess_to_dict(self._scan_guesses.get(self._key(p.band)))))

    def _refresh_bands_in_place(self, before: str | None) -> None:
        self._refresh_genre_cells()
        s = self.current_show
        if s is not None:
            lineup = self.query_one("#lineup", OptionList)
            for idx, b in enumerate(s.bands):
                if idx < lineup.option_count:
                    lineup.replace_option_prompt_at_index(idx, self._lineup_label(b))
        if self.current_band:
            # The checkpoint may have swapped in a record for the band on
            # screen; wake whatever it lacks (song lists) as selecting it would.
            self.book.get(self.current_band, urgent=True)
        p = self._profile()
        if p is not None and self._profile_sig() != before:
            self._render_profile(p)
        self._update_subtitle()

    def _seed_bands(self, snap: dataset.Snapshot) -> None:
        """Hand the dataset's band records to the book, and remember the genre
        guesses for the table. Nothing is scheduled: only the band put on
        screen is ever looked up, and only for an answer the dataset lacks."""
        seeded = []
        self._scan_guesses = {}
        for key, rec in snap.bands.items():
            self._scan_guesses[key] = profiles.guess_from_dict(rec.get("genre"))
            seeded.append(profiles.from_record(rec, pinned=cache.pinned))
        self.book.seed(seeded)
        self.book.reseed = lambda band: (
            profiles.from_record(snap.bands[dataset.band_id(band)], pinned=cache.pinned)
            if dataset.band_id(band) in snap.bands else None)

    def _watch_dataset(self) -> None:
        """A scheduled (or `r`-started) build published: show it without a keypress."""
        if self.dataset_mtime() != self._snap_mtime:
            self._reload()

    def build_dataset(self) -> None:
        if self._building:
            self.notify("already building the dataset", timeout=3)
            return
        self._building = True
        self._status("building the dataset…")
        self._run_build()

    @work(thread=True, exclusive=True, group="build")
    def _run_build(self) -> None:
        from textual.worker import get_current_worker
        worker = get_current_worker()
        try:
            code, tail = self.run_build(lambda: worker.is_cancelled)
        except Exception as exc:        # the runner itself failed: say so, don't die
            code, tail = 1, str(exc)
        if code == BUILD_CANCELLED:
            return                      # quitting: nothing left to show it to
        self.call_from_thread(self._build_done, code, tail)

    def _build_done(self, code: int, tail: str) -> None:
        self._building = False
        self._status("")
        if code == BUILD_BUSY:
            self.notify("a build is already running (a scheduled one?) -- "
                        "new data will appear when it publishes", timeout=6)
        elif code != 0:
            self.notify(tail or f"`scene build` exited {code}", title="build failed",
                        severity="error", timeout=12)
        self._reload()

    def _set_shows(self, shows: list[Show], at: float | None) -> None:
        self.shows = shows
        self.fetched_at = at
        if self._initial_venue:
            v = venues_mod.resolve(self.watched, self._initial_venue)
            match = next((s.venue for s in shows if (v.matches(s.venue) if v else
                          self._initial_venue.lower() in s.venue.lower())), None)
            self.venue_choice = v.name if v else (self._venue_key(match) if match else None)
            self._initial_venue = None
        self._refresh_venues()
        self._refresh_shows()
        self._update_subtitle()

    def _tick(self) -> None:
        """Every few minutes: roll over at midnight, refresh the freshness line."""
        if self._rendered_day != self.today:
            self._refresh_venues()
            self._refresh_shows()
        self._update_subtitle()

    def _update_subtitle(self) -> None:
        if self.snapshot is None:
            note = "no dataset yet -- press r to build it" if not self._building else \
                "building the first dataset…"
        else:
            age = ""
            if self.fetched_at:
                mins = int((time.time() - self.fetched_at) / 60)
                age = "just now" if mins < 1 else (f"{mins}m ago" if mins < 120
                                                   else f"{mins // 60}h ago")
            note = f"dataset built {age or 'at an unknown time'}"
            if not self.snapshot.complete:
                note += " · enriching…"
            elif self.snapshot.stale():
                note += " · stale -- r rebuilds"
        self.sub_title = note + ("  [dry-run]" if self.dry_run else "")

    def _in_window(self, s: Show) -> bool:
        if s.day < self.today:
            return False
        if self.window == "tonight":
            return s.day == self.today
        if self.window in WINDOW_DAYS:
            return s.day < self.today + timedelta(days=WINDOW_DAYS[self.window])
        return True

    def _venue_key(self, listed: str) -> str:
        """One key per venue, however the source spells it.

        The List writes Gilman seven ways ("924 Gilman Street, Berkeley",
        "924 Gilman St., Berkeley", "924 Gilman, Berkeley" ...) and GAMH as
        both "S.F." and "S.f.", so grouping by the raw string splits one
        room into several rows. A watched venue groups by its own name.
        """
        v = venues_mod.find(self.watched, listed)
        return v.name if v else listed.strip().casefold()

    def _in_scope(self, s: Show) -> bool:
        return self.all_venues or venues_mod.find(self.watched, s.venue) is not None

    def _visible_shows(self) -> list[Show]:
        q = self.filter_text.lower()
        out = []
        for s in self.shows:
            if not (self._in_scope(s) and self._in_window(s)):
                continue
            if self.venue_choice and self._venue_key(s.venue) != self.venue_choice:
                continue
            if q and q not in s.venue.lower() and q not in s.billing.lower() \
                    and q not in self._show_genre(s).label():
                continue
            out.append(s)
        return out

    def _refresh_venues(self) -> None:
        ol = self.query_one("#venues", OptionList)
        counts: dict[str, int] = {}
        labels: dict[str, str] = {}
        for s in self.shows:
            if self._in_scope(s) and self._in_window(s):
                key = self._venue_key(s.venue)
                counts[key] = counts.get(key, 0) + 1
                labels.setdefault(key, venues_mod.display_name(self.watched, s.venue))
        if not self.all_venues:
            # Watched venues show even at zero, so "nothing at Eli's" is visible.
            for v in self.watched:
                counts.setdefault(v.name, 0)
                labels.setdefault(v.name, v.name)
        names = sorted(counts, key=lambda k: labels[k].lower())
        ol.clear_options()
        total = sum(counts.values())
        ol.add_option(Option(Text.assemble(("All", "bold"), f"  {total}"), id="*"))
        highlight = 0
        for i, name in enumerate(names, 1):
            n = counts[name]
            ol.add_option(Option(Text.assemble(self._venue_dot(name), labels[name],
                                               (f"  {n}", "dim" if not n else "")),
                                 id=name, disabled=not n))
            if name == self.venue_choice:
                highlight = i
        ol.highlighted = highlight
        self.query_one("#window", Static).update(
            Text.assemble((WINDOW_LABEL[self.window], "bold"), (" (t)", "dim"),
                          "\n", ("all venues" if self.all_venues else "your venues", "bold"),
                          (" (v)", "dim")))

    def _venue_color(self, key: str) -> tuple[int, int, int]:
        """A venue's own colour, for its dot, name and tile.

        Watched venues get hues spread evenly around the wheel: hashed ones
        measured three greens among six rooms. Others hash, as in `dial`.
        """
        names = [v.name for v in self.watched]
        if key in names:
            return art._rgb(names.index(key) / len(names), 0.6, 0.6)
        return art.color_for(key)

    def _venue_dot(self, key: str) -> Text:
        return Text("● ", style=art.hex_of(self._venue_color(key)))

    def _refresh_shows(self) -> None:
        table = self.query_one("#shows", DataTable)
        keep = self.current_show.key if self.current_show else None
        table.clear()
        self._rendered_day = self.today
        visible = self._visible_shows()
        self._rows = {}
        target_row = 0
        for i, s in enumerate(visible):
            rk = f"r{i}"
            self._rows[rk] = s
            tonight = s.day == self.today
            when = Text("Tonight" if tonight else s.day.strftime("%a %b %-d"),
                        style="bold cyan" if tonight else "")
            flags = " ★" if "recommended" in s.flags else ""
            venue = Text.assemble(self._venue_dot(self._venue_key(s.venue)),
                                  venues_mod.display_name(self.watched, s.venue)[:15])
            # A night with no bands billed (a DJ night, karaoke) goes by its name.
            billing = Text(s.billing + flags, no_wrap=True,
                           style="" if s.bands else "italic")
            table.add_row(when, venue, self._genre_cell(s), billing, key=rk)
            if s.key == keep:
                target_row = i
        self._shows_subtitle(len(visible))
        if visible:
            table.move_cursor(row=target_row)
            self._show_selected(visible[target_row])
        else:
            self._show_selected(None)

    # ---- genre -------------------------------------------------------------

    def _band_genre(self, band: str) -> genre_mod.Guess | None:
        """The fullest guess we have: the looked-up profile, else the scan's."""
        p = self.book.profiles.get(self._key(band))
        if p is not None and p.status.get("bandcamp") in ("done", None):
            g = p.genre()
            if g:
                return g
        return self._scan_guesses.get(self._key(band))

    def _show_genre(self, s: Show) -> genre_mod.Guess:
        return genre_mod.for_show([self._band_genre(b) for b in s.bands])

    def _genre_cell(self, s: Show) -> Text:
        g = self._show_genre(s)
        if not g:
            return Text("")
        style = genre_mod.FAMILIES[g.top[0]][1] + ("" if g.sure else " dim")
        label = g.label()
        if len(label) > GENRE_WIDTH:        # "electronic/latin" -> "electronic+"
            label = genre_mod.FAMILIES[g.top[0]][0] + "+" + ("" if g.sure else "?")
        return Text(label, style=style)

    def _shows_subtitle(self, n: int | None = None) -> None:
        table = self.query_one("#shows", DataTable)
        n = len(self._rows) if n is None else n
        table.border_subtitle = f"{n} shows"

    def _refresh_genre_cells(self) -> None:
        table = self.query_one("#shows", DataTable)
        for rk, s in self._rows.items():
            try:
                table.update_cell(rk, "genre", self._genre_cell(s))
            except Exception:
                pass    # the table was rebuilt under us; the next pass catches up
        self._shows_subtitle()

    # ---- selection -------------------------------------------------------

    @on(OptionList.OptionHighlighted, "#venues")
    def venue_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        choice = None if event.option.id == "*" else event.option.id
        if choice != self.venue_choice:
            self.venue_choice = choice
            self._refresh_shows()

    @on(OptionList.OptionSelected, "#venues")
    def venue_selected(self, _event) -> None:
        self.query_one("#shows").focus()

    @on(DataTable.RowHighlighted, "#shows")
    def show_highlighted(self, event: DataTable.RowHighlighted) -> None:
        s = self._rows.get(event.row_key.value) if event.row_key else None
        if s is not None and s is not self.current_show:
            self._show_selected(s)

    @on(DataTable.RowSelected, "#shows")
    def show_chosen(self, _event) -> None:
        self.query_one("#lineup").focus()

    def _show_selected(self, s: Show | None) -> None:
        self.current_show = s
        lineup = self.query_one("#lineup", OptionList)
        lineup.clear_options()
        self._show_venue(s)
        self._show_flyer(s)
        if s is None:
            lineup.border_subtitle = ""
            self._band_selected(None)
            return
        for i, b in enumerate(s.bands):
            lineup.add_option(Option(self._lineup_label(b), id=f"b{i}"))
        extra = [s.age, s.price, s.times, *s.notes, *s.flags]
        lineup.border_subtitle = escape(" · ".join(x for x in extra if x))
        if not s.bands:
            self._night_selected(s)
            return
        lineup.highlighted = 0
        self._band_selected(s.bands[0])

    def _lineup_label(self, band: str) -> Text:
        p = self.book.profiles.get(self._key(band))
        return Text.assemble(badge(p), " ", band)

    @staticmethod
    def _key(band: str) -> str:
        from ..lookup import norm
        return norm(band)

    @on(OptionList.OptionHighlighted, "#lineup")
    def band_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.current_show is None or event.option_index >= len(self.current_show.bands):
            return
        band = self.current_show.bands[event.option_index]
        if band != self.current_band:
            self._band_selected(band)

    @on(OptionList.OptionSelected, "#lineup")
    def band_chosen(self, _event) -> None:
        self.query_one("#tracks").focus()

    @on(OptionList.OptionSelected, "#tracks")
    def track_chosen(self, _event) -> None:
        self.action_play()

    def _band_selected(self, band: str | None) -> None:
        self.current_band = band
        if band is None:
            self.query_one("#profile", Static).update(
                Text("No shows here in this window. t changes the window, "
                     "v shows every venue.", style="dim"))
            self.query_one("#tracks", OptionList).clear_options()
            self._set_photo(None, None, "")
            return
        p = self.book.get(band, urgent=True)
        self._render_profile(p)

    def _night_selected(self, s: Show) -> None:
        """A night that bills no bands: its name, and nothing to look up."""
        self.current_band = None
        self.query_one("#tracks", OptionList).clear_options()
        self._set_photo(None, None, "")
        body = Text(s.title or "(untitled)", style="bold")
        for n in s.notes:
            body.append("\n" + n, style="dim")
        body.append("\n\nNo bands billed" + (" -- f opens the flyer." if s.flyer else "."),
                    style="dim italic")
        self.query_one("#profile", Static).update(body)

    # ---- band profile ----------------------------------------------------

    def _from_worker_band(self, p: BandProfile) -> None:
        """BandBook's callback: runs on a worker thread."""
        if not self.is_running:
            return
        try:
            self.call_from_thread(self._band_updated, p)
        except RuntimeError:
            pass    # app shutting down

    def _band_updated(self, p: BandProfile) -> None:
        lineup = self.query_one("#lineup", OptionList)
        if self.current_show and p.band in self.current_show.bands:
            for idx, b in enumerate(self.current_show.bands):
                if b == p.band and idx < lineup.option_count:
                    lineup.replace_option_prompt_at_index(idx, self._lineup_label(b))
        if self.current_band and self._key(self.current_band) == self._key(p.band):
            self._render_profile(p)
        if p.status.get("bandcamp") == "done":
            self._refresh_genre_cells()

    def _render_profile(self, p: BandProfile) -> None:
        sym, style, label = BADGE[p.confidence]
        t = Text()
        t.append(p.band, style="bold")
        t.append("   ")
        t.append(f"{sym} {label}", style=style)
        t.append("\n")
        if p.why:
            t.append(p.why + "\n", style="dim italic")
        g = p.genre() or self._scan_guesses.get(self._key(p.band))
        if g:
            fam = g.top[0]
            t.append("sounds like ", style="dim")
            t.append(g.label(), style=f"bold {genre_mod.FAMILIES[fam][1]}")
            if not g.sure:
                t.append(" (by name only -- may be another band)", style="dim")
            if g.tags:
                t.append("  " + ", ".join(g.tags[:6]), style="italic")
            t.append(f"  ({' + '.join(sorted(g.sources))})\n", style="dim")
        info = p.info
        lookup_state = p.status.get("lookup", "")
        bc = p.bandcamp
        if bc and bc.get("location") and not (info and info.origin):
            t.append(f"{bc['location']}  (Bandcamp)\n")
        if info:
            facts = [x for x in (info.origin, info.years, info.kind) if x]
            if facts:
                t.append(" · ".join(facts) + "\n", style="")
            if info.disambiguation:
                t.append(f"({info.disambiguation})\n", style="dim")
            if info.genres:
                t.append(", ".join(info.genres) + "\n", style="italic cyan")
            if info.description:
                t.append(info.description + "\n", style="dim")
            if info.summary:
                t.append("\n" + info.summary + "\n")
                if info.summary_source:
                    t.append(f"— {info.summary_source}\n", style="dim")
            if info.members:
                t.append("\nmembers: ", style="bold")
                t.append(", ".join(info.members) + "\n")
            if info.matched_by:
                t.append(f"\nidentified by {info.matched_by}\n", style="dim")
        elif lookup_state in ("pending", "running"):
            t.append("\n… looking them up (MusicBrainz, Wikipedia, Discogs, Bandcamp)\n",
                     style="dim")
        elif lookup_state.startswith("error"):
            t.append(f"\nlookup failed: {lookup_state[7:]}\n", style="red")
        elif p.lookup_candidates:
            names = ", ".join(c.get("name", "?") + (f" ({c['disambiguation']})"
                              if c.get("disambiguation") else "")
                              for c in p.lookup_candidates[:5])
            t.append(f"\nseveral artists share this name: {names}\n", style="dim")
        elif not bc:
            t.append("\nnot in MusicBrainz, Discogs or Bandcamp under this name\n",
                     style="dim")
        links = p.links()
        if links:
            t.append("\n")
            for k in LINK_ORDER:
                if k in links:
                    t.append(f"{k} ", style="bold")
                    t.append(links[k] + "\n", style=f"link {links[k]} underline dim")
        sp_state = p.status.get("spotify", "")
        if sp_state.startswith("error"):
            t.append(f"\nSpotify: {sp_state[7:]}\n", style="red")
        tr_state = p.status.get("tracks", "")
        if tr_state.startswith("error"):       # the identity is fine; the songs could not be fetched
            t.append(f"\nsong lists: {tr_state[7:]}\n", style="red")
        self.query_one("#profile", Static).update(t)
        self.query_one("#profile-scroll").scroll_home(animate=False)
        sig = pictures.signature(p)
        if sig != self._photo_sig:
            self._photo_sig = sig
            self.load_photo(p, sig)

        self._render_tracks(p)

    def _render_tracks(self, p: BandProfile) -> None:
        """Bandcamp's songs first, then Spotify's. Options are keyed
        `bc:<i>` / `sp:<i>`, so the cursor survives either list arriving
        late -- an index would jump, and play the wrong track."""
        tracks = self.query_one("#tracks", OptionList)
        keep = None
        # Only a cursor you moved is kept: otherwise the list's first song
        # (Bandcamp's, when it lands after Spotify's) is what `p` plays.
        if self.focused is tracks and tracks.highlighted is not None and tracks.option_count:
            keep = tracks.get_option_at_index(tracks.highlighted).id
        tracks.clear_options()
        both = bool(p.bc_tracks and p.tracks)
        if both:
            tracks.add_option(Option(Text("Bandcamp", style="bold dim"), disabled=True))
        for i, tr in enumerate(p.bc_tracks):
            mins, secs = divmod(int(tr.get("duration") or 0), 60)
            tracks.add_option(Option(Text.assemble(
                (f"{i + 1:>2}. ", "dim"), tr.get("title", "?"),
                (f"   {tr.get('album', '')}" + (f" ({tr['year']})" if tr.get("year") else "")
                 + (f"  {mins}:{secs:02d}" if mins or secs else ""), "dim")), id=f"bc:{i}"))
        if both:
            tracks.add_option(Option(Text("Spotify", style="bold dim"), disabled=True))
        for i, tr in enumerate(p.tracks):
            album = (tr.get("album") or {})
            year = (album.get("release_date") or "")[:4]
            tracks.add_option(Option(Text.assemble(
                (f"{i + 1:>2}. ", "dim"), tr.get("name", "?"),
                (f"   {album.get('name', '')}" + (f" ({year})" if year else ""), "dim")),
                id=f"sp:{i}"))
        ids = [f"bc:{i}" for i in range(len(p.bc_tracks))] + \
              [f"sp:{i}" for i in range(len(p.tracks))]
        if ids:
            tracks.highlighted = tracks.get_option_index(keep if keep in ids else ids[0])
        parts = [f"{len(p.bc_tracks)} on Bandcamp" if p.bc_tracks else "",
                 f"{len(p.tracks)} on Spotify" if p.tracks else ""]
        busy = p.status.get("bandcamp") in ("pending", "running") or p.confidence == PENDING \
            or p.status.get("tracks") in ("pending", "running")
        failed = p.status.get("tracks", "").startswith("error")
        tracks.border_subtitle = " · ".join(x for x in parts if x) or \
            ("searching…" if busy else "song lists failed -- see the band card" if failed else "")

    # ---- pictures ------------------------------------------------------------

    @work(thread=True, exclusive=True, group="photo")
    def load_photo(self, p: BandProfile, sig: tuple) -> None:
        """The band's photo, or their monogram -- only while they're on screen.

        `_band_updated` fires as each enricher lands and the cursor moves
        fast, so a slow Bandcamp search can finish after the band has
        changed: `_set_photo` drops an answer that is no longer wanted.
        """
        try:
            url, credit = self.band_photo(p)
        except Exception:
            url, credit = None, ""
        im = self.load_image(url) if url else None
        if url and im is None:
            credit = ""
        if im is None:
            im = art.monogram(p.info.name if p.info else p.band)
        self.call_from_thread(self._set_photo, sig, im, credit)

    def _set_photo(self, sig: tuple | None, im, credit: str) -> None:
        if sig is not None and sig != self._photo_sig:
            return
        if sig is None:
            self._photo_sig = None
        self.query_one("#photo", ImageWidget).image = im
        self.query_one("#photo-col").display = im is not None
        self.query_one("#photo-credit", Static).update(Text(credit, style="dim italic"))

    def _show_venue(self, s: Show | None) -> None:
        """The icon and name of the highlighted show's venue, under the list."""
        key = self._venue_key(s.venue) if s else None
        if key == self._venue_shown:
            return
        self._venue_shown = key
        name = venues_mod.display_name(self.watched, s.venue) if s else ""
        v = venues_mod.find(self.watched, s.venue) if s else None
        card = Text()
        if key:
            card.append(name, style=f"bold {art.hex_of(self._venue_color(key))}")
        if v and v.info.address:
            # The street and town; the state and ZIP don't help at a glance.
            card.append("\n" + v.info.address.split(", CA")[0], style="dim")
        self.query_one("#venue-name", Static).update(card)
        if key is None:
            self.query_one("#venue-icon", ImageWidget).image = None
            return
        cached = self._venue_images.get(key)
        if cached is not None:
            self.query_one("#venue-icon", ImageWidget).image = cached
        else:
            self.load_venue_icon(key, name, venues_mod.find(self.watched, s.venue))

    def _show_flyer(self, s: Show | None) -> None:
        """The show's flyer in place of the venue's icon, when it has one.

        Keyed by the flyer, not the venue: two Stork Club nights in a row
        have two flyers, although `_show_venue` has nothing to redraw.
        """
        url = s.flyer if s else ""
        if url == self._flyer_shown:
            return
        self._flyer_shown = url
        cached = self._flyer_images.get(url) if url else None
        self._set_flyer(url, cached)
        if url and cached is None:
            self.load_flyer(url)

    @work(thread=True, exclusive=True, group="flyer")
    def load_flyer(self, url: str) -> None:
        im = self.load_image(url)
        if im is not None and hasattr(im, "thumbnail"):
            # Kept for the session, which lasts days in tmux; a 24-column
            # card needs nothing like a full-size poster.
            im = im.copy()
            im.thumbnail((480, 480))
        self.call_from_thread(self._set_flyer, url, im)

    def _set_flyer(self, url: str, im) -> None:
        if im is not None:
            self._flyer_images[url] = im
        if url != self._flyer_shown:
            return
        flyer = self.query_one("#flyer", ImageWidget)
        flyer.image = im
        flyer.display = im is not None
        self.query_one("#venue-icon").display = im is None

    @work(thread=True, exclusive=True, group="venue-icon")
    def load_venue_icon(self, key: str, name: str, v: venues_mod.Venue | None) -> None:
        """Its hand-picked logo, else its Instagram picture, else a tile. A
        tile standing in for a picture that failed to load is not
        remembered, so a pane left open for days tries again (`art.load`
        and `instagram` space the retries)."""
        im = art.plate(self.load_image(v.icon)) if v and v.icon else None
        handle = v.info.instagram if v else ""
        if im is None and handle:
            try:
                im = self.instagram_picture(handle)
            except Exception:
                im = None
        final = im is not None or not (v and (v.icon or handle))
        if im is None:
            words = [w for w in name.replace("&", " ").split() if w[:1].isalnum()]
            im = art.tile("".join(w[0] for w in words[:3]).upper() or "?",
                          self._venue_color(key))
        self.call_from_thread(self._set_venue_icon, key, im, final)

    def _set_venue_icon(self, key: str, im, final: bool = True) -> None:
        if final:
            self._venue_images[key] = im
        if key == self._venue_shown:
            self.query_one("#venue-icon", ImageWidget).image = im

    def on_click(self, event: events.Click) -> None:
        """Double-click the venue card for its Instagram, a flyer for the poster."""
        if event.chain != 2:
            return
        w = event.widget
        while w is not None and w.id not in ("flyer", "venue-card"):
            w = w.parent
        if w is None:
            return
        if w.id == "flyer":
            self.action_open_flyer()
        else:
            self.action_venue_instagram()

    def action_venue_instagram(self) -> None:
        """The highlighted show's venue on Instagram."""
        s = self.current_show
        v = venues_mod.find(self.watched, s.venue) if s else None
        url = v.info.instagram_url if v else ""
        if not url:
            self.notify("no Instagram known for this venue", severity="warning")
            return
        webbrowser.open(url)
        self.notify(url, title="opened", timeout=3)

    # ---- actions: movement -------------------------------------------------

    def action_cursor(self, direction: str) -> None:
        w = self.focused
        fn = getattr(w, f"action_cursor_{direction}", None)
        if fn:
            fn()

    def action_cycle_window(self) -> None:
        self.window = WINDOWS[(WINDOWS.index(self.window) + 1) % len(WINDOWS)]
        cache.set_state("window", self.window)
        self._refresh_venues()
        self._refresh_shows()

    def action_toggle_all_venues(self) -> None:
        self.all_venues = not self.all_venues
        cache.set_state("all_venues", self.all_venues)
        self.venue_choice = None
        self._refresh_venues()
        self._refresh_shows()

    def viz_source(self):
        """Worker thread (the visualizer's): what to tap for what's playing."""
        from ..viz import source
        return source.for_scene(self.device, self.bc_now, self.now or {}, self.outputs)

    def action_visualize(self) -> None:
        from ..viz.screen import VizScreen     # numpy: only when asked for
        self.push_screen(VizScreen(self.viz_source, **self.viz_options))

    def action_search(self) -> None:
        box = self.query_one("#search", Input)
        box.display = True
        box.focus()

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self._refresh_shows()

    @on(Input.Submitted, "#search")
    def search_done(self, _event) -> None:
        self.query_one("#shows").focus()

    def action_clear_search(self) -> None:
        box = self.query_one("#search", Input)
        if box.display:
            box.value = ""
            box.display = False
            self.filter_text = ""
            self._refresh_shows()
            self.query_one("#shows").focus()

    def action_refresh(self) -> None:
        self.build_dataset()

    def action_cycle_theme(self) -> None:
        cur = self.theme if self.theme in THEMES else THEMES[-1]
        self.theme = THEMES[(THEMES.index(cur) + 1) % len(THEMES)]
        cache.set_state("theme", self.theme)
        self.notify(self.theme, title="theme", timeout=2)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # ---- actions: bands --------------------------------------------------

    def _profile(self) -> BandProfile | None:
        return self.book.profiles.get(self._key(self.current_band)) if self.current_band else None

    def action_venue_info(self) -> None:
        """The venue highlighted in the venue list, else the highlighted show's."""
        key = self.venue_choice if self.focused is self.query_one("#venues") else None
        s = self.current_show
        if key is None and s is not None:
            key = self._venue_key(s.venue)
        if key is None:
            self.notify("highlight a venue or a show first", severity="warning")
            return
        v = next((w for w in self.watched if w.name == key), None)
        listed = next((x.venue for x in self.shows if self._venue_key(x.venue) == key), key)
        name = v.name if v else venues_mod.display_name(self.watched, listed)
        rec = self.snapshot.venue(name) if self.snapshot else None
        self.push_screen(VenueScreen(name, v.info if v else VenueInfo(), listed,
                                     (rec or {}).get("wikipedia_summary")))

    def action_open_listing(self) -> None:
        """Where the highlighted show was listed: The List's page, Yoshi's event."""
        s = self.current_show
        if s is None or not s.source_url:
            self.notify("no listing link for this show", severity="warning")
            return
        webbrowser.open(s.source_url)
        self.notify(s.source_url, title="opened", timeout=3)

    def action_open_flyer(self) -> None:
        """The highlighted show's flyer, full size, in the browser."""
        s = self.current_show
        if s is None or not s.flyer:
            self.notify("no flyer for this show -- the Stork Club and Gilman "
                        "post theirs", severity="warning")
            return
        webbrowser.open(s.flyer)
        self.notify(s.flyer, title="opened", timeout=3)

    def action_copy_show(self) -> None:
        """The highlighted show as a message to send someone, on the clipboard."""
        s = self.current_show
        if s is None:
            self.notify("highlight a show first", severity="warning")
            return
        v = venues_mod.find(self.watched, s.venue)
        text = s.share_text(v.name if v else None, v.info.address if v else None)
        # pbcopy is exact on a Mac; the terminal escape (OSC 52) is the
        # fallback, and inside tmux needs `set -g set-clipboard on`.
        if shutil.which("pbcopy"):
            subprocess.run(["pbcopy"], input=text.encode(), check=False)
        else:
            self.copy_to_clipboard(text)
        self.notify(text, title="copied", timeout=6)

    def action_open_link(self) -> None:
        p = self._profile()
        links = p.links() if p else {}
        url = next((links[k] for k in LINK_ORDER if k in links), None)
        if not url:
            self.notify("no link known for this band yet", severity="warning")
            return
        webbrowser.open(url)
        self.notify(url, title="opened", timeout=3)

    def action_match(self) -> None:
        if self.current_band:
            self.match_band(self.current_band)

    @work(thread=True, exclusive=True, group="match")
    def match_band(self, band: str) -> None:
        spot = next((e for e in self.book.enrichers if e.name == "spotify"), None)
        if spot is None:
            return
        try:
            hits = spot.search(band)
        except Exception as exc:
            self.call_from_thread(self.notify, str(getattr(exc, "message", exc)),
                                  title="Spotify search", severity="error")
            return
        opts = [(a.get("id", ""), self._artist_line(a)) for a in hits if a.get("id")]
        opts += [("__none__", "✗  none of these -- not on Spotify"),
                 ("__auto__", "↺  forget my choice, match automatically")]
        self.call_from_thread(
            self.push_screen,
            ChoiceScreen(f"Which Spotify artist is “{band}”?", opts,
                         "Spotify hides popularity and genres for this app, so "
                         "check with o / a listen before trusting a name."),
            lambda choice: self._matched(band, choice, hits))

    @staticmethod
    def _artist_line(a: dict) -> str:
        extra = [", ".join(a.get("genres") or [])] if a.get("genres") else []
        return a.get("name", "?") + (f"   ({'; '.join(extra)})" if extra else "") + \
            f"   spotify:{a.get('id', '')[:8]}…"

    def _matched(self, band: str, choice: str | None, hits: list[dict]) -> None:
        if choice is None:
            return
        if choice == "__auto__":
            cache.unpin(band)
        elif choice == "__none__":
            cache.pin(band, None)
        else:
            name = next((a.get("name", "") for a in hits if a.get("id") == choice), "")
            cache.pin(band, choice, name)
        p = self.book.refresh(band)
        self._render_profile(p)
        self._band_updated(p)

    # ---- actions: playback ---------------------------------------------------

    def _status(self, msg: str, style: str = "") -> None:
        self._status_msg = (msg, style)
        self._render_nowbar()

    def _render_nowbar(self) -> None:
        msg, style = getattr(self, "_status_msg", ("", ""))
        np = self.now
        t = Text()
        bc = self.bc_now
        if bc:
            t.append("▶ ", style="bold green")
            t.append(bc["track"], style="bold")
            t.append(f" – {bc['band']}")
            t.append(f"   Bandcamp, on {bc['label']}", style="dim")
        elif np.get("track"):
            t.append("▶ " if np.get("playing") else "⏸ ", style="bold green")
            t.append(np["track"], style="bold")
            if np.get("artists"):
                t.append(" – " + ", ".join(np["artists"]))
            t.append(f"   on {np.get('device', '?')}", style="dim")
        else:
            t.append("nothing playing", style="dim")
        t.append("    device: ", style="dim")
        t.append(self.device.label if self.device else "none — press d", style="")
        if msg:
            t.append("\n")
            t.append(msg, style=style or "italic")
        self.query_one("#nowbar", Static).update(t)

    @work(thread=True, exclusive=True, group="now")
    def poll_now(self) -> None:
        bc = self.bc_now
        if bc and self.outputs is not None:
            # Ended, or something else took the output: the bar says so.
            try:
                st = self.outputs.get(bc["output"]).state()
                ours = bc["output"] == MAC_OUTPUT or "bcbits.com" in (st.other or "")
                if not (st.playing and ours) and self.bc_now is bc:
                    self.bc_now = None
                    if bc["output"] != MAC_OUTPUT:
                        self.call_from_thread(
                            self._status, f"{bc['label']} stopped after the Bandcamp track"
                            " -- R puts them back on the relay", "italic")
            except Exception:
                pass
        try:
            np = self.player.now()
        except Exception:
            return
        self.call_from_thread(self._set_now, np)

    def _set_now(self, np: dict) -> None:
        self.now = np
        self._render_nowbar()

    def action_device(self, then_play: bool = False) -> None:
        self.pick_device(then_play)

    @work(thread=True, exclusive=True, group="device")
    def pick_device(self, then_play: bool = False) -> None:
        try:
            devs = self.player.devices()
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
            return
        by_id = {f"d{i}": d for i, d in enumerate(devs)}
        opts = [(k, ("● " if d.active else "  ") + d.label) for k, d in by_id.items()]
        note = (f"{here.LABEL} plays here, no Spotify app needed. "
                if any(d.local for d in devs) else "")
        if any(d.relay for d in devs):
            note += ("The Roams (relay) option plays through the relay experiment. "
                     "Any other device takes Spotify away from it while it plays.")

        def done(choice: str | None) -> None:
            if choice is None:
                return
            self.device = by_id[choice]
            cache.set_state("device", {"id": self.device.id, "name": self.device.name,
                                       "relay": self.device.relay,
                                       "local": self.device.local})
            self._render_nowbar()
            if then_play:
                self.action_play()
            else:
                self._move_bandcamp()
        self.call_from_thread(self.push_screen,
                              ChoiceScreen("Play on which device?", opts, note), done)

    def _picked(self) -> tuple[str, int] | None:
        """("bc" | "sp", index): the highlighted track when the Tracks pane
        has focus, else the first listed (Bandcamp's, when there are any)."""
        p = self._profile()
        if not p or not (p.bc_tracks or p.tracks):
            return None
        tracks = self.query_one("#tracks", OptionList)
        oid = None
        if self.focused is tracks and tracks.highlighted is not None:
            oid = tracks.get_option_at_index(tracks.highlighted).id
        if not oid:
            return ("bc", 0) if p.bc_tracks else ("sp", 0)
        kind, i = oid.split(":")
        return kind, int(i)

    def action_play(self) -> None:
        pick = self._picked()
        if pick is None:
            p = self._profile()
            why = "still looking this band up" if p and p.busy() else \
                "no tracks on Bandcamp or Spotify for this band" + (
                    " -- m to choose the Spotify artist"
                    if p and p.confidence == UNCERTAIN else "")
            self.notify(why, severity="warning")
            return
        if self.device is None:
            self.action_device(then_play=True)
            return
        kind, off = pick
        if kind == "bc":
            self._play_bandcamp(self._profile(), off)
            return
        p = self._profile()
        uris = [t.get("uri", "") for t in p.tracks]
        label = f"{p.tracks[off].get('name', '?')} – {p.band}"
        confirmed = False
        if self._pending:
            p_uris, p_off, p_dev, _l, at = self._pending
            confirmed = (p_uris == uris and p_off == off and p_dev == self.device
                         and time.time() - at < CONFIRM_WINDOW_S)
        self._pending = None
        self.do_play(uris, off, self.device, label, confirmed)

    @work(thread=True, exclusive=True, group="play")
    def do_play(self, uris, off, device, label, confirmed) -> None:
        self.call_from_thread(self._status, f"starting {label}…")
        try:
            msg = self.player.play(uris, device, offset=off, confirmed=confirmed, label=label)
        except NeedsConfirmation as exc:
            self._pending = (uris, off, device, label, time.time())
            self.call_from_thread(self._status, f"⚠ {exc}", "bold yellow")
            return
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
            return
        bc = self.bc_now
        if bc and device.relay and bc["output"] == self._bandcamp_output(device):
            self.bc_now = None          # re-pointed at the relay: already replaced
        elif bc:
            self._stop_bandcamp(asked=False)    # or it plays on alongside Spotify
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self.poll_now)

    # ---- Bandcamp playback -------------------------------------------------

    def _bandcamp_output(self, device: Device) -> str | None:
        """Where a Bandcamp track can play for this Spotify device: the Mac's
        own speakers (ffplay) or the Roam itself. Not a phone: that's Spotify's."""
        if device.local:
            return MAC_OUTPUT
        if device.relay:
            return f"room:{getattr(self.player, 'room', 'roam')}"
        return None

    def _play_bandcamp(self, p: BandProfile, i: int, confirmed: bool | None = None) -> None:
        oid = self._bandcamp_output(self.device)
        if oid is None or self.outputs is None:
            self.notify(f"Bandcamp tracks play on {here.THIS} or the Roams, not "
                        f"{self.device.label} -- d to switch", severity="warning")
            return
        key = ("bc", p.band, i, oid)
        if confirmed is None:
            confirmed = bool(self._pending and self._pending[0] == key
                             and time.time() - self._pending[4] < CONFIRM_WINDOW_S)
        self._pending = None
        self.do_play_bandcamp(p, i, oid, confirmed)

    @work(thread=True, exclusive=True, group="play")
    def do_play_bandcamp(self, p: BandProfile, i: int, oid: str, confirmed: bool) -> None:
        tr = p.bc_tracks[i]
        label = f"{tr.get('title', '?')} – {p.band}"
        self.call_from_thread(self._status, f"starting {label} (Bandcamp)…")
        try:
            url = self.bandcamp_stream(tr)
        except Exception as exc:
            self.call_from_thread(self._error, spotify_ops.PlaybackError(
                f"couldn't get {tr.get('title')!r} from Bandcamp: {exc}"))
            return
        out = self.outputs.get(oid)
        try:
            # Through `outputs`: it stops a track we left on another output.
            from ..dial.output import Media     # not at the top: see MAC_OUTPUT
            msg = self.outputs.play(oid, Media.track(url, label, tr.get("art")),
                                    confirmed=confirmed, source="scene")
            self._pause_local_spotify()
        except NeedsConfirmation as exc:
            self._pending = (("bc", p.band, i, oid), None, None, label, time.time())
            self.call_from_thread(self._status, f"⚠ {exc}", "bold yellow")
            return
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
            return
        if not self.dry_run:
            self.bc_now = {"band": p.band, "profile": p, "index": i, "output": oid,
                           "track": tr.get("title", "?"), "label": out.label}
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self._render_nowbar)

    def _pause_local_spotify(self) -> None:
        """Worker thread only. Spotify on this Mac's own librespot would play
        on under a Bandcamp track wherever it went. On the relay it's the
        room's `play_url` that pauses it (after asking); on a phone, it's
        someone else's to stop."""
        local = getattr(self.player, "local", None)
        names = {(local.name or "").lower()} if local else set()
        if self.device and self.device.local:
            names.add((self.device.name or "").lower())
        try:
            np = self.player.now()      # fresh: the bar's is up to 15s old
        except spotify_ops.PlaybackError:
            return
        if np.get("playing") and np.get("device", "").lower() in names - {""}:
            try:
                self.player.pause()
            except spotify_ops.PlaybackError:
                pass

    def _move_bandcamp(self) -> None:
        """After `d`: a Bandcamp track follows the device, when it can play
        there -- started on the new output, then stopped on the old. On a
        device it can't (a phone), it plays on where it is; space stops it."""
        bc = self.bc_now
        oid = self._bandcamp_output(self.device) if self.device else None
        if bc and oid and oid != bc["output"]:
            self._play_bandcamp(bc["profile"], bc["index"], confirmed=False)

    def _stop_bandcamp(self, asked: bool = True) -> None:
        """Worker thread only. `asked` (space, R): stop the output outright.
        Otherwise it's a side effect of playing something else, so stop it
        only if it is still on what we put there -- not whatever replaced it."""
        now, self.bc_now = self.bc_now, None
        if now and self.outputs is not None:
            try:
                if asked:
                    self.outputs.stop(now["output"])
                else:
                    self.outputs.release(now["output"])
            except spotify_ops.PlaybackError:
                pass

    def _error(self, exc: spotify_ops.PlaybackError) -> None:
        self._status("")
        self.notify(escape(exc.message + (f"\n{exc.hint}" if exc.hint else "")),
                    title="playback", severity="error", timeout=10)

    def action_toggle_pause(self) -> None:
        if self.bc_now:
            self._transport("stop_bandcamp")
            return
        self._transport("resume" if not self.now.get("playing") else "pause")

    def action_next_track(self) -> None:
        if self.bc_now:
            now = self.bc_now
            p = now["profile"]
            if now["index"] + 1 < len(p.bc_tracks):
                self._play_bandcamp(p, now["index"] + 1, confirmed=True)
            else:
                self.notify("that was their last track here", timeout=3)
            return
        self._transport("next")

    @work(thread=True, group="transport")
    def _transport(self, verb: str) -> None:
        if verb == "stop_bandcamp":
            self._stop_bandcamp()
            self.call_from_thread(self._status, "■ stopped")
            return
        try:
            getattr(self.player, verb)()
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
            return
        time.sleep(0.4)
        self.call_from_thread(self.poll_now)

    @work(thread=True, exclusive=True, group="play")
    def action_back_to_relay(self) -> None:
        if self.bc_now and self.bc_now["output"] == MAC_OUTPUT:
            self._stop_bandcamp()
        try:
            # re-points the Roam too, if a Bandcamp track took it off the relay
            msg = self.player.back_to_relay()
            self.bc_now = None
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
            return
        self._pending = None
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self.poll_now)

    def on_resize(self, _event) -> None:
        # A narrow tmux pane: drop the venue column, stack shows over the band.
        self.screen.set_class(self.size.width < NARROW_BELOW, "-narrow")

    def on_unmount(self) -> None:
        self.book.close()
        if self.outputs is not None:
            self.outputs.close()    # ffplay stops with the app; a Sonos room plays on
        # Stops this Mac's librespot if the app started it: music played
        # through "This Mac (speakers)" ends when the app does.
        close = getattr(self.player, "close", None)
        if close:
            close()
