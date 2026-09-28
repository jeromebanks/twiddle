"""Read-only access to Sonos per-device state.

Everything here talks directly to a ZonePlayer's HTTP port (1400) using the
same endpoints the Sonos app's diagnostics use. No writes, no side effects.
"""
from __future__ import annotations

import html
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Iterable

import requests

SSDP_ADDR = ("239.255.255.250", 1900)
PORT = 1400
TIMEOUT = 6

_M_SEARCH = "\r\n".join([
    "M-SEARCH * HTTP/1.1",
    f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}",
    'MAN: "ssdp:discover"',
    "MX: 2",
    "ST: urn:schemas-upnp-org:device:ZonePlayer:1",
    "", "",
])


def primary_ip() -> str:
    """This machine's address on the LAN, found without sending anything."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # reserved TEST-NET-1: never routed
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def discover(timeout: float = 4.0, strict: bool = False) -> list[str]:
    """Return IPs of every ZonePlayer answering SSDP, sorted numerically.

    Multicast is not always available: a process launched by launchd does not
    inherit the Local Network grant that a terminal session has, and the send
    then fails with EHOSTUNREACH. Callers that already know a speaker's
    address should treat an empty result as "discovery unavailable" rather
    than "no speakers", which is why failures are swallowed unless `strict`.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Pin multicast to the LAN interface; without this the kernel may pick a
    # route that cannot carry it.
    local = primary_ip()
    if local:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(local))
            sock.bind((local, 0))
        except OSError:
            pass
    sock.settimeout(timeout)
    found: set[str] = set()
    try:
        sock.sendto(_M_SEARCH.encode(), SSDP_ADDR)
        while True:
            _, addr = sock.recvfrom(65535)
            found.add(addr[0])
    except socket.timeout:
        pass
    except OSError:
        if strict:
            raise
    finally:
        sock.close()
    return sorted(found, key=lambda ip: tuple(int(p) for p in ip.split(".")))


