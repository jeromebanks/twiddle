"""`twiddle alarm` with no verb: the household's alarms in a Textual list.

UI only, over `alarms/clock`, with `alarm_cli` for how an alarm is described.
Opening it, browsing and `r` are read-only: one tolerant `ListAlarms` (the
alarms twiddle can't read are listed apart, with why) plus the household's
clock. What writes goes through the same journalled `clock` writes as the CLI:
space (`UpdateAlarm`, only `Enabled` changed), `d` (`DestroyAlarm`, after
asking twice: a y, then the ID typed out), and saving the editor that `n` (a
new alarm, `CreateAlarm`) and enter (this one, `UpdateAlarm`) open. `t` fires
the highlighted alarm now (`RunAlarm`, as `alarm try`).

While it is open it also asks each group (`GetRunningAlarmProperties`, read-only,
every few seconds) whether an alarm is going off. One that is takes the whole
screen: a ringing alarm clock, the room and what plays, `x` to turn it off
(`Stop`) and `z` to snooze (`SnoozeAlarm`; `m` picks 5/10/15/30 minutes), the
CLI's own journalled `alarm stop`/`alarm snooze`.

The editor changes only the fields you change: one left alone goes back as
the speaker gave it, so an alarm whose source twiddle doesn't recognise keeps
its URI and metadata byte for byte. Its source picker lists whatever is in
the `alarms/sources/` registry, each with a ✓ or the ⌁ warning and its
fallback, so a new provider needs no edit here. Searching and building a
source happen in a thread: a provider may ask the network or the household.

What you saw is what you write: each write reads the list again strictly, so
an unreadable alarm refuses it, and if the list moved since it was shown (the
Sonos app?) nothing is written and the list is read again. `--dry-run` does
that read and says what it would do, and writes neither speaker nor journal
(`t`, `x` and `z` included).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timezone
from typing import Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from .. import alarm_cli
from ..household import Ambiguous, Household, NotFound
from . import baseline, clock, sources
from .model import Alarm, Recurrence, UnreadableAlarm

ON, OFF = "●", "○"
KEYS = "n new  enter edit  space on/off  d delete  t try now  r refresh  q quit"
POLL_S = 5.0            # how often it asks whether an alarm is going off
RING_KEYS = "x off    z snooze    m snooze length    (the speaker keeps ringing until you do)"
TITLE_WIDTH = 30


@dataclass
class Shown:
    """One read of the household's alarms, as the list shows it."""
    house: Household
    ip: str
    found: clock.AlarmList
    hh: clock.HouseholdTime
    rows: list[dict]
    bad: list[dict]

    def alarm(self, aid: str) -> Alarm | None:
        return self.found.get(aid)


def _rooms(house: Household) -> list[str]:
    return sorted({s.room or s.name for s in house.speakers if not s.invisible}, key=str.lower)


def _mode(r: dict) -> str:
    name = alarm_cli._mode_name(r["play_mode"])
    return "" if name == "normal" else name


def alarm_line(r: dict) -> str:
    """One alarm, as the main list shows it."""
    mac = f" {alarm_cli.NEEDS_MAC}" if r["needs_mac"] else ""
    title = r["source_title"]
    title = title if len(title) <= TITLE_WIDTH else title[:TITLE_WIDTH - 1].rstrip() + "…"
    return (f"  {ON if r['enabled'] else OFF} {r['time_text']:>8}  {r['days_text']:<18} "
            f"{title + mac:<{TITLE_WIDTH + 2}} vol {r['volume']:<3} {r['duration_text']:<7} "
            f"{_mode(r)}").rstrip()


def heading(r: dict) -> str:
    if r["status"] == "ok":
        return r["room"]
    return f"{r['room']} — {alarm_cli._LABEL[r['status']].format(**r)}"


def options(shown: Shown) -> list[Option]:
    """Every room (one with no alarms says so), each followed by its alarms:
    a bonded follower's or a vanished speaker's under their own labelled
    heading; then the alarms that couldn't be read. Only alarms are selectable."""
    out = []
    rooms = sorted(set(_rooms(shown.house)) | {r["room"] for r in shown.rows}, key=str.lower)
    for room in rooms:
        mine = [r for r in shown.rows if r["room"] == room]
        if not mine:
            out += [Option(Text(room, style="bold"), disabled=True),
                    Option(Text("  (no alarms)", style="dim"), disabled=True)]
            continue
        order = ("ok", "bonded_follower", "vanished", "unknown")
        for head in sorted({heading(r) for r in mine},
                           key=lambda h: (min(order.index(r["status"]) for r in mine
                                              if heading(r) == h), h)):
            out.append(Option(Text(head, style="bold"), disabled=True))
            out += [Option(alarm_line(r), id=f"alarm:{r['id']}")
                    for r in mine if heading(r) == head]
    if shown.bad:
        many = len(shown.bad) != 1
        out.append(Option(Text(f"can't read {len(shown.bad)} alarm{'s' if many else ''} "
                               f"(fix or delete {'them' if many else 'it'} in the Sonos app)",
                               style="bold red"), disabled=True))
        for b in shown.bad:
            name = f"#{b['id']}" if b["id"] else "an alarm with no ID"
            out.append(Option(Text(f"  {name} {b['room'] or 'no room'}: {b['reason']}",
                                   style="red"), disabled=True))
    return out


