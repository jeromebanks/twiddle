"""`twiddle alarm` with no verb: the household's alarms in a Textual list.

UI only, over `alarms/clock`, with `alarm_cli` for how an alarm is described.
Opening it, browsing and `r` are read-only: one tolerant `ListAlarms` (the
alarms twiddle can't read are listed apart, with why) plus the household's
clock. Only two keys write, through the same journalled `clock` writes as the
CLI: space (`UpdateAlarm`, only `Enabled` changed) and `d` (`DestroyAlarm`,
after asking twice: a y, then the ID typed out).

What you saw is what you write: each write reads the list again strictly, so
an unreadable alarm refuses it, and if the list moved since it was shown (the
Sonos app?) nothing is written and the list is read again. `--dry-run` does
that read and says what it would do, and writes neither speaker nor journal.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import alarm_cli
from ..household import Household
from . import clock
from .model import Alarm, UnreadableAlarm

ON, OFF = "●", "○"
KEYS = "space on/off  d delete  r refresh  q quit"
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
        for status in ("ok", "bonded_follower", "vanished", "unknown"):
            group = [r for r in mine if r["status"] == status]
            for i, r in enumerate(group):
                if i == 0 or heading(r) != heading(group[i - 1]):
                    out.append(Option(Text(heading(r), style="bold"), disabled=True))
                out.append(Option(alarm_line(r), id=f"alarm:{r['id']}"))
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


class AlarmApp(App):
    TITLE = "twiddle alarm"
    CSS = """
    #top { height: 1; padding: 0 1; text-style: bold; }
    #alarms { height: 1fr; border: round $panel-lighten-2; }
    #legend, #keys, #status { height: auto; padding: 0 1; }
    #keys { color: $text-muted; }
    AskScreen, TypeIdScreen { align: center middle; }
    #dialog { width: 70; height: auto; border: round $accent; padding: 1 2;
              background: $surface; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("space", "toggle", "On/off"),
        Binding("d", "delete", "Delete"),
        Binding("r", "reload", "Refresh"),
        Binding("j", "cursor('down')", show=False),
        Binding("k", "cursor('up')", show=False),
    ]

    def __init__(self, *, household: Callable[[], Household], dry_run: bool = False):
        super().__init__()
        self.household = household
        self.dry_run = dry_run
        self.shown: Shown | None = None
        self.status_text = ""

    def compose(self) -> ComposeResult:
        yield Static("twiddle alarm", id="top")
        yield OptionList(id="alarms")
        yield Static("", id="legend")
        yield Static(KEYS, id="keys")
        yield Static("", id="status")

    def on_mount(self) -> None:
        self.query_one("#alarms", OptionList).focus()
        self.load()

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
        """The highlighted alarm as shown: its ID, the alarm and the read it came from."""
        aid, shown = self._current_id(), self.shown
        if aid is None or shown is None or shown.alarm(aid) is None:
            return None
        return aid, shown.alarm(aid), shown

    # ---- keys --------------------------------------------------------------

    def action_cursor(self, direction: str) -> None:
        alarms = self.query_one("#alarms", OptionList)
        (alarms.action_cursor_down if direction == "down" else alarms.action_cursor_up)()

    def action_reload(self) -> None:
        self.say("")
        self.load()

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

    # ---- writing -----------------------------------------------------------

    @work(thread=True, exclusive=True, group="write")
    def _write(self, verb: str, aid: str, shown: Shown) -> None:
        """`verb` the alarm as `shown`, or nothing if that's no longer what
        the speaker has."""
        text, severity = self._do(verb, aid, shown)
        self.call_from_thread(self.say, text, severity)
        self.call_from_thread(self.load)

    def _do(self, verb: str, aid: str, shown: Shown) -> tuple[str, str]:
        house, ip, version = shown.house, shown.ip, shown.found.version
        try:
            found = clock.list_alarms(ip)
        except UnreadableAlarm as exc:
            name = f"alarm {exc.alarm_id}" if exc.alarm_id else "an alarm with no ID"
            return (f"twiddle can't read {name} ({exc.reason}), so it won't {verb} alarm "
                    f"{aid}: nothing written. Fix or delete it in the Sonos app", "error")
        except Exception as exc:
            return f"could not read the alarms from {ip}: {exc}; nothing written", "error"
        alarm = found.get(aid)
        if found.version != version or alarm is None:
            return ("the alarms changed since they were shown (the Sonos app?): "
                    f"nothing written; here they are again", "error")
        what = alarm_cli.brief(house, alarm)
        if self.dry_run:
            return f"[dry-run] would {verb} alarm {aid}: {what}", "information"
        try:
            if verb == "delete":
                clock.destroy_alarm(ip, aid, version)
                return f"deleted alarm {aid}: {what} (journalled whole)", "information"
            after, _ = clock.update_alarm(ip, replace(alarm, enabled=verb == "enable"), version)
            return f"{verb}d alarm {aid}: {alarm_cli.brief(house, after)}", "information"
        except Exception as exc:
            return _failed(verb, aid, exc), "error"


def _failed(verb: str, aid: str, exc: Exception) -> str:
    """A write that raised, saying whether it happened as far as can be told
    (as `alarm_cli._write_failed` does)."""
    landed = getattr(exc, "landed", False)
    if landed is True:
        return f"{verb}d alarm {aid}, but: {exc}"
    if landed is None:
        return f"could not {verb} alarm {aid}: {exc}; it may have happened anyway"
    if isinstance(exc, clock.VersionChanged):
        return (f"could not {verb} alarm {aid}: someone changed an alarm meanwhile "
                "(the Sonos app?); nothing written")
    return f"could not {verb} alarm {aid}: {exc}"
