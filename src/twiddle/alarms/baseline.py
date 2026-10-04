"""The household's alarms saved as a baseline, and put back to match it.

`household.Snapshot` covers one room's transport and volume, not alarms, which
belong to the whole household. An `AlarmSnapshot` is one `ListAlarms`: the
`CurrentAlarmList` document exactly as the speaker sent it, its version and
when it was read. Parsing it again gives every field the model keeps.

Restoring compares alarms by `key`: the fields CreateAlarm/UpdateAlarm can set,
with `ProgramURI` and `ProgramMetaData` compared as exact strings. IDs and the
list version are not compared: a recreated alarm gets a new ID. `extra`
attributes and child elements (some Spotify alarms carry a `<Content>`) aren't
arguments to either call, so a difference there can't be restored. It is
reported as a note, never a failure.

`plan` pairs alarms whose keys are equal first, whatever their IDs, so
restoring twice against one baseline does nothing the second time. Then a
baseline alarm whose ID is still there is updated, the rest are created, and
anything the baseline lacks is destroyed. Destroys go last: a restore that
stops partway leaves an extra alarm, not a missing one.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .. import play
from . import clock
from .model import Alarm, key, parse_alarms


_FIELDS = ("start_time", "recurrence", "duration", "enabled", "room_uuid",
           "program_uri", "program_metadata", "play_mode", "volume",
           "include_linked_zones")


def differences(want: Alarm, got: Alarm) -> list[str]:
    """The restorable fields that differ, by name."""
    return [f for f, w, g in zip(_FIELDS, key(want), key(got)) if w != g]


@dataclass
class AlarmSnapshot:
    taken_utc: str
    version: str
    xml: str                # CurrentAlarmList, as the speaker sent it

    @classmethod
    def of(cls, found: clock.AlarmList) -> "AlarmSnapshot":
        return cls(datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   found.version, found.xml)

    @property
    def alarms(self) -> list[Alarm]:
        return parse_alarms(self.xml)

    def to_dict(self) -> dict:
        return {"taken_utc": self.taken_utc, "version": self.version,
                "alarm_list": self.xml}

    @classmethod
    def from_dict(cls, d: dict) -> "AlarmSnapshot":
        return cls(d["taken_utc"], d["version"], d["alarm_list"])

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path

    @classmethod
    def load(cls, path: Path) -> "AlarmSnapshot":
        return cls.from_dict(json.loads(path.read_text()))


@dataclass
class Change:
    op: str                     # "create", "update" or "destroy"
    want: Alarm | None          # the baseline alarm (None for a destroy)
    have: Alarm | None          # the alarm now on the speaker (None for a create)
    fields: list[str] = field(default_factory=list)   # what an update changes

    def to_dict(self) -> dict:
        return {"op": self.op,
                "baseline_id": self.want.id if self.want else None,
                "current_id": self.have.id if self.have else None,
                "fields": self.fields,
                "alarm": (self.want or self.have).to_attributes()}


def plan(baseline: list[Alarm], current: list[Alarm]) -> list[Change]:
    """The writes that make `current` match `baseline`: creates and updates in
    baseline order, then destroys."""
    want, have = list(baseline), list(current)
    for w in list(want):               # equal keys pair up; same ID first
        k = key(w)
        h = (next((h for h in have if h.id == w.id and key(h) == k), None)
             or next((h for h in have if key(h) == k), None))
        if h is not None:
            want.remove(w)
            have.remove(h)
    changes = []
    for w in want:
        h = next((h for h in have if h.id == w.id), None)
        if h is None:
            changes.append(Change("create", w, None))
        else:
            have.remove(h)
            changes.append(Change("update", w, h, differences(w, h)))
    changes += [Change("destroy", None, h) for h in have]
    return changes


def leftovers(baseline: list[Alarm], current: list[Alarm]) -> tuple[list[Change], list[str]]:
    """What still differs (as the changes that would fix it), and notes on
    differences restore can't make: `extra` attributes and child elements."""
    left = plan(baseline, current)
    notes = []
    have = list(current)
    for w in baseline:
        h = next((h for h in have if key(h) == key(w)), None)
        if h is None:
            continue
        have.remove(h)
        if h.children != w.children:
            notes.append(f"alarm {h.id} (baseline {w.id}): its child elements differ "
                         "from the baseline; CreateAlarm/UpdateAlarm can't set them")
        odd = sorted(k for k in set(h.extra) | set(w.extra) if h.extra.get(k) != w.extra.get(k))
        if odd:
            notes.append(f"alarm {h.id} (baseline {w.id}): attributes {', '.join(odd)} "
                         "differ from the baseline; CreateAlarm/UpdateAlarm can't set them")
    return left, notes