def legend(shown: Shown) -> str:
    parts = []
    if any(r["needs_mac"] for r in shown.rows):
        parts.append(f"{alarm_cli.NEEDS_MAC} = needs this Mac at fire time")
    soon = min((r for r in shown.rows if r["next_fire"]), key=lambda r: r["next_fire"],
               default=None)
    if soon:
        parts.append(f"next: {soon['next_fire_text']} {soon['source_title']}")
    return "     ".join(parts)


def top(shown: Shown, dry_run: bool) -> str:
    now = shown.hh.local
    when = (f"{alarm_cli._DAY[(now.weekday() + 1) % 7]} "
            f"{alarm_cli._date_text(now, shown.hh.date_format)}  "
            f"{alarm_cli.clock_text(now.time().replace(second=0), shown.hh.time_format)}")
    return f"twiddle alarm{'  [dry-run: writes nothing]' if dry_run else ''}    {when}"


def mac_line(source: sources.Source | None, at: str) -> str:
    """The editor's last line: whether the alarm plays without this Mac."""
    if source is None or not source.needs_mac:
        return "✓ Plays without this Mac."
    return f"{alarm_cli.NEEDS_MAC} Needs this Mac awake at {at}. If it isn't: {source.fallback}."


def provider_line(source: sources.Source, at: str) -> Text:
    """One provider in the source picker, with its ✓ or ⌁ warning."""
    out = Text(source.title, style="bold")
    out.append(f"\n    {mac_line(source, at)}", style="yellow" if source.needs_mac else "dim")
    return out


def targets(house: Household) -> list[tuple[str, str]]:
    """`(name, RoomUUID)` for every room an alarm can be aimed at: its primary
    unit, as `alarm add --room` aims it."""
    out = []
    for name in _rooms(house):
        try:
            out.append((name, alarm_cli.room_target(house, name)["room_uuid"]))
        except (Ambiguous, NotFound):
            continue                # two rooms of one name: `alarm add` names them by IP
    return out


def room_choices(house: Household, alarm: Alarm) -> list[tuple[str, str]]:
    """`targets`, plus the alarm's own RoomUUID as it is when that is none of
    them (a bonded follower, a vanished speaker), so leaving it alone keeps it."""
    out = targets(house)
    if alarm.room_uuid not in {uuid for _, uuid in out}:
        r = alarm_cli.aimed_at(house, alarm.room_uuid) | {"room_uuid": alarm.room_uuid}
        out.append((f"{heading(r)} (as it is)", alarm.room_uuid))
    return out


def mode_choices(alarm: Alarm) -> list[tuple[str, str]]:
    """The play modes by the CLI's names, plus the alarm's own if it is none of them."""
    out = list(alarm_cli.PLAY_MODES.items())
    if alarm.play_mode not in alarm_cli.PLAY_MODES.values():
        out.append((f"{alarm.play_mode} (as it is)", alarm.play_mode))
    return out


def time_seed(start_time: str) -> str:
    return start_time[:5] if start_time.endswith(":00") and len(start_time) == 8 else start_time


def duration_seed(duration: str) -> str:
    """The auto-stop as the editor shows it: what `alarm_cli.duration_text`
    says, when `parse_duration` reads that back to the same value, else as is."""
    if not duration:
        return "none"
    text = alarm_cli.duration_text(duration)
    try:
        return text if alarm_cli.parse_duration(text) == duration else duration
    except ValueError:
        return duration


_CLOCK = r"""
   {a}  _______  {b}
 {w} (  .-""-.  ) {w}
   {b} /  /  12   \  \ {a}
        |    |    |
        | 9  o──3 |
        |         |
         \   6   /
          '-...-'
          /     \
     ~~~~~~~~~~~~~~~~~
""".strip("\n")


def clock_art(frame: int) -> str:
    """The ringing alarm clock; `frame` swings its bells and its sound."""
    a, b, w = ("\\", "/", "──") if frame % 2 else ("/", "\\", "  ")
    return _CLOCK.replace("{a}", a).replace("{b}", b).replace("{w}", w)


def ringing_text(row: dict, minutes: int) -> str:
    """What the ringing screen says beside the clock."""
    what = row.get("what") or "an alarm"
    since = f" · since {row['logged_start'][11:16]}" if row.get("logged_start") else ""
    snooze = "snoozed" if row.get("snoozed") else "is ringing"
    return (f"⏰  {row['room']} {snooze}\n{what}{since}\n\n"
            f"[ x  off ]   [ z  snooze {minutes}m ▾ ]")


