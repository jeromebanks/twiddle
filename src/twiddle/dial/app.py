"""The `dial` TUI: every station's now-playing, one key to tune.

Only presentation lives here. What the stations are playing comes from
`feed.StationFeed`, who the artist is from `enrich.Enricher`, pictures from
`art`, and sound from an `output.Output` -- all injected, so the tests
drive this with fakes.

Every network call runs in a worker thread and reports back with
`call_from_thread`; nothing here blocks the event loop.

Pictures are drawn with `textual-image`. Its import asks the terminal what
it can do, which is only possible before Textual takes the screen over --
so `cli.py` imports this module before `run()`. In Ghostty that is the kitty
graphics protocol (as `np` uses); elsewhere, coloured half-blocks.
`TWIDDLE_DIAL_IMAGES=halfcell` forces the half-blocks.
"""
from __future__ import annotations

import random
import time
import webbrowser
from collections.abc import Callable

from rich.markup import escape
from rich.style import Style
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static

from .. import here, play, spotify_ops, streaminfo
from ..scene import cache as scene_cache
from ..scene.app import THEMES, ChoiceScreen, ImageWidget
from ..scene.players import NeedsConfirmation
from ..stations import STATIONS, TAGS, NowPlaying, Station, tag_counts, with_tag
from . import art, output as output_mod, state
from .enrich import ArtistCard, Enricher, query_of
from .feed import StationFeed, StationState

CONFIRM_WINDOW_S = 20
VOLUME_SETTLE_S = 0.35      # an output with no `volume_interval_s` of its own
SLEEP_SETTLE_S = 1.2        # z z z to reach 45 min is one write, not three
SLEEP_PRESETS_MIN = (15, 30, 45, 60, 90, 120)
STATE_POLL_S = 12
EQ_FRAMES_S = 0.18
LINK_ORDER = ("bandcamp", "official", "wikipedia", "discogs", "musicbrainz")
BARS = "▁▂▃▄▅▆▇"


def city_of(s: Station) -> str:
    return s.blurb.split(" -- ", 1)[0] if " -- " in s.blurb else ""


def genre_of(s: Station) -> str:
    return s.blurb.split(" -- ", 1)[1] if " -- " in s.blurb else s.blurb


def song_line(np: NowPlaying | None) -> Text:
    if np is None:
        return Text("…", style="dim")
    if np.artist or np.song:
        return Text.assemble((np.artist or "?", "bold"), " — ", np.song or "")
    if np.raw_title:
        return Text(f"‹{np.raw_title}›", style="italic")
    return Text("(no title from the station right now)", style="dim italic")


def gauge(volume: int | None, muted: bool, color: str = "cyan", width: int = 20) -> Text:
    """The volume bar. It is also the mouse control: the icon toggles mute,
    and each of the bar's `width + 1` cells sets the volume it stands for."""
    if volume is None:
        return Text("♪ ?", style="dim")
    filled = round(volume / 100 * width)
    t = Text()
    t.append("✕ muted " if muted else "♪ ",
             style=Style.parse("bold red" if muted else "bold")
             + Style(meta={"@click": "app.mute"}))
    t.append(f"{volume:>3} ", style="dim" if muted else "bold")
    for cell in range(width + 1):
        char, style = (("━", "dim" if muted else f"bold {color}") if cell < filled
                       else ("●", "dim" if muted else "bold") if cell == filled
                       else ("─", "dim"))
        t.append(char, style=Style.parse(style) + Style(
            meta={"@click": f"app.set_volume({round(cell / width * 100)})"}))
    return t


class StationCard(Horizontal):
    """One station: logo, call sign, what's on, and the show."""

    def __init__(self, st: StationState, index: int | None):
        super().__init__(id=f"card-{st.key}")
        self.st = st
        self.index = index
        self.tuned = False
        self.playing = False

    def compose(self) -> ComposeResult:
        yield ImageWidget(None, classes="logo")
        yield Static(classes="text")

    def on_mount(self) -> None:
        self.render_text()

    def set_logo(self, im) -> None:
        for logo in self.query(".logo"):
            logo.image = im

    def render_text(self, frame: int = 0) -> None:
        # The feed and the output poll can report before this card's children
        # have mounted; on_mount renders it once they have.
        texts = self.query(".text")
        if not texts:
            return
        st, s = self.st, self.st.station
        line1 = Text(no_wrap=True, overflow="ellipsis")
        # 1..9, 0 jump to the first ten; the rest are reached by moving.
        line1.append(f"{(self.index + 1) % 10} " if self.index is not None and self.index < 10
                     else "  ", style="dim")
        line1.append(s.name, style="bold")
        # The meter sits right after the name, not at the end of the line: a
        # long name and city (Beat Blender, SomaFM, San Francisco) run past
        # the card's edge, and the ellipsis would trim the meter away.
        if self.tuned:
            if self.playing:
                rnd = random.Random(frame)
                line1.append(" " + "".join(rnd.choice(BARS) for _ in range(5)),
                             style="bold green")
            else:
                line1.append(" ⏸", style="bold green")
        city = city_of(s)
        if city:
            line1.append(f"  {city}", style="dim")
        line2 = song_line(st.np)
        line2.no_wrap, line2.overflow = True, "ellipsis"
        if st.error and st.np is None:
            wait = max(0, int(st.next_at - time.time()))
            line3 = Text(f"◌ couldn't reach · retry {wait}s", style="dim red")
        else:
            show = (st.np.show if st.np else None) or genre_of(s)
            hosts = ", ".join(st.np.hosts) if st.np and st.np.hosts else ""
            line3 = Text(show + (f" · {hosts}" if hosts else ""), style="dim italic")
            if st.error:
                line3 = Text("◌ stale · ", style="dim red") + line3
        line3.no_wrap, line3.overflow = True, "ellipsis"
        texts.first(Static).update(Text("\n").join([line1, line2, line3]))

    def on_click(self, event: events.Click) -> None:
        # One click previews, like j/k; a double click tunes, like enter.
        self.app.select(self.st.key)       # type: ignore[attr-defined]
        if event.chain == 2:
            self.app.action_tune()         # type: ignore[attr-defined]


