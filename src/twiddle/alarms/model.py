"""One Sonos alarm, exactly as the speaker returns it, and its recurrence.

`AlarmClock.ListAlarms` answers with an `<Alarms>` document whose `<Alarm>`
attributes are the alarm. `CreateAlarm`/`UpdateAlarm` take the same fields as
arguments, except that the attribute `StartTime` is the argument
`StartLocalTime`. An `Alarm` keeps every field so `parse` then `create_args`
gives back what the speaker said:

- `ProgramURI` and `ProgramMetaData` are opaque, kept as the decoded string
  ElementTree hands back and never re-parsed. soco escapes arguments when it
  sends them, so an escaped value here would be escaped twice on the wire.
  The DIDL can itself carry escaped text (`&amp;apos;` in a title, `&amp;amp;`
  in an art URL), which has to survive exactly.
- Attributes the model doesn't know go in `extra`, and child elements in
  `children`, both untouched. Some music-service alarms carry a `<Content>`
  child (one of three Spotify alarms in the recorded fixture, same speaker and
  firmware). Neither is a CreateAlarm/UpdateAlarm argument, and whether an
  update keeps, drops or regenerates an existing `<Content>` is unverified.

No soco and no speaker here: `clock.py` does the calls.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# The named forms, as the speaker spells them. ONCE has no days.
NAMED = {
    "ONCE": frozenset(),
    "DAILY": frozenset(range(7)),
    "WEEKDAYS": frozenset(range(1, 6)),
    "WEEKENDS": frozenset({0, 6}),
}
_ON = re.compile(r"ON_[0-6]{1,7}")
FORMS = "ONCE, DAILY, WEEKDAYS, WEEKENDS or ON_<days> (1-7 digits 0-6, Sunday 0)"


@dataclass(frozen=True)
class Recurrence:
    """Which days an alarm fires on. Equal by days, so DAILY == ON_0123456;
    `str()` gives back the spelling it was parsed from."""
    days: frozenset[int]
    raw: str = field(compare=False)

    @classmethod
    def parse(cls, text: str) -> "Recurrence":
        if text in NAMED:
            return cls(NAMED[text], text)
        if _ON.fullmatch(text or ""):
            return cls(frozenset(int(d) for d in text[3:]), text)
        raise ValueError(f"invalid alarm recurrence {text!r}: expected {FORMS}")

    @classmethod
    def on(cls, days) -> "Recurrence":
        """The speaker's own name for a set of days (0 = Sunday): a named form
        when one fits, else ON_ with the days in order."""
        days = frozenset(days)
        if not days <= NAMED["DAILY"]:
            raise ValueError(f"invalid alarm days {sorted(days)}: days are 0-6, Sunday 0")
        for name, named in NAMED.items():
            if days == named:
                return cls(days, name)
        return cls(days, "ON_" + "".join(str(d) for d in sorted(days)))

    @property
    def once(self) -> bool:
        return not self.days

    def __str__(self) -> str:
        return self.raw


# ListAlarms attribute -> Alarm field, in the speaker's order.
_ATTRS = (
    ("ID", "id"),
    ("StartTime", "start_time"),
    ("Duration", "duration"),
    ("Recurrence", "recurrence"),
    ("Enabled", "enabled"),
    ("RoomUUID", "room_uuid"),
    ("ProgramURI", "program_uri"),
    ("ProgramMetaData", "program_metadata"),
    ("PlayMode", "play_mode"),
    ("Volume", "volume"),
    ("IncludeLinkedZones", "include_linked_zones"),
)
CHIME_URI = "x-rincon-buzzer:0"


@dataclass
class Alarm:
    """One alarm. `id` is None until the speaker has assigned one."""
    start_time: str                     # "HH:MM:SS", the household's local time
    recurrence: Recurrence
    room_uuid: str
    id: str | None = None
    duration: str = "02:00:00"          # auto-stop, "HH:MM:SS"
    enabled: bool = True
    program_uri: str = CHIME_URI
    program_metadata: str = ""
    play_mode: str = "NORMAL"           # the speaker's own value, e.g. SHUFFLE, REPEAT_ALL
    volume: int = 25
    include_linked_zones: bool = False
    extra: dict[str, str] = field(default_factory=dict)
    children: tuple[str, ...] = ()

    @classmethod
    def from_element(cls, el: ET.Element) -> "Alarm":
        a = dict(el.attrib)
        missing = [k for k, _ in _ATTRS if k not in a]
        if missing:
            raise ValueError(f"alarm {a.get('ID', '?')}: missing {', '.join(missing)}")
        try:
            return cls(
                id=a.pop("ID"),
                start_time=a.pop("StartTime"),
                duration=a.pop("Duration"),
                recurrence=Recurrence.parse(a.pop("Recurrence")),
                enabled=_flag(a.pop("Enabled")),
                room_uuid=a.pop("RoomUUID"),
                program_uri=a.pop("ProgramURI"),
                program_metadata=a.pop("ProgramMetaData"),
                play_mode=a.pop("PlayMode"),
                volume=_volume(a.pop("Volume")),
                include_linked_zones=_flag(a.pop("IncludeLinkedZones")),
                extra=a,
                children=tuple(_child(c) for c in el),
            )
        except ValueError as e:
            raise ValueError(f"alarm {el.get('ID')}: {e}") from None

    def _field(self, name: str) -> str:
        v = getattr(self, name)
        if isinstance(v, bool):
            return "1" if v else "0"
        return str(v)

    def to_attributes(self) -> dict[str, str]:
        """The `<Alarm>` attributes ListAlarms would show for this alarm."""
        out = {k: self._field(f) for k, f in _ATTRS if not (f == "id" and self.id is None)}
        return out | self.extra

    def create_args(self) -> list[tuple[str, str]]:
        """CreateAlarm's arguments, in soco's order (`soco.alarms.Alarm.save`)."""
        return [("StartLocalTime" if k == "StartTime" else k, self._field(f))
                for k, f in _ATTRS if f != "id"]

    def update_args(self) -> list[tuple[str, str]]:
        """UpdateAlarm's arguments: CreateAlarm's, after the ID."""
        if self.id is None:
            raise ValueError("an alarm the speaker hasn't created has no ID to update")
        return [("ID", self.id), *self.create_args()]


def parse_alarms(xml: str | bytes) -> list[Alarm]:
    """Every alarm in a ListAlarms `CurrentAlarmList`."""
    root = ET.fromstring(xml)
    if root.tag != "Alarms":
        raise ValueError(f"expected an <Alarms> document, got <{root.tag}>")
    return [Alarm.from_element(el) for el in root.iter("Alarm")]


def _flag(text: str) -> bool:
    if text not in ("0", "1"):
        raise ValueError(f"expected 0 or 1, got {text!r}")
    return text == "1"


def _volume(text: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]?|100", text):
        raise ValueError(f"expected a volume 0-100, got {text!r}")
    return int(text)


def _child(el: ET.Element) -> str:
    tail, el.tail = el.tail, None
    try:
        return ET.tostring(el, encoding="unicode")
    finally:
        el.tail = tail
