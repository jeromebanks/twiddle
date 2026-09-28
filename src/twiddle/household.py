"""An object model for the household: speakers, groups, and how to name one.

Written as a small library rather than CLI glue, because three different
surfaces want it -- the CLI, an eventual MCP server, and ad-hoc scripts.
Inspired by soco's shape, but hand-rolled: every write here routes through
``play`` so it lands in the intervention journal, and ``analyse`` only
discounts events it can see in that journal.

Two facts about this household drive the whole design, and both were measured
rather than assumed (see the module tests):

* **``Invisible`` is what marks a speaker as not independently addressable**,
  not ``is_satellite``. The Beam's surrounds appear as ``<Satellite>``
  elements, but a bonded *stereo pair* -- the Roams here -- appear as ordinary
  ``ZoneGroupMember`` entries carrying a ``ChannelMapSet``. So the right Roam
  has ``is_satellite=False`` and is still not a valid transport target.
  Routing on ``is_satellite`` alone silently sends commands into a void.

* **The coordinator comes from the group's ``Coordinator`` attribute, never
  from the group ID's prefix.** Those genuinely differ here: the Roam group is
  ``RINCON_...9FBA01400:2831555993`` while its coordinator is
  ``RINCON_...B9F401400``. The ID appears to keep whichever unit first formed
  the group.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import play, topology
from .devices import Device, discover, load_device


class ResolutionError(Exception):
    """Base for 'I could not turn that name into a speaker'."""


class NotFound(ResolutionError):
    def __init__(self, query: str, known: list[str]):
        self.query, self.known = query, known
        super().__init__(
            f"no speaker matches {query!r}. Known: " + ", ".join(known))


class Ambiguous(ResolutionError):
    def __init__(self, query: str, candidates: list[str]):
        self.query, self.candidates = query, candidates
        super().__init__(
            f"{query!r} matches more than one target: " + ", ".join(candidates))


def _norm(s: str) -> str:
    """Fold a name to something a human might plausibly type instead."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


@dataclass
class Speaker:
    """One physical unit, and whether it is a legitimate command target."""
    ip: str
    uuid: str = ""
    name: str = ""          # ZoneName, e.g. "Sonos Roam (L)"
    room: str = ""          # roomName, e.g. "Sonos Roam"
    model: str = ""
    model_number: str = ""
    is_satellite: bool = False
    invisible: bool = False
    group_id: str = ""
    is_coordinator: bool = False
    channel: str = ""       # LF / RF / LR / RR, when part of a bond

    @property
    def addressable(self) -> bool:
        """Can this unit take transport commands in its own right?

        Bonded units cannot: the household presents the bond as one zone, and
        commands sent to the follower are accepted and then ignored, which is
        indistinguishable from a dropout unless you know to expect it.
        """
        return not self.invisible

    @property
    def label(self) -> str:
        return f"{self.name or self.room or self.model} [{self.ip}]"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["addressable"] = self.addressable
        return d