class StationList(VerticalScroll, can_focus=True):
    pass


HELP = """\
[b]Stations[/b]
  j k ↑ ↓     browse (the right side previews without tuning)
  1 … 9, 0    jump to a station
  enter       tune the output to it (or double-click a station)
  t           show only one tag: electronic, news, sleep … (or all)
[b]Sound[/b]
  + / -       volume ±2          ] / \\[    volume ±5
  m           mute / unmute      s        stop
  D           disconnect: stop and clear the output -- a Sonos room shows
              nothing in its app, not a paused station (`s` leaves it there)
  z           sleep timer: 15, 30, 45, 60, 90, 120 min, off    Z  off
  (mouse)     click the volume bar to set it; click ♪ to mute
  d           choose the output: a Sonos room, this Mac, or paired
              Bluetooth headphones -- the
              station moves with you (the old one stops)
  R           put the room back on the relay (Spotify) and resume it
[b]Artist[/b]
  i           more / less about the artist
  o           open their Bandcamp / site / Wikipedia
[b]Visualize[/b]
  v           the whole window becomes the music: ←/→ changes the picture,
              c the colours, , and . slide it to meet a Sonos's buffering.
              It shows the stream, not the speaker -- see docs/GUIDE.md → Visualizer
[b]Other[/b]
  r  refresh now     ctrl+t  next colour theme     ?  help     q  quit
  ctrl+w      Twiddle waves hello again (the opening card; any key closes it)

Now-playing comes from each station: KEXP's, SomaFM's, Radio France's and
NTS's own feeds, Spinitron (the college stations), or the stream's own
title. Some only name the show (NTS, WMBR) or a news segment.
Artists are identified via MusicBrainz, Wikipedia and Discogs, as `np -i`.
"""