def _get(ip: str, path: str) -> str:
    r = requests.get(f"http://{ip}:{PORT}/{path.lstrip('/')}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def soap(ip: str, service: str, action: str, body: str = "",
         path: str | None = None) -> str:
    """Invoke a UPnP action. Read-only actions only, unless a caller opts in."""
    urn = f"urn:schemas-upnp-org:service:{service}:1"
    endpoint = path or f"/{service}/Control"
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{urn}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    )
    r = requests.post(
        f"http://{ip}:{PORT}{endpoint}",
        data=envelope.encode(),
        headers={
            "SOAPACTION": f'"{urn}#{action}"',
            "Content-Type": 'text/xml; charset="utf-8"',
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.text


@dataclass
class Device:
    """One ZonePlayer as reported by itself."""
    ip: str
    uuid: str = ""
    room: str = ""
    zone_name: str = ""
    model: str = ""
    model_number: str = ""
    software: str = ""
    hardware: str = ""
    serial: str = ""
    mac: str = ""
    household: str = ""
    boot_seq: int | None = None
    # RF / link
    channel_freq: str = ""
    ieee_channel: str = ""
    wireless_mode: str = ""
    connection_type: str = ""
    eth_link: str = ""
    wifi_enabled: str = ""
    phy_errors: int | None = None   # errors SINCE LAST READ (counter resets)
    uptime_s: int | None = None
    wifi_flags: str = ""
    # portables
    battery: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.zone_name or self.room or self.model} [{self.ip}]"

    def to_dict(self) -> dict:
        return asdict(self)


def _text(root: ET.Element, tag: str) -> str:
    el = root.find(f".//{tag}")
    return (el.text or "").strip() if el is not None and el.text else ""


def load_device(ip: str) -> Device:
    """Fetch the full read-only state of one speaker."""
    dev = Device(ip=ip)

    try:
        root = ET.fromstring(_get(ip, "/status/zp"))
        dev.uuid = _text(root, "LocalUID")
        dev.zone_name = _text(root, "ZoneName")
        dev.software = _text(root, "SoftwareVersion")
        dev.hardware = _text(root, "HardwareVersion")
        dev.serial = _text(root, "SerialNumber")
        dev.mac = _text(root, "MACAddress")
        dev.household = _text(root, "HouseholdControlID")
    except Exception:
        pass

    try:
        desc = ET.fromstring(_get(ip, "/xml/device_description.xml"))
        ns = {"u": "urn:schemas-upnp-org:device-1-0"}
        dev.model = _text(desc, "{urn:schemas-upnp-org:device-1-0}modelName")
        dev.model_number = _text(desc, "{urn:schemas-upnp-org:device-1-0}modelNumber")
        dev.room = _text(desc, "{urn:schemas-upnp-org:device-1-0}roomName")
    except Exception:
        pass

    dev.phy_errors, dev.uptime_s, dev.ieee_channel = _wifi_driver(ip)
    dev.wifi_flags = _wireless_flags(ip)
    return dev


def _wifi_driver(ip: str) -> tuple[int | None, int | None, str]:
    """PHY errors since the previous read, driver uptime (seconds) and channel.

    Note the counter semantics: /proc/ath_rincon/status reports "PHY errors
    since last reading/reset" and zeroes itself on every read. So a single
    sample is only meaningful relative to whenever it was last polled -- use
    sample_phy_rate() to get an interval you actually control.
    """
    try:
        raw = html.unescape(_get(ip, "/status/proc/ath_rincon/status"))
    except Exception:
        return None, None, ""
    phy = re.search(r"PHY errors since last reading/reset:\s*(\d+)", raw)
    up = re.search(r"Debug info for .*? at (\d+)", raw)
    chan = re.search(r"IEEE channel:\s*(\d+)", raw)
    return (
        int(phy.group(1)) if phy else None,
        int(up.group(1)) if up else None,
        chan.group(1) if chan else "",
    )


def _wireless_flags(ip: str) -> str:
    try:
        raw = html.unescape(_get(ip, "/status/wireless"))
    except Exception:
        return ""
    stripped = re.sub(r"<[^>]+>", "\n", raw)
    words = [w.strip() for w in stripped.split("\n") if w.strip()]
    for w in words:
        if "SONOSNET" in w.upper() or "AUDIO_OUT" in w.upper():
            return w
    return ""


def sample_phy_rate(ips: Iterable[str], seconds: float = 20.0) -> dict[str, dict]:
    """Measure PHY errors per second over an interval we control.

    The driver's counter resets on read, so the first read is a throwaway that
    just zeroes it; the second read then covers exactly our own interval.
    All speakers are primed together so they share the same window.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    ips = list(ips)

    def prime(ip: str):
        try:
            _wifi_driver(ip)
            return time.monotonic()
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=len(ips) or 1) as pool:
        t0 = dict(zip(ips, pool.map(prime, ips)))

    time.sleep(seconds)

    def read(ip: str):
        try:
            phy, up, chan = _wifi_driver(ip)
            return time.monotonic(), phy, up, chan
        except Exception:
            return None, None, None, None

    with ThreadPoolExecutor(max_workers=len(ips) or 1) as pool:
        second = dict(zip(ips, pool.map(read, ips)))

    out: dict[str, dict] = {}
    for ip in ips:
        start, (t1, phy, up, chan) = t0.get(ip), second[ip]
        if start is None or t1 is None or phy is None:
            out[ip] = {"error": "unreadable"}
            continue
        window = t1 - start
        out[ip] = {
            "phy_errors": phy,
            "window_s": round(window, 2),
            "phy_per_sec": round(phy / window, 1) if window > 0 else None,
            "uptime_s": up,
            "ieee_channel": chan,
        }
    return out


_IFACE_RE = re.compile(
    r"^(\S+)\s+Link encap.*?"
    r"RX packets:(\d+) errors:(\d+) dropped:(\d+).*?"
    r"TX packets:(\d+) errors:(\d+) dropped:(\d+)",
    re.S | re.M,
)


def link_stats(ip: str) -> dict:
    """Cumulative driver counters for the interface carrying the uplink.

    Interface naming differs by hardware family -- Roams expose an Atheros
    ``ath0`` station interface, the Beam a MediaTek ``apcli0``, with the unused
    SonosNet AP interfaces sitting idle alongside. Rather than hard-code names,
    take whichever interface has actually moved packets.

    Unlike the PHY error counter these are cumulative since boot and mean the
    same thing on every model, so they are the safest cross-device comparison.
    TX errors are frames that were never acknowledged even after retries.
    """
    try:
        raw = html.unescape(_get(ip, "/status/ifconfig"))
    except Exception:
        return {"error": "unreadable"}
    raw = re.sub(r"<[^>]+>", "", raw)

    best: dict = {}
    bridge: dict = {}
    for m in _IFACE_RE.finditer(raw):
        name, rxp, rxe, rxd, txp, txe, txd = m.groups()
        if name.startswith("lo"):
            continue
        stats = {
            "iface": name,
            "rx_packets": int(rxp), "rx_errors": int(rxe), "rx_dropped": int(rxd),
            "tx_packets": int(txp), "tx_errors": int(txe), "tx_dropped": int(txd),
        }
        if name.startswith("br"):
            # The bridge aggregates every interface, so its error counts are
            # not radio errors. Keep it only as a fallback.
            if stats["rx_packets"] + stats["tx_packets"] > \
                    bridge.get("rx_packets", 0) + bridge.get("tx_packets", 0):
                bridge = stats
            continue
        if stats["tx_packets"] + stats["rx_packets"] > \
                best.get("tx_packets", 0) + best.get("rx_packets", 0):
            best = stats
    if not best:
        # Some models (the Play:1 surrounds here) do not expose per-radio
        # counters at all; say so rather than passing off bridge totals as
        # radio errors.
        if bridge:
            return {"error": "radio counters not exposed",
                    "bridge_only": True, "iface": bridge["iface"],
                    "rx_packets": bridge["rx_packets"],
                    "tx_packets": bridge["tx_packets"]}
        return {"error": "no interface found"}
    if best["tx_packets"]:
        best["tx_error_pct"] = round(100 * best["tx_errors"] / best["tx_packets"], 2)
    if best["rx_packets"]:
        best["rx_error_pct"] = round(100 * best["rx_errors"] / best["rx_packets"], 2)
    return best