@dataclass
class Picked:
    """A source chosen in the picker, already built."""
    source: sources.Source
    title: str
    uri: str
    metadata: str


@dataclass
class Edited:
    """What the editor saves: the alarm it opened on (no ID: a new one) and
    only the fields changed, as `Alarm` fields."""
    base: Alarm
    changes: dict


# Sonos numbers days from Sunday 0; the editor reads Monday first.
_DAYS = [(d, alarm_cli._DAY[d]) for d in alarm_cli._READING_ORDER]
_SHORTCUTS = ("once", "daily", "weekdays", "weekends")


class SourcePicker(ModalScreen[Picked | None]):
    """Every registered source, then (for one that takes a choice) its
    choices, searched; the one picked is built before it is handed back."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, at: str, anchor: str | None):
        super().__init__()
        self.at, self.anchor = at, anchor
        self.source: sources.Source | None = None
        self.found: list[sources.Choice] = []
        self.status_text = ""
        self.ticket = 0         # the latest search or build; an older one's answer is dropped

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Pick a source", id="picker-title")
            yield OptionList(*[Option(provider_line(s, self.at), id=s.name)
                               for s in sources.all_sources()], id="providers")
            yield Input(placeholder="search, then enter", id="query")
            yield OptionList(id="choices")
            yield Static("", id="picker-status")

    def on_mount(self) -> None:
        self.query_one("#query").display = False
        self.query_one("#choices").display = False
        self.query_one("#providers", OptionList).focus()

    def say(self, text: str) -> None:
        self.status_text = text
        self.query_one("#picker-status", Static).update(text)

    @on(OptionList.OptionSelected, "#providers")
    def provider(self, event: OptionList.OptionSelected) -> None:
        source = sources.get(event.option.id).bind(self.anchor)
        self.source = source
        if not source.takes_choice:
            self.say(f"building {source.title}…")
            self.build(source, sources.Choice("", source.title), self._next())
            return
        self.query_one("#picker-title", Static).update(
            f"{source.title}{' ' + alarm_cli.NEEDS_MAC if source.needs_mac else ''}: "
            "pick one, or search")
        self.query_one("#providers").display = False
        self.query_one("#query").display = True
        self.query_one("#choices").display = True
        self.query_one("#query", Input).focus()
        self.search("", self._next())

    @on(Input.Submitted, "#query")
    def submitted(self, event: Input.Submitted) -> None:
        if self.source is not None:
            self.search(event.value, self._next())

    def _next(self) -> int:
        self.ticket += 1
        return self.ticket

    def _back(self, ticket: int, fn, *args) -> None:
        """From a worker: `fn(*args)` on the UI thread, if this picker is still
        the screen on top (escape may have closed it while a provider was slow)
        and `ticket` is still the latest: a thread can't be stopped, so a
        search or build that was overtaken finishes, and its answer is dropped."""
        def run():
            if ticket == self.ticket and self.is_attached and self.app.screen is self:
                fn(*args)
        self.app.call_from_thread(run)

    @work(thread=True, group="search")
    def search(self, query: str, ticket: int) -> None:
        self._back(ticket, self.say, f"searching {self.source.title}…" if query else "")
        try:
            found = self.source.choices(query)
        except Exception as exc:        # a provider's failure is said, never the app's end
            self._back(ticket, self.say, str(exc) or type(exc).__name__)
            return
        self._back(ticket, self.listed, query, found)

    def listed(self, query: str, found: list[sources.Choice]) -> None:
        self.found = found
        choices = self.query_one("#choices", OptionList)
        choices.clear_options()
        choices.add_options([Option(f"{c.title}" + (f"  — {c.detail}" if c.detail else ""),
                                    id=str(i)) for i, c in enumerate(found)])
        if found:
            self.say("")
            choices.highlighted = 0
            if query:
                choices.focus()
        else:
            self.say(f"nothing matches {query!r}" if query else "type something to search")

    @on(OptionList.OptionSelected, "#choices")
    def chosen(self, event: OptionList.OptionSelected) -> None:
        choice = self.found[int(event.option.id)]
        self.say(f"building {choice.title}…")
        self.build(self.source, choice, self._next())

    @work(thread=True, group="build")
    def build(self, source: sources.Source, choice: sources.Choice, ticket: int) -> None:
        try:
            uri, metadata = source.build(choice.key)
            title = source.title if not source.takes_choice else \
                f"{source.title} · {source.describe(uri, metadata) or choice.title}"
        except Exception as exc:
            self._back(ticket, self.say, str(exc) or type(exc).__name__)
            return
        self._back(ticket, self.dismiss, Picked(source, title, uri, metadata))

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditorScreen(ModalScreen[Edited | None]):
    """Every field of one alarm (or a new one). Save hands back only what
    changed, and only when every changed field can be read."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "save", "Save")]

    def __init__(self, shown: Shown, alarm: Alarm):
        super().__init__()
        self.shown, self.base = shown, alarm
        self.picked: Picked | None = None
        self.problem = ""

    def compose(self) -> ComposeResult:
        a, house = self.base, self.shown.house
        with Vertical(id="editor"):
            yield Static("New alarm" if a.id is None else f"Alarm {a.id}", id="editor-title")
            with Horizontal(classes="row"):
                yield Static("Time", classes="label")
                yield Input(time_seed(a.start_time), id="time", classes="short")
            with Horizontal(classes="row"):
                yield Static("Days", classes="label")
                for d, name in _DAYS:
                    yield Checkbox(name, d in a.recurrence.days, id=f"day-{d}", compact=True)
            with Horizontal(classes="row"):
                yield Static("", classes="label")
                for name in _SHORTCUTS:
                    yield Button(name, id=name, compact=True)
            with Horizontal(classes="row"):
                yield Static("Room", classes="label")
                yield Select(room_choices(house, a), value=a.room_uuid, allow_blank=False,
                             id="room")
                yield Checkbox("include grouped rooms", a.include_linked_zones,
                               id="grouped", compact=True)
            with Horizontal(classes="row"):
                yield Static("Source", classes="label")
                yield Static(alarm_cli.source_title(a), id="source")
                yield Button("change…", id="change", compact=True)
            with Horizontal(classes="row"):
                yield Static("Volume", classes="label")
                yield Input(str(a.volume), id="volume", classes="short")
                yield Static("Stop after", classes="label")
                yield Input(duration_seed(a.duration), id="duration", classes="short")
            with Horizontal(classes="row"):
                yield Static("Mode", classes="label")
                yield Select(mode_choices(a), value=a.play_mode, allow_blank=False, id="mode")
                yield Checkbox("on", a.enabled, id="enabled", compact=True)
            yield Static("", id="mac")
            yield Static("", id="problem")
            with Horizontal(id="buttons"):
                yield Button("Save", id="save", variant="primary", compact=True)
                yield Button("Cancel", id="cancel", compact=True)

    def on_mount(self) -> None:
        self.update_mac()
        self.query_one("#time", Input).focus()

    def source(self) -> sources.Source | None:
        if self.picked is not None:
            return self.picked.source
        return sources.recognise(self.base.program_uri, self.base.program_metadata)

    def at_text(self) -> str:
        """The time in the editor, the way the household writes it."""
        typed = self.query_one("#time", Input).value
        try:
            at = alarm_cli.parse_time(typed)
        except ValueError:
            return typed.strip() or "?"
        return alarm_cli.clock_text(time.fromisoformat(at), self.shown.hh.time_format)

    def update_mac(self) -> None:
        self.query_one("#mac", Static).update(mac_line(self.source(), self.at_text()))

    @on(Input.Changed, "#time")
    def time_changed(self) -> None:
        self.update_mac()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid in _SHORTCUTS:
            days = alarm_cli.parse_days(bid).days
            for d, _ in _DAYS:
                self.query_one(f"#day-{d}", Checkbox).value = d in days
        elif bid == "change":
            self.app.push_screen(SourcePicker(self.at_text(), self.shown.ip), self.picked_source)
        elif bid == "save":
            self.action_save()
        elif bid == "cancel":
            self.action_cancel()

    def picked_source(self, picked: Picked | None) -> None:
        if picked is None:
            return
        self.picked = picked
        self.query_one("#source", Static).update(picked.title)
        self.update_mac()

    def changes(self) -> dict:
        """The fields changed, parsed; ValueError names the first that can't be."""
        a, out = self.base, {}
        for wid, field, seed, parse in (
                ("time", "start_time", time_seed(a.start_time), alarm_cli.parse_time),
                ("volume", "volume", str(a.volume), alarm_cli.parse_volume),
                ("duration", "duration", duration_seed(a.duration), alarm_cli.parse_duration)):
            typed = self.query_one(f"#{wid}", Input).value
            if typed.strip() != seed:
                out[field] = parse(typed)
        days = frozenset(d for d, _ in _DAYS if self.query_one(f"#day-{d}", Checkbox).value)
        if days != a.recurrence.days:
            out["recurrence"] = Recurrence.on(days)
        for wid, field in (("grouped", "include_linked_zones"), ("enabled", "enabled")):
            if self.query_one(f"#{wid}", Checkbox).value != getattr(a, field):
                out[field] = self.query_one(f"#{wid}", Checkbox).value
        for wid, field in (("room", "room_uuid"), ("mode", "play_mode")):
            if self.query_one(f"#{wid}", Select).value != getattr(a, field):
                out[field] = self.query_one(f"#{wid}", Select).value
        if self.picked is not None and (self.picked.uri, self.picked.metadata) != \
                (a.program_uri, a.program_metadata):
            out["program_uri"], out["program_metadata"] = self.picked.uri, self.picked.metadata
        return out

    def action_save(self) -> None:
        try:
            changes = self.changes()
        except ValueError as exc:
            self.problem = str(exc)
            self.query_one("#problem", Static).update(Text(self.problem, style="red"))
            return
        self.dismiss(Edited(self.base, changes))

    def action_cancel(self) -> None:
        self.dismiss(None)