@dataclass
class Group:
    """A set of speakers that share transport. Commands belong here."""
    gid: str
    coordinator: Speaker
    members: list[Speaker] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.coordinator.room or self.coordinator.name or self.gid

    @property
    def ip(self) -> str:
        return self.coordinator.ip

    def to_dict(self) -> dict:
        return {"gid": self.gid, "name": self.name,
                "coordinator": self.coordinator.to_dict(),
                "members": [m.to_dict() for m in self.members]}

    # -- reads ---------------------------------------------------------------

    def now_playing(self) -> dict:
        info = play.transport_info(self.ip)
        media = play.media_info(self.ip)
        uri = media.get("CurrentURI", "") or info.get("TrackURI", "")
        return {
            "group": self.name,
            "coordinator_ip": self.ip,
            "state": info.get("CurrentTransportState", "UNKNOWN"),
            "uri": uri,
            "is_stream": is_stream(uri, info),
            "track": info.get("Track"),
            "position": info.get("RelTime"),
            "duration": info.get("TrackDuration"),
            "volume": self.volume(),
        }

    def volume(self) -> dict[str, int]:
        """Per-unit volume for every member, keyed by IP.

        Reported per unit rather than as one number because a bond's members
        each answer GetVolume, and there is no guarantee they agree -- a
        mismatched pair is itself worth seeing.
        """
        out = {}
        for m in self.members:
            try:
                out[m.ip] = play.get_volume(m.ip)
            except Exception:
                out[m.ip] = -1
        return out

    # -- writes --------------------------------------------------------------

    def play(self) -> None:
        play.play(self.ip)

    def pause(self) -> None:
        play.pause(self.ip)

    def stop(self) -> None:
        play.stop(self.ip)

    def next(self) -> None:
        play.next_track(self.ip)

    def previous(self) -> None:
        play.previous_track(self.ip)

    def set_volume(self, volume: int, every_member: bool = True) -> dict[str, int]:
        """Set volume across the bond.

        Sonos propagates a coordinator volume change to its bonded partners,
        but not reliably to *grouped* zones, so by default set each member
        explicitly and report what each one ended up at.
        """
        targets = self.members if every_member else [self.coordinator]
        for m in targets:
            try:
                play.set_volume(m.ip, volume)
            except Exception:
                pass
        return self.volume()

    def set_mute(self, muted: bool) -> None:
        for m in self.members:
            try:
                play.set_mute(m.ip, muted)
            except Exception:
                pass

    # -- EQ --------------------------------------------------------------
    #
    # Bass, treble, loudness and balance are zone state, not per-driver --
    # confirmed live: both units of a bonded pair answer identically. Read
    # and write every member anyway, for the same reason `set_volume` does:
    # propagation to a bonded partner isn't guaranteed, and a mismatch is
    # itself worth seeing rather than papering over.

    def _eq_read(self, getter) -> dict:
        out = {}
        for m in self.members:
            try:
                out[m.ip] = getter(m.ip)
            except Exception:
                out[m.ip] = None
        return out

    def _eq_write(self, setter, value) -> None:
        for m in self.members:
            try:
                setter(m.ip, value)
            except Exception:
                pass

    def bass(self) -> dict[str, int]:
        return self._eq_read(play.get_bass)

    def set_bass(self, level: int) -> dict[str, int]:
        self._eq_write(play.set_bass, level)
        return self.bass()

    def treble(self) -> dict[str, int]:
        return self._eq_read(play.get_treble)

    def set_treble(self, level: int) -> dict[str, int]:
        self._eq_write(play.set_treble, level)
        return self.treble()

    def loudness(self) -> dict[str, bool]:
        return self._eq_read(play.get_loudness)

    def set_loudness(self, on: bool) -> dict[str, bool]:
        self._eq_write(play.set_loudness, on)
        return self.loudness()

    def balance(self) -> dict[str, int]:
        return self._eq_read(play.get_balance)

    def set_balance(self, level: int) -> dict[str, int]:
        self._eq_write(play.set_balance, level)
        return self.balance()

    # -- playback mode -----------------------------------------------------
    #
    # Unlike volume/EQ this is a queue property, not a per-driver one, so it
    # belongs to the coordinator alone -- no per-member loop.

    def shuffle(self) -> bool:
        on, _ = play.decode_play_mode(play.play_mode(self.ip))
        return on

    def set_shuffle(self, on: bool) -> bool:
        _, repeat = play.decode_play_mode(play.play_mode(self.ip))
        play.set_play_mode(self.ip, play.encode_play_mode(on, repeat))
        return self.shuffle()

    def repeat(self) -> str:
        _, repeat = play.decode_play_mode(play.play_mode(self.ip))
        return repeat

    def set_repeat(self, mode: str) -> str:
        on, _ = play.decode_play_mode(play.play_mode(self.ip))
        play.set_play_mode(self.ip, play.encode_play_mode(on, mode))
        return self.repeat()

    def play_radio(self, url: str, title: str = "Stream", art: str | None = None) -> None:
        play.play_radio(self.ip, url, title, art)

    def play_url(self, url: str, metadata: str = "") -> None:
        play.set_uri(self.ip, url, metadata)
        play.play(self.ip)

    def snapshot(self) -> "Snapshot":
        return Snapshot.capture(self)

    def restore(self, snap: "Snapshot") -> dict:
        return snap.restore(self)


