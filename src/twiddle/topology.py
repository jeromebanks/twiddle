"""Parse the household's own view of itself: groups, bonds, vanished devices.

This is the authoritative source for "did the household lose a speaker",
which is a different question from "can I reach the speaker" -- the whole
point of the diagnosis is that those two answers can disagree.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict

from .devices import soap


@dataclass
class Member:
    uuid: str
    ip: str
    zone_name: str
    software: str
    min_compatible: str = ""
    boot_seq: str = ""
    channel_freq: str = ""
    wireless_mode: str = ""
    connection_type: str = ""
    eth_link: str = ""
    invisible: bool = False
    is_satellite: bool = False
    sat_chan_map: str = ""
    chan_map: str = ""
    more_info: str = ""
    coordinator_of: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Vanished:
    uuid: str
    zone_name: str
    reason: str
    model: str
    mac: str
    last_known_ip: str
    last_seen_utc: str
    more_info: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Topology:
    groups: dict[str, list[Member]] = field(default_factory=dict)
    vanished: list[Vanished] = field(default_factory=list)

    @property
    def members(self) -> list[Member]:
        return [m for ms in self.groups.values() for m in ms]

    def by_uuid(self, uuid: str) -> Member | None:
        return next((m for m in self.members if m.uuid == uuid), None)

    def to_dict(self) -> dict:
        return {
            "groups": {g: [m.to_dict() for m in ms] for g, ms in self.groups.items()},
            "vanished": [v.to_dict() for v in self.vanished],
        }


def _ip_of(location: str) -> str:
    m = re.search(r"http://([\d.]+):", location or "")
    return m.group(1) if m else ""


def _attrs(el: ET.Element) -> dict:
    return {k: v for k, v in el.attrib.items()}


def _member(el: ET.Element, satellite: bool, coord_group: str = "") -> Member:
    a = _attrs(el)
    return Member(
        uuid=a.get("UUID", ""),
        ip=_ip_of(a.get("Location", "")),
        zone_name=a.get("ZoneName", ""),
        software=a.get("SoftwareVersion", ""),
        min_compatible=a.get("MinCompatibleVersion", ""),
        boot_seq=a.get("BootSeq", ""),
        channel_freq=a.get("ChannelFreq", ""),
        wireless_mode=a.get("WirelessMode", ""),
        connection_type=a.get("ConnectionType", ""),
        eth_link=a.get("EthLink", ""),
        invisible=a.get("Invisible", "0") == "1",
        is_satellite=satellite,
        sat_chan_map=a.get("HTSatChanMapSet", ""),
        chan_map=a.get("ChannelMapSet", ""),
        more_info=a.get("MoreInfo", ""),
        coordinator_of=coord_group,
    )


def parse(state_xml: str) -> Topology:
    root = ET.fromstring(state_xml)
    topo = Topology()
    for group in root.iter("ZoneGroup"):
        gid = group.attrib.get("ID", "")
        coord = group.attrib.get("Coordinator", "")
        members: list[Member] = []
        for gm in group.iter("ZoneGroupMember"):
            m = _member(gm, satellite=False,
                        coord_group=gid if gm.attrib.get("UUID") == coord else "")
            members.append(m)
            for sat in gm.iter("Satellite"):
                members.append(_member(sat, satellite=True))
        topo.groups[gid] = members
    for d in root.iter("Device"):
        a = _attrs(d)
        topo.vanished.append(Vanished(
            uuid=a.get("UUID", ""),
            zone_name=a.get("ZoneName", ""),
            reason=a.get("Reason", ""),
            model=a.get("ModelInfo", ""),
            mac=a.get("Mac", ""),
            last_known_ip=a.get("LastKnownIP", ""),
            last_seen_utc=a.get("LastSeenUTC", ""),
            more_info=a.get("MoreInfo", ""),
        ))
    return topo


def fetch(ip: str) -> Topology:
    """Ask one speaker for the household topology (any member can answer)."""
    resp = soap(ip, "ZoneGroupTopology", "GetZoneGroupState")
    m = re.search(r"<ZoneGroupState>(.*)</ZoneGroupState>", resp, re.S)
    if not m:
        raise RuntimeError("no ZoneGroupState in response")
    return parse(html.unescape(m.group(1)))
