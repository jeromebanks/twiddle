"""The speaker's `AlarmClock` service: the read side.

Alarms are household-wide, so any speaker answers for all of them. Every call
here is a read (`ListAlarms`, `GetTimeNow`, `GetFormat`); the
writes come later and go through the journal.

Each call has a pure parser beside it, so tests feed recorded SOAP responses
and never reach a speaker.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

from ..devices import soap
from .model import Alarm, parse_alarms

SERVICE = "AlarmClock"
READS = frozenset({"ListAlarms", "GetTimeNow", "GetFormat"})


@dataclass
class AlarmList:
    version: str            # CurrentAlarmListVersion, "<uuid>:<n>"; changes on every edit
    alarms: list[Alarm]


@dataclass
class HouseholdTime:
    """The household's own clock and display format, as the speaker reports it."""
    local: datetime         # naive: the household's local wall-clock time
    utc: datetime
    time_format: str = ""   # "INV" (unset) observed; "12H"/"24H" assumed
    date_format: str = ""

    def to_dict(self) -> dict:
        return {"local": self.local.isoformat(), "utc": self.utc.isoformat(),
                "time_format": self.time_format, "date_format": self.date_format}


def _response(xml: str, action: str) -> dict[str, str]:
    """The out-arguments of a SOAP `<action>Response`, by name."""
    body = ET.fromstring(xml)
    for el in body.iter():
        if el.tag.endswith(f"}}{action}Response") or el.tag == f"{action}Response":
            return {c.tag.rsplit("}", 1)[-1]: c.text or "" for c in el}
    raise ValueError(f"no {action}Response in the speaker's answer")


def parse_list_alarms(xml: str) -> AlarmList:
    r = _response(xml, "ListAlarms")
    return AlarmList(r.get("CurrentAlarmListVersion", ""),
                     parse_alarms(r.get("CurrentAlarmList") or "<Alarms/>"))


def parse_time_now(xml: str) -> tuple[datetime, datetime]:
    r = _response(xml, "GetTimeNow")
    fmt = "%Y-%m-%d %H:%M:%S"
    return (datetime.strptime(r["CurrentLocalTime"], fmt),
            datetime.strptime(r["CurrentUTCTime"], fmt))


def parse_format(xml: str) -> tuple[str, str]:
    r = _response(xml, "GetFormat")
    return r.get("CurrentTimeFormat", ""), r.get("CurrentDateFormat", "")


def _read(ip: str, action: str) -> str:
    assert action in READS, action
    return soap(ip, SERVICE, action)


def list_alarms(ip: str) -> AlarmList:
    """Every alarm in the household (read-only)."""
    return parse_list_alarms(_read(ip, "ListAlarms"))


def household_time(ip: str) -> HouseholdTime:
    """The household's time now, and how it likes times written (read-only)."""
    local, utc = parse_time_now(_read(ip, "GetTimeNow"))
    time_format, date_format = parse_format(_read(ip, "GetFormat"))
    return HouseholdTime(local, utc, time_format, date_format)