@dataclass
class Restored:
    done: list[dict] = field(default_factory=list)     # each change made, with its result
    id_map: dict[str, str] = field(default_factory=dict)  # baseline ID -> new ID
    error: str = ""                                    # why it stopped early, if it did
    left: list[Change] = field(default_factory=list)   # still different afterwards
    notes: list[str] = field(default_factory=list)
    version: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.left


def restore(ip: str, snap: AlarmSnapshot, found: clock.AlarmList) -> Restored:
    """Make the household's alarms match `snap`, starting from `found` (the
    list as just read). Stops at the first failed write, including a refusal
    because someone else changed the list; then reads the list once more and
    reports whatever still differs. A write that failed but landed anyway
    still counts as done, and one that may have is marked `uncertain`. The ID
    map is journalled either way."""
    out = Restored()
    version = found.version
    changes = plan(snap.alarms, found.alarms)

    def did(c: Change, alarm_id: str | None, uncertain: bool = False) -> None:
        rec = c.to_dict()
        if c.op == "create" and alarm_id:
            out.id_map[c.want.id] = rec["new_id"] = alarm_id
        if uncertain:
            rec["uncertain"] = True
        out.done.append(rec)

    try:
        for c in changes:
            try:
                if c.op == "create":
                    made, now = clock.create_alarm(ip, c.want, version)
                    alarm_id = made.id
                elif c.op == "update":
                    _, now = clock.update_alarm(ip, replace(c.want, id=c.have.id), version)
                    alarm_id = c.have.id
                else:
                    now = clock.destroy_alarm(ip, c.have.id, version)
                    alarm_id = c.have.id
            except clock.AlarmWriteError as exc:
                if exc.landed is not False:
                    did(c, exc.alarm_id, uncertain=exc.landed is None)
                raise
            version = now.version
            did(c, alarm_id)
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
    try:
        final = clock.list_alarms(ip)
        out.version = final.version
        out.left, out.notes = leftovers(snap.alarms, final.alarms)
        _recover_ids(out, changes, found, final)
    except Exception as exc:
        out.error = (out.error + "; " if out.error else "") + \
            f"could not read the alarms back: {type(exc).__name__}: {exc}"
    play._journal("alarm_restore", ip, taken_utc=snap.taken_utc,
                  baseline_version=snap.version, changes=len(out.done),
                  id_map=out.id_map, left=len(out.left), error=out.error or None)
    return out


def _recover_ids(out: Restored, changes: list[Change], found: clock.AlarmList,
                 final: clock.AlarmList) -> None:
    """Map a create whose new ID never came back (its answer and its read-back
    both failed) from the final read: one new alarm equal to it is its copy."""
    for c in changes:
        if c.op != "create" or c.want.id in out.id_map:
            continue
        new = [a for a in final.alarms if found.get(a.id) is None
               and a.id not in out.id_map.values() and key(a) == key(c.want)]
        if len(new) != 1:
            continue
        out.id_map[c.want.id] = new[0].id
        rec = next((d for d in out.done if d["op"] == "create"
                    and d["baseline_id"] == c.want.id), None)
        if rec is None:
            out.done.append(rec := c.to_dict())
        rec |= {"new_id": new[0].id, "recovered": True}