# Sonos URI schemes that have no seekable position: restoring one means
# re-issuing the URI and pressing play, never seeking into it.
_STREAM_SCHEMES = ("x-rincon-mp3radio:", "x-sonosapi-stream:",
                   "x-sonosapi-radio:", "x-rincon-stream:", "x-sonosprog-http:")


def is_stream(uri: str, info: dict | None = None) -> bool:
    if any((uri or "").startswith(s) for s in _STREAM_SCHEMES):
        return True
    # A live stream reports no duration; a queued file always has one.
    dur = (info or {}).get("TrackDuration", "")
    return dur in ("0:00:00", "NOT_IMPLEMENTED") and bool(uri)


@dataclass
class Snapshot:
    """Enough state to put a group back exactly as it was found.

    The repo's working rule is to restore what you found, and the honest
    reason this exists is that the previous session did not: the Roams were
    left at volume 32 against a documented normal of 43. A human forgets that
    once a day; an agent would do it twenty times.
    """
    group: str = ""
    coordinator_ip: str = ""
    coordinator_uuid: str = ""
    volumes: dict[str, int] = field(default_factory=dict)
    mutes: dict[str, bool] = field(default_factory=dict)
    bass: dict[str, int] = field(default_factory=dict)
    treble: dict[str, int] = field(default_factory=dict)
    loudness: dict[str, bool] = field(default_factory=dict)
    balance: dict[str, int] = field(default_factory=dict)
    state: str = ""
    uri: str = ""
    metadata: str = ""
    play_mode: str = ""
    position: str = ""
    track: str = ""
    is_stream: bool = False
    taken_utc: str = ""

    @classmethod
    def capture(cls, group: Group) -> "Snapshot":
        from datetime import datetime, timezone
        info = play.transport_info(group.ip)
        media = play.media_info(group.ip)
        uri = media.get("CurrentURI", "") or info.get("TrackURI", "")
        mutes = {}
        for m in group.members:
            try:
                mutes[m.ip] = play.get_mute(m.ip)
            except Exception:
                pass
        return cls(
            group=group.name,
            coordinator_ip=group.ip,
            coordinator_uuid=group.coordinator.uuid,
            volumes=group.volume(),
            mutes=mutes,
            bass=group.bass(),
            treble=group.treble(),
            loudness=group.loudness(),
            balance=group.balance(),
            state=info.get("CurrentTransportState", ""),
            uri=uri,
            metadata=media.get("CurrentURIMetaData", ""),
            play_mode=play.play_mode(group.ip),
            position=info.get("RelTime", ""),
            track=info.get("Track", ""),
            is_stream=is_stream(uri, info),
            taken_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def load(cls, path: Path) -> "Snapshot":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def restore(self, group: Group) -> dict:
        """Put the group back. Returns what was actually done, step by step.

        Deliberately verbose in its return value: a restore that silently
        half-worked is worse than one that failed loudly, because the next
        measurement inherits the difference without anyone knowing.
        """
        done: list[str] = []
        problems: list[str] = []

        for ip, vol in self.volumes.items():
            if vol is None or vol < 0:
                continue
            try:
                play.set_volume(ip, vol)
                done.append(f"volume {ip}={vol}")
            except Exception as exc:
                problems.append(f"volume {ip}: {type(exc).__name__}")
        for ip, muted in self.mutes.items():
            try:
                play.set_mute(ip, muted)
            except Exception:
                pass
        for ip, level in self.bass.items():
            if level is None:
                continue
            try:
                play.set_bass(ip, level)
            except Exception:
                pass
        for ip, level in self.treble.items():
            if level is None:
                continue
            try:
                play.set_treble(ip, level)
            except Exception:
                pass
        for ip, on in self.loudness.items():
            if on is None:
                continue
            try:
                play.set_loudness(ip, on)
            except Exception:
                pass
        for ip, level in self.balance.items():
            if level is None:
                continue
            try:
                play.set_balance(ip, level)
            except Exception:
                pass

        if not self.uri:
            done.append("no source to restore")
            return {"restored": done, "problems": problems}

        try:
            play.set_uri(group.ip, self.uri, self.metadata)
            done.append(f"uri {self.uri[:60]}")
        except Exception as exc:
            problems.append(f"set_uri: {type(exc).__name__}")
            return {"restored": done, "problems": problems}

        if self.play_mode:
            try:
                play.set_play_mode(group.ip, self.play_mode)
            except Exception:
                pass

        # A stream has no position to return to; seeking into one is a UPnP
        # error, not a no-op, so branch rather than trying and swallowing.
        if not self.is_stream and self.position and self.position != "0:00:00":
            try:
                play.seek(group.ip, self.position)
                done.append(f"position {self.position}")
            except Exception as exc:
                problems.append(f"seek: {type(exc).__name__}")

        if self.state == "PLAYING":
            try:
                play.play(group.ip)
                done.append("playing")
            except Exception as exc:
                problems.append(f"play: {type(exc).__name__}")
        else:
            done.append(f"left {self.state or 'stopped'}")
        return {"restored": done, "problems": problems}


@dataclass
class Resolution:
    """The answer to 'what did you actually send this to, and why?'.

    An agent that names a bonded follower needs to be told that the command
    went somewhere else. Silence there produces confident, wrong retries.
    """
    requested: str
    group: Group
    matched: Speaker
    redirected: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        d = {"requested": self.requested,
             "acted_on": self.group.coordinator.label,
             "group": self.group.name}
        if self.redirected:
            d["redirected_from"] = self.matched.label
            d["reason"] = self.reason
        return d


class Household:
    """Every speaker, how they are grouped, and how to name one."""

    def __init__(self, speakers: list[Speaker], groups: list[Group],
                 topo: topology.Topology | None = None,
                 devices: dict[str, Device] | None = None):
        self.speakers = speakers
        self.groups = groups
        self.topology = topo
        self.devices = devices or {}

    # -- construction --------------------------------------------------------

    @classmethod
    def build(cls, topo: topology.Topology,
              devices: dict[str, Device]) -> "Household":
        speakers: list[Speaker] = []
        groups: list[Group] = []
        for gid, members in topo.groups.items():
            built: list[Speaker] = []
            for m in members:
                dev = devices.get(m.ip)
                sp = Speaker(
                    ip=m.ip,
                    uuid=m.uuid,
                    # Prefer the unit's own ZoneName: the topology reports
                    # both halves of a bond as plain "Sonos Roam", while the
                    # device itself says "Sonos Roam (L)" / "(R)" -- and that
                    # distinction is exactly what someone naming one will use.
                    name=(dev.zone_name if dev else "") or m.zone_name,
                    room=(dev.room if dev else "") or m.zone_name,
                    model=dev.model if dev else "",
                    model_number=dev.model_number if dev else "",
                    is_satellite=m.is_satellite,
                    invisible=m.invisible,
                    group_id=gid,
                    # The Coordinator attribute, not the group ID prefix.
                    is_coordinator=bool(m.coordinator_of),
                    channel=_channel_of(m),
                )
                built.append(sp)
                speakers.append(sp)
            coord = next((s for s in built if s.is_coordinator), None)
            if coord is None:
                # Should not happen, but a group with no coordinator would
                # otherwise vanish silently; fall back to a visible member.
                coord = next((s for s in built if s.addressable),
                             built[0] if built else None)
            if coord is not None:
                groups.append(Group(gid=gid, coordinator=coord, members=built))
        return cls(speakers, groups, topo, devices)

    @classmethod
    def load(cls, anchor: str | None = None) -> "Household":
        """Discover the household. `anchor` skips SSDP, which launchd needs,
        and so does a Chromebook's Linux container: it is NATed off the LAN,
        so multicast never arrives, though unicast to a speaker does.
        `TWIDDLE_ANCHOR` is the anchor for every command that doesn't pass one."""
        anchor = anchor or os.environ.get("TWIDDLE_ANCHOR") or None
        ips = [anchor] if anchor else discover()
        if not ips:
            raise RuntimeError(
                "no speakers answered discovery. If multicast is unavailable "
                "(e.g. under launchd, or on a Chromebook), pass an anchor IP "
                "or set TWIDDLE_ANCHOR.")
        topo = None
        for ip in ips:
            try:
                topo = topology.fetch(ip)
                break
            except Exception:
                continue
        if topo is None:
            raise RuntimeError(f"could not read topology from any of {ips}")
        all_ips = sorted({m.ip for m in topo.members if m.ip} | set(ips))
        devices = {}
        for ip in all_ips:
            try:
                devices[ip] = load_device(ip)
            except Exception:
                pass
        return cls.build(topo, devices)

    # -- naming --------------------------------------------------------------

    @property
    def names(self) -> list[str]:
        seen, out = set(), []
        for g in self.groups:
            for n in (g.name, g.coordinator.name):
                if n and n not in seen:
                    seen.add(n)
                    out.append(n)
        return out

    def group_of(self, speaker: Speaker) -> Group:
        for g in self.groups:
            if any(m.ip == speaker.ip for m in g.members):
                return g
        raise NotFound(speaker.ip, self.names)

    def resolve(self, query: str) -> Resolution:
        """Turn a human name or IP into the group that will act on it.

        Matching widens in stages -- exact, prefix, substring -- and stops at
        the first stage that produces a hit, so a room literally called "Roam"
        is never shadowed by "Sonos Roam (L)" merely containing the word.
        """
        q = (query or "").strip()
        if not q:
            raise NotFound(query, self.names)

        by_ip = next((s for s in self.speakers if s.ip == q), None)
        if by_ip is not None:
            return self._to_resolution(q, by_ip, "IP names a bonded follower")

        nq = _norm(q)
        stages = (
            lambda n: n == nq,
            lambda n: n.startswith(nq),
            lambda n: nq in n,
        )
        # Names and models are matched together within each stage, not in
        # two passes. Two passes look tidier and are wrong: "sonos" would
        # prefix-match the Roams by name, return them, and never notice it
        # also matches the Beam by model -- picking one of two plausible
        # targets in silence, which is the worst thing a resolver can do.
        # Staging still keeps an exact room name ahead of a model substring.
        for match in stages:
            hits = [s for s in self.speakers
                    if any(match(_norm(f)) for f in (s.name, s.room, s.model) if f)]
            if not hits:
                continue
            gids = {self.group_of(s).gid for s in hits}
            if len(gids) > 1:
                raise Ambiguous(q, sorted({self.group_of(s).name for s in hits}))
            # One group: prefer a visible member as "what you meant".
            best = next((h for h in hits if h.addressable), hits[0])
            return self._to_resolution(q, best, "named a bonded follower")
        raise NotFound(q, self.names)

    def _to_resolution(self, query: str, sp: Speaker, why: str) -> Resolution:
        group = self.group_of(sp)
        redirected = sp.ip != group.coordinator.ip
        return Resolution(
            requested=query, group=group, matched=sp,
            redirected=redirected,
            reason=(f"{sp.label} is not independently addressable "
                    f"({why}); commands go to the group coordinator"
                    if redirected else ""),
        )

    def to_dict(self) -> dict:
        return {"groups": [g.to_dict() for g in self.groups]}


def _channel_of(m: topology.Member) -> str:
    """Which channel this unit carries in a bond, if any.

    Both map attributes list every unit in the bond as ``UUID:CHANNELS``
    pairs, so find our own UUID rather than assuming a position.
    """
    for raw in (m.chan_map, m.sat_chan_map):
        if not raw or not m.uuid:
            continue
        for part in raw.split(";"):
            uuid, _, chans = part.partition(":")
            if uuid == m.uuid:
                # "LF,LF" means the same channel twice; report it once.
                seen = []
                for c in chans.split(","):
                    if c and c not in seen:
                        seen.append(c)
                return ",".join(seen)
    return ""