class AskScreen(ModalScreen[bool]):
    """A yes/no question; only y (or yes) is yes."""

    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n", "answer(False)", "No"),
                Binding("escape", "answer(False)", "No")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"{self.question} [y/N]")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class TypeIdScreen(ModalScreen[str | None]):
    """The second ask: the alarm's ID, typed out."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, aid: str):
        super().__init__()
        self.aid = aid

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"type its ID ({self.aid}) to delete it, escape to keep it")
            yield Input(id="typed")

    def on_mount(self) -> None:
        self.query_one("#typed", Input).focus()

    @on(Input.Submitted, "#typed")
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class RingingScreen(Screen):
    """An alarm is going off: the whole screen, until it is stopped, snoozed
    or stops by itself. It only asks the app to write (`ring_stop`/`ring_snooze`)."""

    BINDINGS = [Binding("x", "stop", "Off"), Binding("z", "snooze", "Snooze"),
                Binding("m", "length", "Snooze length")]

    def __init__(self, row: dict, ip: str):
        super().__init__()
        self.row, self.ip = row, ip
        self.minutes = clock.DEFAULT_SNOOZE
        self.frame = 0
        self.key: tuple = ()
        self.busy = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="ring"):
            yield Static(clock_art(0), id="ring-art")
            yield Static(ringing_text(self.row, self.minutes), id="ring-text", markup=False)
        yield Static(RING_KEYS, id="ring-keys")
        yield Static("", id="ring-status")

    def on_mount(self) -> None:
        self.set_interval(0.6, self.swing)

    def swing(self) -> None:
        self.frame += 1
        self.query_one("#ring-art", Static).update(clock_art(self.frame))

    def update(self, row: dict, ip: str) -> None:
        self.row, self.ip = row, ip         # what is shown and what x/z write to move together
        self.query_one("#ring-text", Static).update(ringing_text(row, self.minutes))

    def say(self, text: str) -> None:
        self.query_one("#ring-status", Static).update(text)

    def action_length(self) -> None:
        options = clock.SNOOZE_MINUTES
        self.minutes = options[(options.index(self.minutes) + 1) % len(options)]
        self.query_one("#ring-text", Static).update(ringing_text(self.row, self.minutes))

    def action_stop(self) -> None:
        if not self.busy:
            self.busy = True
            self.app.ring_stop(self)

    def action_snooze(self) -> None:
        if not self.busy:
            self.busy = True
            self.app.ring_snooze(self, self.minutes)


class AlarmApp(App):
    TITLE = "twiddle alarm"
    CSS = """
    #top { height: 1; padding: 0 1; text-style: bold; }
    #alarms { height: 1fr; border: round $panel-lighten-2; }
    #legend, #keys, #status { height: auto; padding: 0 1; }
    #keys { color: $text-muted; }
    AskScreen, TypeIdScreen, SourcePicker, EditorScreen { align: center middle; }
    #dialog { width: 70; height: auto; border: round $accent; padding: 1 2;
              background: $surface; }
    SourcePicker #dialog { width: 90; max-height: 90%; }
    #providers, #choices { height: auto; max-height: 20; }
    #editor { width: 90; height: auto; border: round $accent; padding: 0 1;
              background: $surface; }
    #editor .row { height: auto; }
    #editor .label { width: 11; }
    #editor .short { width: 12; }
    #editor Select { width: 30; }
    #editor Checkbox, #editor Button { margin-right: 1; }
    #source { width: 50; }
    #mac, #problem { height: auto; }
    #buttons { height: auto; align: right middle; }
    RingingScreen { align: center middle; }
    #ring { height: auto; width: auto; }
    #ring-art { width: 40; color: $warning; }
    #ring-text { width: 50; padding: 4 2; text-style: bold; }
    #ring-keys, #ring-status { width: 100%; content-align: center middle; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("n", "new", "New"),
        Binding("space", "toggle", "On/off"),
        Binding("d", "delete", "Delete"),
        Binding("t", "try", "Try now"),
        Binding("r", "reload", "Refresh"),
        Binding("j", "cursor('down')", show=False),
        Binding("k", "cursor('up')", show=False),
    ]

    def __init__(self, *, household: Callable[[], Household], dry_run: bool = False,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        super().__init__()
        self.now = now
        self.household = household
        self.dry_run = dry_run
        self.shown: Shown | None = None
        self.status_text = ""
        self.writing = False        # one write at a time: space and d do nothing meanwhile
        self.ringing: RingingScreen | None = None
        # Alarms stopped or snoozed here that the speaker may still report: not
        # shown again until it stops reporting them, or (a snooze) rings again.
        self.hushed: dict[tuple, datetime | None] = {}

    def compose(self) -> ComposeResult:
        yield Static("twiddle alarm", id="top")
        yield OptionList(id="alarms")
        yield Static("", id="legend")
        yield Static(KEYS, id="keys")
        yield Static("", id="status")

    def on_mount(self) -> None:
        self.query_one("#alarms", OptionList).focus()
        self.load()
        self.set_interval(POLL_S, self.check)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """While an alarm rings, the list's keys are not the screen's: a stray
        `t` must not fire whatever alarm is highlighted behind it."""
        if isinstance(self.screen, RingingScreen):
            return action == "quit"
        return True

    # ---- reading -----------------------------------------------------------

    def _read(self) -> Shown:
        """The one tolerant read: a view, which lists what it left out. Every
        write reads again strictly (`_write`)."""
        house = self.household()
        if not house.groups:
            raise RuntimeError("no speakers in the household")
        ip = house.groups[0].coordinator.ip
        found = clock.list_alarms(ip, tolerant=True)
        hh = clock.household_time(ip)
        return Shown(house, ip, found, hh, alarm_cli.listing(house, found.alarms, hh),
                     alarm_cli.unreadable_rows(house, found.unreadable))

    @work(thread=True, exclusive=True, group="read")
    def load(self) -> None:
        try:
            shown = self._read()
        except Exception as exc:
            self.call_from_thread(self.say, f"could not read the alarms: {exc}", "error")
            return
        self.call_from_thread(self.show, shown)

    def show(self, shown: Shown) -> None:
        first = self.shown is None
        self.shown = shown
        alarms = self.query_one("#alarms", OptionList)
        was = self._current_id()
        alarms.clear_options()
        alarms.add_options(options(shown))
        ids = [o.id for o in alarms.options]
        if was and f"alarm:{was}" in ids:
            alarms.highlighted = ids.index(f"alarm:{was}")
        else:
            alarms.highlighted = next((i for i, o in enumerate(alarms.options)
                                       if not o.disabled), None)
        self.query_one("#top", Static).update(top(shown, self.dry_run))
        self.query_one("#legend", Static).update(legend(shown))
        if first:
            self.check()            # one may be ringing already

    def say(self, text: str, severity: str = "information") -> None:
        self.status_text = text
        self.query_one("#status", Static).update(
            Text(text, style="red" if severity == "error" else ""))

    def _current_id(self) -> str | None:
        alarms = self.query_one("#alarms", OptionList)
        if alarms.highlighted is None or not alarms.option_count:
            return None
        oid = alarms.get_option_at_index(alarms.highlighted).id or ""
        return oid.removeprefix("alarm:") if oid.startswith("alarm:") else None

    def _selected(self) -> tuple[str, Alarm, Shown] | None:
        """The highlighted alarm as shown: its ID, the alarm and the read it
        came from; None while a write is still going."""
        aid, shown = self._current_id(), self.shown
        if self.writing or aid is None or shown is None or shown.alarm(aid) is None:
            return None
        return aid, shown.alarm(aid), shown

    # ---- keys --------------------------------------------------------------

    def action_cursor(self, direction: str) -> None:
        alarms = self.query_one("#alarms", OptionList)
        (alarms.action_cursor_down if direction == "down" else alarms.action_cursor_up)()

    def action_reload(self) -> None:
        self.say("")
        self.load()

    @on(OptionList.OptionSelected, "#alarms")
    def action_edit(self) -> None:
        """enter: edit the highlighted alarm."""
        picked = self._selected()
        if picked is not None:
            aid, alarm, shown = picked
            self.push_screen(EditorScreen(shown, alarm),
                             lambda edited: self._edited("update", aid, shown, edited))

    def action_new(self) -> None:
        shown = self.shown
        if self.writing or shown is None:
            return
        rooms = targets(shown.house)
        if not rooms:
            self.say("no room to put an alarm in", "error")
            return
        here = shown.alarm(self._current_id() or "")
        uuid = next((u for _, u in rooms if here is not None and u == here.room_uuid),
                    rooms[0][1])
        base = Alarm(start_time="07:00:00", recurrence=Recurrence.parse("WEEKDAYS"),
                     room_uuid=uuid, duration="01:00:00", volume=20)
        self.push_screen(EditorScreen(shown, base),
                         lambda edited: self._edited("create", None, shown, edited))

    def _edited(self, verb: str, aid: str | None, shown: Shown, edited: Edited | None) -> None:
        if edited is None:
            self.say("not saved")
        elif verb == "update" and not edited.changes:
            self.say(f"nothing changed: alarm {aid}")
        else:
            self._write(verb, aid, shown, edited)

    def action_toggle(self) -> None:
        picked = self._selected()
        if picked is not None:
            aid, alarm, shown = picked
            self._write("disable" if alarm.enabled else "enable", aid, shown)

    def action_delete(self) -> None:
        picked = self._selected()
        if picked is None:
            return
        aid, alarm, shown = picked
        if self.dry_run:            # as `alarm rm --dry-run`: nothing to ask about
            self._write("delete", aid, shown)
            return
        what = alarm_cli.brief(shown.house, alarm)

        def typed(answer: str | None) -> None:
            if answer == aid:
                self._write("delete", aid, shown)
            else:
                self.say(f"not deleted: alarm {aid}")

        def first(yes: bool | None) -> None:
            if yes:
                self.push_screen(TypeIdScreen(aid), typed)
            else:
                self.say(f"not deleted: alarm {aid}")

        self.push_screen(AskScreen(f"delete alarm {aid}: {what}?"), first)

    def action_try(self) -> None:
        """t: fire the highlighted alarm now, as `alarm try`."""
        picked = self._selected()
        if picked is not None:
            aid, _, shown = picked
            self._write("try", aid, shown)

    # ---- an alarm going off ------------------------------------------------

    @work(thread=True, exclusive=True, group="ring")
    def check(self) -> None:
        """Ask every group whether an alarm is going off (read-only). A group
        that can't be asked counts as quiet: the next poll asks again."""
        shown = self.shown
        if shown is None:
            return
        found = []
        for g in shown.house.groups:
            try:
                running = clock.alarm_now(g.coordinator.ip, events=False)
            except Exception:
                continue
            key = (g.coordinator.ip,) + ((running.alarm_id, running.logged_start)
                                         if running else ())
            if running is None:
                self.call_from_thread(self._quiet, g.coordinator.ip)
            elif not self._hushed(key):
                found.append((key, g.coordinator.ip,        # named from the list shown
                              alarm_cli.ringing_row(shown.house, g, running, shown.found)))
        if found:
            key, ip, row = found[0]
            self.call_from_thread(self._ring, key, ip, row)
        elif self.ringing is not None:
            self.call_from_thread(self._hush_screen)

    def _hushed(self, key: tuple) -> bool:
        """Was this stopped or snoozed here (and a snooze not yet over)?"""
        if key not in self.hushed:
            return False
        until = self.hushed[key]
        if until is not None and self.now() >= until:
            del self.hushed[key]
            return False
        return True

    def _quiet(self, ip: str) -> None:
        self.hushed = {k: v for k, v in self.hushed.items() if k[0] != ip}
        if self.ringing is not None and self.ringing.ip == ip:
            self._hush_screen()

    def _hush_screen(self) -> None:
        screen, self.ringing = self.ringing, None
        if screen is not None and screen.is_attached:
            screen.dismiss()

    def _ring(self, key: tuple, ip: str, row: dict) -> None:
        if self.ringing is not None and self.ringing.is_attached:
            self.ringing.key = key
            self.ringing.update(row, ip)
            return
        screen = RingingScreen(row, ip)
        screen.key = key
        self.ringing = screen
        self.push_screen(screen)
        if not row.get("what"):         # not in the list shown: made since
            self.load()

    def ring_stop(self, screen: RingingScreen) -> None:
        self._ring_write(screen, "stop", 0)

    def ring_snooze(self, screen: RingingScreen, minutes: int) -> None:
        self._ring_write(screen, "snooze", minutes)

    @work(thread=True, group="ring-write")
    def _ring_write(self, screen: RingingScreen, verb: str, minutes: int) -> None:
        """`alarm stop`/`alarm snooze` on the group that is ringing, refused
        (as the CLI does) when nothing is going off any more."""
        ip, name = screen.ip, screen.row["room"]
        until = None
        try:
            running = clock.alarm_now(ip)       # as the CLI: events too, before any Stop
            if running is None:
                text, done = f"no alarm is going off in {name} any more", True
            elif self.dry_run:
                text, done = (f"[dry-run] would {verb} the alarm in {name}"
                              + (f" for {minutes} minutes" if minutes else ""), False)
            elif verb == "stop":
                clock.stop_alarm(ip, running.alarm_id or None)
                text, done = f"stopped the alarm in {name}", True
            else:
                rings = clock.snooze_alarm(ip, minutes, running.alarm_id or None)
                until = rings
                text = f"snoozed the alarm in {name}: it rings again at {rings.astimezone():%H:%M}"
                done = True
        except Exception as exc:
            text, done = f"could not {verb} the alarm in {name}: {exc}", False
        self.call_from_thread(self._ring_written, screen, text, done, until)

    def _ring_written(self, screen: RingingScreen, text: str, done: bool,
                      until: datetime | None) -> None:
        screen.busy = False
        self.say(text)
        if not done:
            if screen.is_attached:
                screen.say(text)
            return
        self.hushed[screen.key] = until
        self._hush_screen()

    # ---- writing -----------------------------------------------------------

    def _write(self, verb: str, aid: str | None, shown: Shown,
               edited: Edited | None = None) -> None:
        if self.writing:
            return
        self.writing = True
        self._writer(verb, aid, shown, edited)

    @work(thread=True, group="write")
    def _writer(self, verb: str, aid: str | None, shown: Shown, edited: Edited | None) -> None:
        """`verb` the alarm as `shown`, or nothing if that's no longer what
        the speaker has."""
        try:
            text, severity = self._do(verb, aid, shown, edited)
        except Exception as exc:
            text, severity = f"could not {verb} {_name(aid)}: {exc}", "error"
        self.call_from_thread(self._written, text, severity)

    def _written(self, text: str, severity: str) -> None:
        self.writing = False
        self.say(text, severity)
        self.load()
        self.check()

    def _do(self, verb: str, aid: str | None, shown: Shown,
            edited: Edited | None = None) -> tuple[str, str]:
        """`verb` is enable, disable, delete, update (with `edited`) or create
        (with `edited` and no `aid`). The version checked is the one the list
        (and the editor) was opened on."""
        house, ip, version = shown.house, shown.ip, shown.found.version
        try:
            found = clock.list_alarms(ip)
        except UnreadableAlarm as exc:
            name = f"alarm {exc.alarm_id}" if exc.alarm_id else "an alarm with no ID"
            return (f"twiddle can't read {name} ({exc.reason}), so it won't {verb} "
                    f"{_name(aid)}: nothing written. Fix or delete it in the Sonos app", "error")
        except Exception as exc:
            return f"could not read the alarms from {ip}: {exc}; nothing written", "error"
        alarm = found.get(aid) if aid is not None else None
        if found.version != version or (aid is not None and alarm is None):
            return ("the alarms changed since they were shown (the Sonos app?): "
                    f"nothing written; here they are again", "error")
        if verb == "create":
            want = replace(edited.base, **edited.changes)
            if self.dry_run:
                return (f"[dry-run] would create an alarm: {alarm_cli.brief(house, want)}",
                        "information")
            try:
                after, _ = clock.create_alarm(ip, want, version)
            except Exception as exc:
                return _failed(verb, getattr(exc, "alarm_id", None), exc), "error"
            return f"created alarm {after.id}: {alarm_cli.brief(house, after)}", "information"
        what = alarm_cli.brief(house, alarm)
        if verb == "try":
            return self._try(house, ip, alarm, what)
        if verb == "update":
            want = replace(alarm, **edited.changes)
            fields = baseline.differences(want, alarm)
            if not fields:
                return f"alarm {aid} is already so: nothing written", "information"
            what = f"{alarm_cli.brief(house, want)}  ({', '.join(fields)})"
        else:
            want = replace(alarm, enabled=verb == "enable")
        if self.dry_run:
            return f"[dry-run] would {verb} alarm {aid}: {what}", "information"
        try:
            if verb == "delete":
                clock.destroy_alarm(ip, aid, version)
                return f"deleted alarm {aid}: {what} (journalled whole)", "information"
            after, _ = clock.update_alarm(ip, want, version)
            said = f"  ({', '.join(fields)})" if verb == "update" else ""
            return f"{verb}d alarm {aid}: {alarm_cli.brief(house, after)}{said}", "information"
        except Exception as exc:
            return _failed(verb, aid, exc), "error"

    def _try(self, house: Household, ip: str, alarm: Alarm, what: str) -> tuple[str, str]:
        """`alarm try`: RunAlarm on the coordinator of the alarm's room."""
        aim = alarm_cli.aimed_at(house, alarm.room_uuid)
        if aim["status"] in ("vanished", "unknown"):
            return (f"alarm {alarm.id} is aimed at a speaker that isn't here ({aim['room']}): "
                    "move it with the editor first", "error")
        sp = next(s for s in house.speakers if s.uuid == alarm.room_uuid)
        coordinator = house.group_of(sp).coordinator
        if self.dry_run:
            return f"[dry-run] would fire alarm {alarm.id} on {coordinator.label}: {what}", \
                "information"
        try:
            logged = clock.household_time(ip).local.strftime("%Y-%m-%d %H:%M:%S")
            stops = clock.run_alarm(coordinator.ip, alarm, logged)
        except Exception as exc:
            return f"could not fire alarm {alarm.id}: {exc}; `twiddle alarm status` shows " \
                   "whether it went off", "error"
        after = f"; it stops itself at {stops.astimezone():%H:%M}" if stops else ""
        return f"fired alarm {alarm.id} on {coordinator.label}: {what}{after}", "information"


def _name(aid: str | None) -> str:
    return f"alarm {aid}" if aid else "the alarm"


def _failed(verb: str, aid: str | None, exc: Exception) -> str:
    """A write that raised, saying whether it happened as far as can be told
    (as `alarm_cli._write_failed` does)."""
    landed = getattr(exc, "landed", False)
    if landed is True:
        return f"{verb}d {_name(aid)}, but: {exc}"
    if landed is None:
        return f"could not {verb} {_name(aid)}: {exc}; it may have happened anyway"
    if isinstance(exc, clock.VersionChanged):
        return (f"could not {verb} {_name(aid)}: someone changed an alarm meanwhile "
                "(the Sonos app?); nothing written")
    return f"could not {verb} {_name(aid)}: {exc}"