class DialHelp(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Static("dial — keys", id="dialog-title")
            yield Static(HELP)


class DialApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "dial"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("enter", "tune", "Tune"),
        Binding("plus,equals_sign", "volume(2)", "Vol+"),
        Binding("minus,underscore", "volume(-2)", "Vol−"),
        Binding("right_square_bracket", "volume(5)", show=False),
        Binding("left_square_bracket", "volume(-5)", show=False),
        Binding("m", "mute", "Mute"),
        Binding("z", "sleep", "Sleep"),
        Binding("Z", "sleep_off", show=False),
        Binding("d", "choose_output", "Output"),
        Binding("s", "stop", "Stop", show=False),
        Binding("D", "disconnect", "Disconnect", show=False),
        Binding("R", "back_to_relay", "→Relay"),
        Binding("t", "choose_tag", "Tags"),
        Binding("i", "expand", "More"),
        Binding("o", "open_link", "Open"),
        Binding("v", "visualize", "Visualize"),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("ctrl+t", "cycle_theme", "Theme", show=False),
        Binding("ctrl+w", "splash", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("j,down", "move(1)", show=False),
        Binding("k,up", "move(-1)", show=False),
        *[Binding(str(n), f"jump({(n - 1) % 10})", show=False) for n in range(10)],
    ]

    def __init__(self, *, outputs: output_mod.Outputs, output_id: str,
                 stations: list[Station] | None = None,
                 feed_factory: Callable[..., StationFeed] = StationFeed,
                 enricher_factory: Callable[..., Enricher] = Enricher,
                 load_image: Callable = art.load,
                 stream_info: Callable[[str], streaminfo.StreamInfo | None] = streaminfo.info,
                 dry_run: bool = False, start_feed: bool = True, tag: str | None = None,
                 splash: bool = True):
        super().__init__()
        # Every station has a card and a feed state; a tag only hides cards
        # (`listed`), so a tuned station outside the filter still updates.
        self.stations = stations or list(STATIONS.values())
        self.feed = feed_factory(self.stations, self._from_worker_station)
        tag = tag if tag is not None else state.get_state("tag")
        self.tag_filter = tag if tag in TAGS and with_tag(tag, self.stations) else None
        self.listed = with_tag(self.tag_filter, self.stations)
        self.feed.set_active(self._active_keys())
        self.enricher = enricher_factory(self._from_worker_card)
        self.outputs = outputs
        self.output: output_mod.Output = outputs.get(output_id)
        self.load_image = load_image
        self.stream_info = stream_info
        self.formats: dict[str, streaminfo.StreamInfo | None] = {}   # station key -> measured
        self.dry_run = dry_run
        self.start_feed = start_feed
        keys = [s.key for s in self.listed]
        saved = state.get_state("station")
        self.current = saved if saved in keys else keys[0]
        self.out_state = output_mod.OutputState()
        self.out_ready = False
        self.volume: int | None = None
        self.muted = False
        self._vol_timer = None
        self._vol_dirty = False
        self._vol_pushed_at = float("-inf")
        self._sleep_at: float | None = None      # monotonic time the timer fires
        self._sleep_pending: int | None = None   # minutes chosen, not yet sent (0 = off)
        self._sleep_timer = None
        self._pending: tuple | None = None      # (station key, output id, at)
        self._status_msg = ("", "")
        self._frame = 0
        self._shown: tuple | None = None        # (station, song identity) the art is for
        self.expanded = False
        self.logos: dict[str, object] = {}
        self.accent = art.color_for(self.current)
        self.viz_options: dict = {}             # VizScreen keywords; the tests' fakes
        self.splash = splash

    # ---- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with StationList(id="stations"):
                for s in self.stations:
                    yield StationCard(self.feed.states[s.key], self._index_of(s.key))
            with Vertical(id="right"):
                with Horizontal(id="hero"):
                    yield ImageWidget(None, id="cover")
                    yield Static(id="hero-text")
                with Horizontal(id="about"):
                    yield ImageWidget(None, id="photo")
                    with VerticalScroll(id="bio-scroll"):
                        yield Static(id="bio")
                with VerticalScroll(id="recent-scroll"):
                    yield Static(id="recent")
        yield Static(id="nowbar")
        yield Footer()

    def on_mount(self) -> None:
        theme = state.get_state("theme") or scene_cache.get_state("theme", THEMES[0])
        self.theme = theme if theme in self.available_themes else THEMES[0]
        self._apply_tag()
        self.query_one("#about").border_title = "About the artist"
        self.query_one("#recent-scroll").border_title = "Just played"
        self.query_one("#stations").focus()
        self.select(self.current)
        self._render_nowbar()
        self._update_subtitle()
        self.load_logos()
        if self.start_feed:
            self.feed.start()
        self.poll_output()
        self.set_interval(STATE_POLL_S, self.poll_output)
        self.set_interval(EQ_FRAMES_S, self._eq_tick)
        self.set_interval(5, self._refresh_cards)
        self.set_interval(1, self._sleep_tick)
        if self.splash:
            self.action_splash()        # last, so all of the above is already under way behind it

    def action_splash(self) -> None:
        """The opening card; also on ctrl+w, any time."""
        from .splash import SplashScreen
        if isinstance(self.screen, SplashScreen):
            return
        # On close, re-apply the layout class: a resize while it was up
        # went to the splash (`on_resize` styles `self.screen`).
        self.push_screen(SplashScreen(self._splash_status), lambda _: self.on_resize(None))

    def _splash_status(self) -> str:
        keys = self._active_keys()
        states = [st for k, st in self.feed.states.items() if keys is None or k in keys]
        heard = sum(st.np is not None or st.error is not None for st in states)
        if heard >= len(states):
            return f"all {len(states)} stations tuned in"
        return f"tuning in · {heard} of {len(states)} stations answered"

    def on_resize(self, _event) -> None:
        self.screen.set_class(self.size.width < 110, "-narrow")

    def card(self, key: str) -> StationCard:
        return self.query_one(f"#card-{key}", StationCard)

    # ---- the feed ----------------------------------------------------------

    def _from_worker_station(self, key: str) -> None:
        """StationFeed's callback: runs on a worker thread."""
        if not self.is_running:
            return
        try:
            self.call_from_thread(self._station_updated, key)
        except RuntimeError:
            pass    # shutting down

    def _station_updated(self, key: str) -> None:
        self.card(key).render_text(self._frame)
        if key == self.current:
            self._render_station()

    def _refresh_cards(self) -> None:
        """Retry countdowns tick even when nothing else changes."""
        try:
            for st in self.feed.states.values():
                if st.error:
                    self.card(st.key).render_text(self._frame)
        except NoMatches:
            pass    # a tick that lands during quit, after the cards are gone

    def _eq_tick(self) -> None:
        self._frame += 1
        tuned = self.out_state.tuned
        if tuned in self.feed.states and self.out_state.playing:
            try:
                self.card(tuned).render_text(self._frame)
            except NoMatches:
                pass    # as above

    # ---- selection -----------------------------------------------------------

    def select(self, key: str) -> None:
        old = self.current
        self.current = key
        for s in self.stations:
            self.card(s.key).set_class(s.key == key, "-highlight")
        card = self.card(key)
        self.query_one("#stations", StationList).scroll_to_widget(card, animate=False)
        self.feed.set_hot([key, self.out_state.tuned])
        if old != key:
            state.set_state("station", key)
        self.want_format(key)
        self._render_station()

    def action_move(self, delta: int) -> None:
        keys = [s.key for s in self.listed]
        if self.current not in keys:        # tuned elsewhere, then filtered away
            self.select(keys[0])
            return
        self.select(keys[(keys.index(self.current) + delta) % len(keys)])

    def action_jump(self, index: int) -> None:
        if index < len(self.listed):
            self.select(self.listed[index].key)

    # ---- tags ------------------------------------------------------------------

    def _index_of(self, key: str) -> int | None:
        return next((i for i, s in enumerate(self.listed) if s.key == key), None)

    def _active_keys(self) -> list[str] | None:
        return None if self.tag_filter is None else [s.key for s in self.listed]

    def _apply_tag(self) -> None:
        """Show only `self.listed`'s cards, numbered 1, 2, 3 … as listed."""
        for s in self.stations:
            c = self.card(s.key)
            c.index = self._index_of(s.key)
            c.display = c.index is not None
            c.render_text(self._frame)
        self.query_one("#stations").border_title = (
            f"Stations · {self.tag_filter} ({len(self.listed)})" if self.tag_filter else "Stations")

    def set_tag(self, tag: str | None) -> None:
        self.tag_filter = tag if tag in TAGS else None
        self.listed = with_tag(self.tag_filter, self.stations)
        state.set_state("tag", self.tag_filter)
        self.feed.set_active(self._active_keys())
        self._apply_tag()
        self.load_logos()
        keys = [s.key for s in self.listed]
        self.select(self.current if self.current in keys else keys[0])

    def action_choose_tag(self) -> None:
        counts = tag_counts(self.stations)
        opts = [("all", ("● " if self.tag_filter is None else "  ") + f"all ({len(self.stations)})")]
        opts += [(t, ("● " if t == self.tag_filter else "  ") + f"{t} ({n})  · {TAGS[t]}")
                 for t, n in counts.items()]

        def done(choice: str | None) -> None:
            if choice is not None and choice != (self.tag_filter or "all"):
                self.set_tag(None if choice == "all" else choice)
        self.push_screen(ChoiceScreen("Show which stations?", opts), done)

    # ---- the right side --------------------------------------------------------

    def _st(self) -> StationState:
        return self.feed.states[self.current]

    def _card_for(self, np: NowPlaying | None) -> ArtistCard | None:
        return self.enricher.get(np)

    def _render_station(self) -> None:
        st = self._st()
        np = st.np
        card = self._card_for(np)
        self._render_hero(st, card)
        self._render_about(np, card)
        self._render_recent(st)
        # `card is not None` counts: a lookup that finds no photo still turns
        # the empty artist box into a monogram, so its arrival must redraw.
        ident = (st.key, query_of(np) or (np.raw_title if np else None),
                 np.art_url if np else None, card is not None,
                 card.cover_url if card else None, card.photo_url if card else None)
        if ident != self._shown:
            self._shown = ident
            self.load_art(st.station, np, card)

    def _render_hero(self, st: StationState, card: ArtistCard | None) -> None:
        s, np = st.station, st.np
        hero = self.query_one("#hero")
        on_air = self.out_state.tuned == s.key
        hero.border_title = (f"▶ {s.name} · on {self.output.label}" if on_air
                             else f"{s.name} · preview — enter to tune")
        fmt = self.formats.get(s.key)
        hero.border_subtitle = genre_of(s) + (f" · {fmt.label()}" if fmt else "")
        accent = art.hex_of(self.accent)
        t = Text()
        city = city_of(s)
        t.append(("ON AIR" if on_air else "NOW ON") + f"  {s.name}", style=f"bold {accent}")
        if city:
            t.append(f" · {city}", style="dim")
        t.append("\n\n")
        if np and (np.artist or np.song):
            t.append((np.song or "?") + "\n", style="bold")
            t.append("━" * min(40, max(12, len(np.song or "") + 2)) + "\n", style=accent)
            t.append((np.artist or "") + "\n", style="bold")
            album = np.album or (card.info.album.title if card and card.info
                                 and card.info.album else None)
            year = card.info.album.year if card and card.info and card.info.album else None
            if album:
                t.append(album + (f" · {year}" if year else "") + "\n", style="italic")
        elif np and np.raw_title:
            t.append(f"‹{np.raw_title}›\n", style="bold italic")
            t.append("━" * 20 + "\n", style=accent)
            if np.note:
                t.append(np.note.strip("()") + "\n", style="dim")
        elif st.error:
            t.append("◌ couldn't reach the station\n", style="bold red")
            t.append(escape(st.error)[:120] + "\n", style="dim")
        elif np is not None:
            t.append("The station isn't naming what's on right now.\n", style="dim")
            t.append(s.blurb + "\n", style="dim italic")
        else:
            t.append("tuning in…\n", style="dim")
        if np and np.show:
            t.append("\n◷ ", style=accent)
            t.append(np.show, style="bold")
            if np.hosts:
                t.append(f"\n   with {', '.join(np.hosts)}", style="")
            t.append("\n")
        if st.since and np and (np.artist or np.song):
            mins = int((time.time() - st.since) / 60)
            t.append(f"\nheard {'just now' if mins < 1 else f'{mins} min ago'} · "
                     f"checked {time.strftime('%H:%M', time.localtime(st.fetched_at))}\n",
                     style="dim")
        genres = (card.info.genres[:4] if card and card.info else [])
        if genres:
            t.append("\n")
            for g in genres:
                t.append(f" {g} ", style=f"reverse {accent}")
                t.append(" ")
        self.query_one("#hero-text", Static).update(t)

    def _render_about(self, np: NowPlaying | None, card: ArtistCard | None) -> None:
        about = self.query_one("#about")
        about.set_class(self.expanded, "-expanded")
        t = Text()
        if np is None or not np.artist:
            about.border_title = "About the artist"
            t.append("\nNothing is naming an artist right now.", style="dim")
            if np and np.note and "talk" in np.note:
                t.append("\nThis is talk radio: the title is a segment, not a song.",
                         style="dim")
            elif np and np.raw_title:
                t.append("\nThis station names the show, not each song.", style="dim")
        elif card is None:
            about.border_title = f"About {np.artist}"
            t.append(f"\n… looking up {np.artist} (MusicBrainz, Wikipedia, Discogs)",
                     style="dim")
        elif card.info is None:
            about.border_title = f"About {np.artist}"
            if card.candidates:
                names = ", ".join(c.get("name", "?") + (f" ({c['disambiguation']})"
                                  if c.get("disambiguation") else "")
                                  for c in card.candidates[:4])
                t.append(f"\nSeveral artists share this name: {names}", style="dim")
            elif card.error:
                t.append(f"\nlookup failed: {card.error}", style="red")
            else:
                t.append("\nNot in MusicBrainz, Discogs or Bandcamp under this name.",
                         style="dim")
        else:
            info = card.info
            about.border_title = f"About {info.name}"
            facts = [x for x in (info.kind, info.origin, info.years) if x]
            t.append(info.name, style="bold")
            if facts:
                t.append("  " + " · ".join(facts), style="dim")
            t.append("\n")
            if info.description:
                t.append(info.description + "\n", style="italic")
            if info.summary:
                summary = info.summary
                if not self.expanded and len(summary) > 360:
                    summary = summary[:360].rsplit(" ", 1)[0] + "…  (i for more)"
                t.append("\n" + summary)
                if info.summary_source:
                    t.append(f"  — {info.summary_source}", style="dim")
                t.append("\n")
            if info.members:
                t.append("\nmembers  ", style="bold")
                t.append(" · ".join(info.members[:8]) + "\n")
            links = [k for k in LINK_ORDER if k in info.links]
            if links:
                t.append("\nlinks    ", style="bold")
                for k in links:
                    t.append(f"⌁ {k}", style=f"link {info.links[k]} underline")
                    t.append("  ")
                t.append("(o opens)", style="dim")
            if info.matched_by:
                t.append(f"\nidentified by {info.matched_by}", style="dim")
        self.query_one("#bio", Static).update(t)

    def _render_recent(self, st: StationState) -> None:
        box = self.query_one("#recent-scroll")
        if st.np and st.np.schedule:
            box.border_title = f"Today on {st.station.name}"
            self.query_one("#recent", Static).update(self._schedule_text(st.np.schedule))
            return
        box.border_title = f"Just played on {st.station.name}"
        rows = st.recent()
        t = Text()
        if not rows:
            t.append("Nothing yet: this station doesn't publish a playlist, so this "
                     "fills in as songs change while dial is open.", style="dim")
        for r in rows[:15]:
            t.append(f"{r.get('time', ''):>8}  ", style="dim")
            t.append("▪ ", style=art.hex_of(self.accent))
            if r.get("artist"):
                t.append(r["artist"], style="bold")
                t.append(" — ")
            t.append(r.get("song", "") or "")
            if r.get("album") and r.get("album") != r.get("song"):
                t.append(f"   {r['album']}", style="dim italic")
            t.append("\n")
        self.query_one("#recent", Static).update(t)

    def _schedule_text(self, slots: list[dict]) -> Text:
        """A program station's day: what aired dimmed, what's on marked,
        what's next in full."""
        accent = art.hex_of(self.accent)
        now_i = next((i for i, r in enumerate(slots) if r.get("on_now")), -1)
        t = Text()
        for i, r in enumerate(slots):
            past, now = i < now_i, i == now_i
            t.append(f"{r.get('time', ''):>8}  ", style="bold" if now else "dim")
            t.append("▶ " if now else "▪ ", style=accent if not past else "dim")
            t.append(r.get("show", ""), style="bold " + accent if now else
                     ("dim" if past else "bold"))
            if r.get("episode"):
                t.append(f" — {r['episode']}", style="dim" if past else "")
            if r.get("host"):
                t.append(f"   with {r['host']}", style="dim italic")
            t.append("\n")
        return t

    # ---- stream format ---------------------------------------------------------

    def want_format(self, key: str | None) -> None:
        """Measure a station's stream once (ffprobe, cached a week on disk)."""
        if key in self.feed.states and key not in self.formats:
            self.formats[key] = None            # in flight: don't probe twice
            self.probe_format(self.feed.states[key].station)

    @work(thread=True, group="formats")
    def probe_format(self, s: Station) -> None:
        found = self.stream_info(s.url)
        if self.is_running:
            self.call_from_thread(self._format_landed, s.key, found)

    def _format_landed(self, key: str, found) -> None:
        self.formats[key] = found
        if key == self.current:
            self._render_station()
        self._render_nowbar()

    # ---- pictures --------------------------------------------------------------

    @work(thread=True, group="logos")
    def load_logos(self) -> None:
        # Visible stations only: a long catalog behind a tag need not fetch
        # every logo at launch. Choosing another tag loads that tag's.
        for s in list(self.listed):
            if s.key in self.logos:
                continue
            im = art.plate(self.load_image(s.logo)) or art.tile(s.name, art.color_for(s.key))
            self.logos[s.key] = im
            self.call_from_thread(self.card(s.key).set_logo, im)

    @work(thread=True, exclusive=True, group="art")
    def load_art(self, s: Station, np: NowPlaying | None, card: ArtistCard | None) -> None:
        cover = None
        for url in (np.art_url if np else None, card.cover_url if card else None):
            cover = self.load_image(url)
            if cover is not None:
                break
        is_cover = cover is not None
        if cover is None:
            cover = self.logos.get(s.key) or art.plate(self.load_image(s.logo)) \
                or art.tile(s.name, art.color_for(s.key))
        photo = None
        if np and np.artist:
            photo = self.load_image(card.photo_url) if card and card.photo_url else None
            if photo is None and card is not None:
                photo = art.monogram(card.info.name if card.info else np.artist)
        accent = art.dominant(cover, s.key) if is_cover else \
            art.dominant(self.logos.get(s.key), s.key)
        self.call_from_thread(self._show_art, s.key, cover, photo, accent)

    def _show_art(self, key: str, cover, photo, accent) -> None:
        if key != self.current:
            return
        try:
            self.query_one("#cover", ImageWidget).image = cover
        except NoMatches:
            return  # art that lands during quit, after the screen is gone
        ph = self.query_one("#photo", ImageWidget)
        ph.image = photo
        ph.display = photo is not None
        if accent != self.accent:
            self.accent = accent
            hx = art.hex_of(accent)
            self.query_one("#hero").styles.border = ("round", hx)
            self.query_one("#hero").styles.border_title_color = hx
            st = self._st()
            self._render_hero(st, self._card_for(st.np))
            self._render_recent(st)

    def _from_worker_card(self, card: ArtistCard) -> None:
        if not self.is_running:
            return
        try:
            self.call_from_thread(self._card_ready, card)
        except RuntimeError:
            pass

    def _card_ready(self, card: ArtistCard) -> None:
        if card.query == query_of(self._st().np):
            self._render_station()

    # ---- the output ---------------------------------------------------------------

    def _status(self, msg: str, style: str = "") -> None:
        self._status_msg = (msg, style)
        self._render_nowbar()

    def sleep_left(self) -> int | None:
        if self._sleep_at is None:
            return None
        left = int(self._sleep_at - time.monotonic())
        return left if left > 0 else None

    def _update_subtitle(self) -> None:
        sleep = ""
        if self._sleep_pending is not None:
            sleep = (f"  ☾ sleep in {self._sleep_pending} min…" if self._sleep_pending
                     else "  ☾ sleep timer off…")
        elif (left := self.sleep_left()) is not None:
            sleep = f"  ☾ sleep in {play.fmt_left(left)}"
        self.sub_title = (f"on {self.output.label}" + sleep
                          + ("  [dry-run]" if self.dry_run else ""))

    def _sleep_tick(self) -> None:
        if self._sleep_at is not None:
            if self.sleep_left() is None:
                self._sleep_at = None
            self._update_subtitle()

    def _render_nowbar(self) -> None:
        t = Text()
        os_ = self.out_state
        if not self.out_ready:
            t.append(f"finding {self.output.label}…", style="dim")
        elif os_.tuned in self.feed.states:
            st = self.feed.states[os_.tuned]
            t.append("▶ " if os_.playing else "⏸ ", style="bold green")
            t.append(st.station.name, style="bold")
            t.append("  ")
            t.append_text(song_line(st.np))
        elif os_.tuned == output_mod.RELAY:
            t.append("♫ ", style="bold green")
            t.append("Spotify via the relay", style="bold")
            t.append("  (the experiment)", style="dim")
        else:
            t.append("■ " + (os_.other[:50] if os_.other else "no station"), style="dim")
        t.append(f"    on {self.output.label}    ", style="dim")
        t.append_text(gauge(self.volume, self.muted, art.hex_of(self.accent)))
        fmt = self.formats.get(os_.tuned) if self.out_ready else None
        if fmt:
            t.append(f"   {fmt.label()}", style="yellow" if fmt.low else "dim")
        msg, style = self._status_msg
        if msg:
            t.append("\n")
            t.append(msg, style=style or "italic")
        # The base screen, not `self.screen`: the visualizer sits above it and
        # still nudges volume, which redraws this bar.
        self.screen_stack[0].query_one("#nowbar", Static).update(t)

    @work(thread=True, exclusive=True, group="output-state")
    def poll_output(self) -> None:
        out = self.output
        try:
            st = out.state()
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._output_error, out, exc)
            return
        self.call_from_thread(self._set_output_state, out, st)

    def _output_error(self, out, exc) -> None:
        if out is self.output:
            self.out_ready = True
            self._status(f"{exc.message}" + (f" — {exc.hint}" if exc.hint else ""),
                         "bold red")

    def _set_output_state(self, out, st: output_mod.OutputState) -> None:
        if out is not self.output:
            return
        first = not self.out_ready
        self.out_ready = True
        old = self.out_state.tuned
        self.out_state = st
        if not self._vol_dirty:
            self.volume, self.muted = st.volume, st.muted
        if self._sleep_pending is None and self._sleep_timer is None:
            self._sleep_at = time.monotonic() + st.sleep_s if st.sleep_s else None
            self._update_subtitle()
        if first:
            self._update_subtitle()
            if self._status_msg[1] == "bold red":
                self._status("")
        for key in {old, st.tuned}:
            if key in self.feed.states:
                c = self.card(key)
                c.tuned, c.playing = key == st.tuned, st.playing
                c.set_class(c.tuned, "-tuned")
                c.render_text(self._frame)
        if old != st.tuned:
            self.feed.set_hot([self.current, st.tuned])
            self._render_station()
        self.want_format(st.tuned)
        self._render_nowbar()

    def action_tune(self) -> None:
        s = STATIONS.get(self.current) or next(x for x in self.stations
                                               if x.key == self.current)
        confirmed = False
        if self._pending:
            key, oid, at = self._pending
            confirmed = (key == s.key and oid == self.output.id
                         and time.time() - at < CONFIRM_WINDOW_S)
        self._pending = None
        self.do_tune(s, confirmed)

    @work(thread=True, exclusive=True, group="tune")
    def do_tune(self, s: Station, confirmed: bool) -> None:
        out = self.output
        self.call_from_thread(self._status, f"tuning {out.label} to {s.name}…")
        self._play_on(out, output_mod.Media.of(s), confirmed)

    def _play_on(self, out, media: output_mod.Media, confirmed: bool) -> None:
        """Worker thread only. Through `outputs`, never `out` directly: that
        is what stops the stream on the output we left."""
        try:
            msg = self.outputs.play(out.id, media, confirmed=confirmed)
        except NeedsConfirmation as exc:
            self._pending = (media.station, out.id, time.time())
            elsewhere = [self.outputs.get(o).label for o in self.outputs.owned() if o != out.id]
            note = f"  (still playing on {', '.join(elsewhere)})" if elsewhere else ""
            self.call_from_thread(self._status, f"⚠ {exc}{note}", "bold yellow")
            self.call_from_thread(self.notify, str(exc), title="press enter again",
                                  severity="warning", timeout=CONFIRM_WINDOW_S)
            return
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
            return
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self.poll_output)

    @work(thread=True, exclusive=True, group="tune")
    def move_stream(self, old_id: str, out) -> None:
        """Carry the station playing on `old_id` over to `out`: start it
        there, then stop it on the old one. Nothing is written when the old
        output was on the relay or idle. If `out` needs a second press (it's
        on the relay), the old one plays on until enter confirms."""
        self.call_from_thread(self._status, f"moving to {out.label}…")
        media = self.outputs.movable(old_id)
        if media is None:
            self.call_from_thread(self._status, "")
            return
        # enter confirms a tune of the *highlighted* station: make it this one.
        self.call_from_thread(self.select, media.station)
        self._play_on(out, media, False)

    def _error(self, exc: spotify_ops.PlaybackError) -> None:
        self._status("")
        self.notify(escape(exc.message + (f"\n{exc.hint}" if exc.hint else "")),
                    title="dial", severity="error", timeout=10)

    def action_volume(self, delta: int) -> None:
        if self.volume is None:
            self.notify("volume isn't known yet", severity="warning", timeout=3)
            return
        self.action_set_volume(self.volume + delta)

    def action_set_volume(self, volume: int) -> None:
        """Keys and clicks on the gauge both land here. The first press is
        written at once; a held key is then written every
        `volume_interval_s` of the output (a journalled Sonos write is paced,
        a process's is free) and once more when it stops, so the sound follows
        the bar instead of waiting for the key to be let go."""
        if self.volume is None:
            return
        self.volume = max(0, min(100, volume))
        self._vol_dirty = True
        self._render_nowbar()
        gap = getattr(self.output, "volume_interval_s", VOLUME_SETTLE_S)
        if self._vol_timer is not None:
            return                  # the trailing write will carry the latest
        wait = self._vol_pushed_at + gap - time.monotonic()
        if wait <= 0:
            self._push_volume()
        else:
            self._vol_timer = self.set_timer(wait, self._push_volume)

    def _push_volume(self) -> None:
        self._vol_timer = None
        self._vol_pushed_at = time.monotonic()
        self.push_volume(self.output, self.volume)

    @work(thread=True, exclusive=True, group="volume")
    def push_volume(self, out, volume: int) -> None:
        try:
            out.set_volume(volume)
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
        except Exception as exc:
            self.call_from_thread(self._error, spotify_ops.PlaybackError(
                f"volume: {type(exc).__name__}: {exc}"))
        self.call_from_thread(self._volume_landed, volume)

    def _volume_landed(self, volume: int) -> None:
        """Only the latest write clears `_vol_dirty`: until then a state poll
        must not snap the bar back to an older level."""
        if self._vol_timer is None and volume == self.volume:
            self._vol_dirty = False

    def action_mute(self) -> None:
        self.muted = not self.muted
        self._render_nowbar()
        self.push_mute(self.output, self.muted)

    @work(thread=True, exclusive=True, group="mute")
    def push_mute(self, out, muted: bool) -> None:
        try:
            out.set_mute(muted)
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
        except Exception as exc:
            self.call_from_thread(self._error, spotify_ops.PlaybackError(
                f"mute: {type(exc).__name__}: {exc}"))

    def action_sleep(self) -> None:
        """Step to the next preset above what is left (or chosen): with 29:40
        left that is 45, so a press always buys time; past 120, off."""
        if self._sleep_pending is not None:
            base = self._sleep_pending
        else:
            left = self.sleep_left()
            base = -(-left // 60) if left else 0       # minutes, rounded up
        self._choose_sleep(next((m for m in SLEEP_PRESETS_MIN if m > base), 0))

    def action_sleep_off(self) -> None:
        self._choose_sleep(0)

    def _choose_sleep(self, minutes: int) -> None:
        self._sleep_pending = minutes
        self._update_subtitle()
        if self._sleep_timer is not None:
            self._sleep_timer.stop()
        self._sleep_timer = self.set_timer(SLEEP_SETTLE_S, self._push_sleep)

    def _push_sleep(self) -> None:
        self._sleep_timer = None
        self.push_sleep(self.output, self._sleep_pending or 0)

    @work(thread=True, exclusive=True, group="sleep")
    def push_sleep(self, out, minutes: int) -> None:
        try:
            left = out.set_sleep_timer(minutes * 60)
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
            left = None
        self.call_from_thread(self._sleep_landed, out, minutes, left)

    def _sleep_landed(self, out, minutes: int, left: int | None) -> None:
        if out is not self.output or self._sleep_timer is not None:
            return          # moved on, or another press is pending
        self._sleep_pending = None
        self._sleep_at = time.monotonic() + left if left else None
        self._update_subtitle()
        if minutes and left:
            at = time.strftime("%H:%M", time.localtime(time.time() + left))
            self.notify(f"{out.label} stops at {at}", title="☾ sleep timer", timeout=5)
        elif minutes:
            self.notify(f"{out.label} took the timer but reports none running",
                        title="☾ sleep timer", severity="warning", timeout=8)
        else:
            self.notify(f"sleep timer off on {out.label}", title="☾", timeout=3)

    def action_stop(self) -> None:
        self._simple(lambda out: self.outputs.stop(out.id))

    def action_disconnect(self) -> None:
        self._simple(lambda out: self.outputs.disconnect(out.id))

    def action_back_to_relay(self) -> None:
        self._pending = None
        self._simple(lambda out: out.back_to_relay())

    @work(thread=True, exclusive=True, group="tune")
    def _simple(self, fn) -> None:
        try:
            msg = fn(self.output)
        except spotify_ops.PlaybackError as exc:
            self.call_from_thread(self._error, exc)
            return
        self.call_from_thread(self._status, msg)
        self.call_from_thread(self.poll_output)

    def action_choose_output(self) -> None:
        self.pick_output()

    @work(thread=True, exclusive=True, group="output-pick")
    def pick_output(self) -> None:
        self.call_from_thread(self._status, "looking for speakers…")
        choices = self.outputs.choices()
        opts = [(oid, ("● " if oid == self.output.id else "  ") + label)
                for oid, label in choices]
        k = here.KIND
        note = (f"A Sonos room plays the station itself (the {k} can sleep). "
                f"This {k} plays it here with ffmpeg; "
                f"Bluetooth plays from this {k} to the headphones. Volume and mute on both "
                "are dial's own: other apps' sound is left alone. "
                "A station playing now moves to the new output and stops on the old.")

        def done(choice: str | None) -> None:
            self._status("")
            if choice is None or choice == self.output.id:
                return
            old_id = self.output.id
            self.output = self.outputs.get(choice)
            state.set_state("output", choice)
            self.out_ready = False
            self.out_state = output_mod.OutputState()
            self.volume, self._pending = None, None
            self._sleep_at, self._sleep_pending = None, None
            if self._sleep_timer is not None:
                self._sleep_timer.stop()
                self._sleep_timer = None
            for s in self.stations:
                c = self.card(s.key)
                c.tuned = False
                c.set_class(False, "-tuned")
                c.render_text()
            self._update_subtitle()
            self._render_nowbar()
            self._render_station()
            self.poll_output()
            self.move_stream(old_id, self.output)
        self.call_from_thread(self.push_screen,
                              ChoiceScreen("Play the radio where?", opts, note), done)

    # ---- the rest ---------------------------------------------------------------

    def action_expand(self) -> None:
        self.expanded = not self.expanded
        st = self._st()
        self._render_about(st.np, self._card_for(st.np))

    def action_open_link(self) -> None:
        card = self._card_for(self._st().np)
        links = card.info.links if card and card.info else {}
        url = next((links[k] for k in LINK_ORDER if k in links), None)
        if not url:
            self.notify("no link known for this artist yet", severity="warning")
            return
        webbrowser.open(url)
        self.notify(url, title="opened", timeout=3)

    def action_refresh(self) -> None:
        self.feed.refresh()
        self.poll_output()
        self.notify("refreshing every station", timeout=2)

    def action_cycle_theme(self) -> None:
        cur = self.theme if self.theme in THEMES else THEMES[-1]
        self.theme = THEMES[(THEMES.index(cur) + 1) % len(THEMES)]
        state.set_state("theme", self.theme)
        self.notify(self.theme, title="theme", timeout=2)

    def action_help(self) -> None:
        self.push_screen(DialHelp())

    def viz_source(self):
        """Worker thread (the visualizer's, every few seconds). What to tap
        on the output: from dial's own poll, not a fresh one -- asking a Roam
        more often than dial already does adds to the control-port latency
        that is the dropout proxy. Only before the first poll lands does it ask."""
        from ..viz import source
        out = self.output
        st = self.out_state
        if not self.out_ready:
            try:
                st = out.state()
            except output_mod.PlaybackError:
                pass
        return source.for_dial(self.outputs, out, st)

    def action_visualize(self) -> None:
        from ..viz.screen import VizScreen     # numpy: only when asked for
        from ..viz.screen import VolumeControls
        controls = VolumeControls(
            nudge=self.action_volume, mute=self.action_mute,
            level=lambda: (" volume: not known yet " if self.volume is None
                           else f" ✕ muted ({self.volume}) " if self.muted
                           else f" ♪ volume {self.volume} "))
        self.push_screen(VizScreen(self.viz_source, controls=controls, **self.viz_options))

    def on_unmount(self) -> None:
        self.feed.close()
        self.enricher.close()
        # Stops ffplay if this app started it; a Sonos room plays on.
        self.outputs.close()

