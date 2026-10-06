"""Turn collected state into findings, and findings into a hardware-vs-setup
verdict.

Each finding carries a severity and, where relevant, what would distinguish
a configuration cause from a failing unit.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import topology
from .devices import Device

# 2.4 GHz only products: these cannot be moved to 5 GHz, which constrains
# any "just use 5 GHz" advice.
BAND_24_ONLY = {"S12", "S14", "S1", "S3", "S5", "S9", "ZP80", "ZP90", "ZP100", "ZP120"}


@dataclass
class Finding:
    severity: str   # critical | warn | info
    title: str
    detail: str
    discriminator: str = ""   # how to tell hardware from setup, if applicable

    def render(self) -> str:
        mark = {"critical": "[!!]", "warn": "[! ]", "info": "[ i]"}[self.severity]
        out = f"{mark} {self.title}\n     {self.detail}"
        if self.discriminator:
            out += f"\n     -> {self.discriminator}"
        return out


def _freq_to_chan(freq: str) -> str:
    try:
        f = int(freq)
    except (TypeError, ValueError):
        return freq or "?"
    if 2412 <= f <= 2484:
        return f"2.4GHz ch{(f - 2407) // 5}"
    if 5000 < f < 6000:
        return f"5GHz ch{(f - 5000) // 5}"
    return str(f)


def analyse(topo: topology.Topology, devices: dict[str, Device],
            rf_rates: dict[str, dict] | None = None,
            ethernet_possible: bool = True) -> list[Finding]:
    """Findings for this household.

    ``ethernet_possible=False`` demotes the SonosNet advice: if the router
    cannot be reached with a cable, leading with "wire a speaker" is useless
    to the reader, so it becomes context and the actionable WiFi fixes are
    promoted instead.
    """
    findings: list[Finding] = []
    members = [m for m in topo.members]

    # ---- wired anchor / SonosNet -----------------------------------------
    wired = [m for m in members if m.eth_link == "1"]
    if not wired:
        findings.append(Finding(
            "critical" if ethernet_possible else "info",
            "No speaker is on Ethernet, so SonosNet is not running"
            + ("" if ethernet_possible else " (and a cable is not an option here)"),
            "Every speaker is a WiFi client of your router (EthLink=0 on all "
            f"{len(members)} units, and the radios report NO_SONOSNET_PEERS). "
            "Sonos is most reliable when at least one mains-powered unit is "
            "wired: that unit then forms SonosNet, a dedicated mesh the other "
            "speakers use for sync traffic instead of competing with every "
            "other device on your WiFi.",
            ("Setup issue, and the single highest-value fix. Wire the Beam to "
             "the router with Ethernet; the Roams will join SonosNet instead "
             "of the congested 2.4GHz band."
             if ethernet_possible else
             "Noted for completeness only -- you have said the router is out "
             "of cable reach, so the fixes below are the ones that apply. A "
             "MoCA/powerline adapter or moving the router would reopen this "
             "option, but nothing here depends on it."),
        ))

    # ---- band / channel --------------------------------------------------
    freqs = defaultdict(list)
    for m in members:
        freqs[m.channel_freq].append(m.zone_name or m.ip)
    if len(freqs) == 1:
        freq = next(iter(freqs))
        if freq and 2412 <= int(freq or 0) <= 2484:
            findings.append(Finding(
                "critical" if not ethernet_possible else "warn",
                f"Whole household is on one 2.4GHz channel ({_freq_to_chan(freq)})",
                "All sync and audio traffic shares a single crowded 20MHz "
                "channel. Stereo pairs are the most timing-sensitive thing "
                "Sonos does, so they are the first to break when that channel "
                "is contended.",
                "Setup issue. Check for neighbour networks on the same channel "
                "and move your router's 2.4GHz radio to whichever of ch 1 / 6 / "
                "11 is quietest.",
            ))

    # ---- firmware skew ---------------------------------------------------
    by_sw = defaultdict(list)
    for m in members:
        if m.software:
            by_sw[m.software].append(m)
    if len(by_sw) > 1:
        desc = "; ".join(
            f"{sw}: " + ", ".join(sorted({x.zone_name or x.ip for x in ms}))
            for sw, ms in sorted(by_sw.items())
        )
        below = []
        for gid, ms in topo.groups.items():
            coord = next((m for m in ms if m.coordinator_of), None)
            if not coord or not coord.min_compatible:
                continue
            for m in ms:
                if m.software and m.software < coord.min_compatible:
                    below.append(f"{m.zone_name or m.ip} ({m.software})")
        extra = ""
        if below:
            extra = (" Satellites below their coordinator's stated "
                     f"MinCompatibleVersion: {', '.join(below)}.")
        findings.append(Finding(
            "info",
            "Speakers are not all on the same software build",
            f"{desc}.{extra} Different hardware families can legitimately carry "
            "different build strings, and the household still reports the bond "
            "as intact, so treat this as context rather than a diagnosis.",
            "Worth confirming in the Sonos app that no product is flagged as "
            "needing an update.",
        ))

    # ---- stereo-paired portables ----------------------------------------
    for gid, ms in topo.groups.items():
        paired = [m for m in ms if m.chan_map]
        portables = [m for m in paired
                     if devices.get(m.ip) and devices[m.ip].model_number in {"S27", "S17", "S33"}]
        if len(paired) >= 2 and portables:
            names = ", ".join(sorted({m.zone_name or m.ip for m in paired}))
            findings.append(Finding(
                "warn",
                f"Battery-powered speakers are stereo-paired ({names})",
                "A stereo pair must keep the two speakers sample-aligned over "
                "the air. Portables make that harder: they use WiFi power "
                "saving, and they are the products most likely to be at the "
                "edge of coverage. This is the least robust Sonos "
                "configuration and the most common cause of one half "
                "disappearing.",
                "Setup issue if both units behave the same. Temporarily "
                "un-pair them into two separate rooms; if the dropouts stop, "
                "the pairing plus radio conditions were the cause, not the "
                "hardware.",
            ))

    # ---- per-unit RF asymmetry (measured, same-model only) --------------
    if rf_rates:
        findings.extend(_rf_findings(topo, devices, rf_rates))
    return findings


def tx_error_findings(links: dict[str, dict],
                      devices: dict[str, Device]) -> list[Finding]:
    """Findings from cumulative TX error rates.

    A transmit error is a frame the radio gave up on after retries. This is
    the most trustworthy cross-model number available here: it is cumulative
    since boot and counted the same way everywhere, unlike the PHY counter.
    A few percent is unremarkable on 2.4GHz; double digits is not.
    """
    out: list[Finding] = []
    usable = {ip: s for ip, s in links.items()
              if s.get("tx_error_pct") is not None and s.get("tx_packets", 0) > 1000}
    if not usable:
        return out

    def name(ip: str) -> str:
        dev = devices.get(ip)
        return (dev.zone_name if dev else "") or ip

    by_model: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for ip, s in usable.items():
        dev = devices.get(ip)
        by_model[dev.model_number if dev and dev.model_number else "?"].append((ip, s))

    ranked = sorted(usable.items(), key=lambda kv: -kv[1]["tx_error_pct"])
    out.append(Finding(
        "info", "WiFi transmit error rate since boot",
        "; ".join(f"{name(ip)} ({s['iface']}): {s['tx_error_pct']:.2f}% "
                  f"of {s['tx_packets']:,} frames" for ip, s in ranked) +
        ". A transmit error is a frame that was never acknowledged, even "
        "after retries. Rates are only compared within a model below, since "
        "different chipsets count differently.",
    ))

    worst_ip, worst = ranked[0]
    if worst["tx_error_pct"] >= 10.0:
        dev = devices.get(worst_ip)
        model = dev.model_number if dev and dev.model_number else "?"
        # Same-model peers only. A Beam's MediaTek driver and a Roam's Atheros
        # driver need not mean the same thing by "TX error".
        peers = [f"{name(ip)} {s['tx_error_pct']:.2f}%"
                 for ip, s in by_model.get(model, []) if ip != worst_ip]
        out.append(Finding(
            "critical",
            f"{name(worst_ip)} is failing to transmit "
            f"{worst['tx_error_pct']:.0f}% of its WiFi frames",
            f"{worst['tx_errors']:,} of {worst['tx_packets']:,} transmitted "
            f"frames on {worst['iface']} were never acknowledged"
            + (f", against {', '.join(peers)} on the identical unit. "
               if peers else ". ") +
            "Its receive side is clean, so this is specifically a problem "
            "getting data out. If this speaker coordinates a stereo pair or a "
            "group, the sync traffic the other speaker depends on is going "
            "through this same failing transmit path.",
            "These counters cannot yet separate a degraded transmitter from "
            "two speakers placed too far apart, and a coordinator transmits "
            "far more than its satellite, so some of the gap is expected. "
            "They are also cumulative since boot, averaging over every "
            "position the speaker has been in. SWAP the two units' physical "
            "positions, leave them a few hours, and re-run `scan`: if the high "
            "rate follows the serial number, that unit's radio is suspect; if "
            "it stays with the location, it is placement or distance.",
        ))
    return out


def _rf_findings(topo: topology.Topology, devices: dict[str, Device],
                 rf_rates: dict[str, dict]) -> list[Finding]:
    """Compare radio error rates, but only between identical models.

    Different Sonos hardware families use different WiFi chipsets and count
    PHY errors differently, so a Beam's number is not comparable with a
    Roam's. Two Roams, however, are directly comparable.
    """
    out: list[Finding] = []
    rates = {ip: r["phy_per_sec"] for ip, r in rf_rates.items()
             if isinstance(r, dict) and r.get("phy_per_sec") is not None}
    if not rates:
        return out

    ranked = sorted(rates.items(), key=lambda kv: -kv[1])
    listing = "; ".join(
        f"{(devices[ip].zone_name if ip in devices else ip) or ip}: {v:.0f}/s"
        for ip, v in ranked)
    out.append(Finding(
        "info", "Measured radio error rate (PHY errors/sec, same window)",
        listing + ". Only compare like with like -- different models count "
        "these differently.",
    ))

    by_model: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for ip, v in rates.items():
        dev = devices.get(ip)
        if dev and dev.model_number:
            by_model[dev.model_number].append((ip, v))

    for model, pairs in by_model.items():
        if len(pairs) < 2:
            continue
        pairs.sort(key=lambda kv: kv[1])
        (lo_ip, lo), (hi_ip, hi) = pairs[0], pairs[-1]
        name = lambda ip: (devices[ip].zone_name if ip in devices else ip) or ip
        if lo > 0 and hi / lo >= 3.0:
            out.append(Finding(
                "warn",
                f"Unequal radio conditions between two identical {model} units",
                f"{name(hi_ip)} ({hi_ip}) logs {hi:.0f} PHY errors/sec versus "
                f"{lo:.0f}/s for {name(lo_ip)} ({lo_ip}) -- {hi/lo:.1f}x more, "
                "on identical hardware.",
                "SWAP the two speakers' physical locations and re-measure. If "
                "the high rate follows the serial number, suspect that unit's "
                "radio; if it stays with the spot, it is placement.",
            ))
        else:
            out.append(Finding(
                "info",
                f"Both {model} units see comparable radio conditions",
                f"{name(lo_ip)} {lo:.0f}/s vs {name(hi_ip)} {hi:.0f}/s. "
                "No evidence that either unit's radio is worse than its twin, "
                "which argues against a hardware fault in one speaker.",
            ))
    return out


def _read_interventions(path: Path):
    """(points, closed spans, still-open spans) from the journal.

    A start with a `span_id` is paired with the end that names it, so two
    overlapping spans of one action on one speaker stay two. A record without
    one (older journals) pairs by action and speaker exactly as it always did:
    a second start replaces an unclosed first. An end without an id closes that
    kind first, else the latest id-ful open span of that action and speaker.
    Each open span is `{name, ip, span_id, start, max_s, pid}`; `max_s` is None for
    a record that predates it.
    """
    from datetime import datetime
    points: list[tuple[float, str, str]] = []
    spans: list[tuple[float, float, str, str]] = []
    by_id: dict[str, dict] = {}
    legacy: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return points, spans, []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec["ts"]).timestamp()
        except Exception:
            continue
        action, ip = rec.get("action", ""), rec.get("ip", "")
        if rec.get("span"):
            sid = rec.get("span_id")
            if action.endswith("_start"):
                st = {"name": action[:-6], "ip": ip, "span_id": sid,
                      "start": ts, "max_s": rec.get("max_s"), "pid": rec.get("pid")}
                if sid:
                    by_id[sid] = st
                else:
                    legacy[(st["name"], ip)] = st
            elif action.endswith("_end"):
                name = action[:-4]
                st = by_id.pop(sid, None) if sid else None
                if st is None:
                    st = legacy.pop((name, ip), None)
                if st is None and not sid:
                    later = [k for k, v in by_id.items() if v["name"] == name and v["ip"] == ip]
                    st = by_id.pop(later[-1]) if later else None
                if st is not None:
                    spans.append((st["start"], ts, name, ip))
            continue
        points.append((ts, action, ip))
    return points, spans, [*legacy.values(), *by_id.values()]


def _load_interventions(path: Path):
    """Our own state-changing activity: discrete calls, plus served spans.

    Returns (points, spans). A span is a window during which this machine was
    itself in the audio path, so everything inside it is confounded.
    """
    points, spans, open_spans = _read_interventions(path)
    # A span left open (crash, kill) ends where its start said it could, at
    # latest. One from before that was recorded has no bound: it covers
    # everything after its start, as it always did.
    for st in open_spans:
        end = st["start"] + st["max_s"] if st["max_s"] is not None else float("inf")
        spans.append((st["start"], end, st["name"], st["ip"]))
    return points, spans


def open_spans(path: Path) -> list[dict]:
    """The spans in the journal that never closed, for whatever opened them to
    close on its next start."""
    return _read_interventions(path)[2]


def _self_induced(ev_ts: str, interventions, window: float = 45.0):
    """Could this event have been caused by us?

    Two ways it could: a write of ours landed within `window` seconds, or the
    event fell inside a window where this machine was serving the audio. A
    stereo pair re-synchronises when its coordinator's transport changes, and
    a Mac in the audio path competes with the speaker's own traffic -- either
    makes a vanish look organic when it is not.
    """
    from datetime import datetime
    points, spans = interventions
    try:
        t = datetime.fromisoformat(ev_ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None
    for start, end, name, ip in spans:
        if start <= t <= end:
            return (0.0, f"during {name}", ip)
    # A point may carry a fourth field, the earliest event it can explain: an
    # alarm's fire can't explain a drop logged before its schedule was.
    near = [(abs(t - ts), act, ip) for ts, act, ip, *since in points
            if abs(t - ts) <= window and not (since and t < since[0])]
    if not near:
        return None
    near.sort()
    return near[0]


def _utc(ts: str) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _seconds(hms: str) -> int:
    """"HH:MM:SS" as seconds; 0 for an empty or unreadable one."""
    try:
        h, m, s = (int(x) for x in hms.split(":"))
    except (AttributeError, ValueError):
        return 0
    return h * 3600 + m * 60 + s


def _alarm_points(schedules: list[dict],
                  until: float) -> list[tuple[float, str, str, float]]:
    """Each enabled alarm's fire and duration-stop instants, as intervention
    points: (UTC seconds, "alarm HH:MM[ stop]", room, valid from).

    A schedule is valid from when it was first recorded (`valid_from`, which
    rotation checkpoints carry over) until the next one, so an alarm added,
    edited or disabled later never changes how earlier events are scored: it
    explains no event logged before it either. A fire belongs to the schedule
    in force at its fire time, and its stop goes with it even past the next
    schedule (disabling an alarm while it rings). StartTime is
    household-local; each schedule's `utc_offset_s` turns it into UTC. A DST
    change arrives as a new schedule with the new offset, valid from the last
    read before it (`offset_from`), since the clock changed somewhere between
    the two: a fire in that gap is tried under both offsets. A ONCE
    alarm can fire on any day: the speaker disables it after it fires, which
    is a new version and ends its segment.
    """
    from datetime import date, datetime, timedelta, timezone

    from .alarms.model import Recurrence

    by_start: dict[float, dict] = {}
    for rec in schedules:
        start = _utc(rec.get("valid_from", ""))
        if start is not None:
            by_start.setdefault(start, rec)
    starts = sorted(by_start)
    points: list[tuple[float, str, str, float]] = []
    for i, recorded in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else until
        rec = by_start[recorded]
        start = min(recorded, _utc(rec.get("offset_from") or "") or recorded)
        if end <= start:
            continue
        offset = timedelta(seconds=rec.get("utc_offset_s", 0))
        first = (datetime.fromtimestamp(start, timezone.utc) + offset).date()
        last = (datetime.fromtimestamp(end, timezone.utc) + offset).date()
        for a in rec.get("alarms", []):
            if not a.get("enabled"):
                continue
            try:
                days = Recurrence.parse(a.get("recurrence", "")).days or None
            except ValueError:
                continue
            at = _seconds(a.get("start_time", ""))
            label = f"alarm {a.get('start_time', '')[:5]}"
            room = a.get("room") or a.get("room_uuid", "?")
            stop = _seconds(a.get("duration", ""))
            day: date = first
            while day <= last:
                # Python's Monday 0 -> Sonos's Sunday 0; None = ONCE, any day.
                if days is None or (day.weekday() + 1) % 7 in days:
                    local = datetime.combine(day, datetime.min.time(), timezone.utc)
                    fire = (local - offset).timestamp() + at
                    if start <= fire < end:
                        points.append((fire, label, room, start))
                        if stop:
                            points.append((fire + stop, label + " stop", room, start))
                day += timedelta(days=1)
    return points


def _unreadable_alarms(schedules: list[dict]) -> list[dict]:
    """Each alarm any schedule couldn't read, once, with every reason seen
    (`reasons`) and its room as last logged. The same alarm is in every
    schedule (and checkpoint) until it's repaired, so one with an ID is
    that ID; alarms with no ID are told apart by how many one schedule holds."""
    seen: dict[tuple, dict] = {}
    for rec in schedules:
        counts: dict[tuple, int] = {}
        for b in rec.get("unreadable", []):
            if b.get("id"):
                k = ("id", b["id"])
            else:
                k = (None, b.get("room_uuid"), b.get("reason"))
                counts[k] = counts.get(k, 0) + 1
                k += (counts[k],)
            reasons = seen.get(k, {}).get("reasons", [])
            if b.get("reason", "?") not in reasons:
                reasons = reasons + [b.get("reason", "?")]
            seen[k] = b | {"reasons": reasons}
    return list(seen.values())


def rotated_logs(path: Path) -> list[Path]:
    """The log plus its rotated predecessors, oldest first.

    A daemon rotates as it runs, so the interesting event is often not in the
    current file. Rotation numbers ascend with age (``.1`` is the most recent
    predecessor), so read them in descending order, then the live file.
    """
    olds = []
    for q in path.parent.glob(path.name + ".*"):
        tail = q.name[len(path.name) + 1:]
        if tail.isdigit():
            olds.append((int(tail), q))
    files = [q for _, q in sorted(olds, reverse=True)]
    if path.exists():
        files.append(path)
    return files


def summarise_log(path: Path,
                  interventions_path: Path | None = None,
                  include_rotated: bool = True) -> list[Finding]:
    """Read a monitor log and report what actually happened.

    Events that coincide with our own writes are reported separately: they are
    not evidence about the speakers' health.
    """
    sources = rotated_logs(path) if include_rotated else (
        [path] if path.exists() else [])
    if not sources:
        return []
    interventions = _load_interventions(
        interventions_path or path.parent / "interventions.jsonl")
    vanish: dict[str, list[dict]] = defaultdict(list)
    induced: dict[str, list[dict]] = defaultdict(list)
    reboots: dict[str, list[dict]] = defaultdict(list)
    phy: dict[str, list[float]] = defaultdict(list)
    names: dict[str, str] = {}
    samples = 0

    lines: list[str] = []
    for src in sources:
        try:
            lines.extend(src.read_text().splitlines())
        except OSError:
            continue

    # Alarms are discounted like our own writes, whoever set them. Every
    # schedule is gathered before any event is scored: a vanish can come up to
    # the window's width before the fire time that explains it. Only vanishes
    # are scored, so the newest schedule need only run an hour past the last.
    schedules: list[dict] = []
    last_vanish = 0.0
    for line in lines:
        if '"alarm_schedule"' not in line and '"vanish"' not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("kind") == "alarm_schedule":
            schedules.append(rec)
        elif rec.get("kind") == "vanish":
            last_vanish = max(last_vanish, _utc(rec.get("ts", "")) or 0.0)
    if schedules:
        points, spans = interventions
        interventions = (points + _alarm_points(schedules, last_vanish + 3600), spans)

    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = rec.get("kind")
        if kind == "vanish":
            hit = _self_induced(rec.get("ts", ""), interventions)
            if hit:
                rec["_induced_by"] = (
                    f"{hit[1]} on {hit[2]}"
                    + (" (we were in the audio path)" if hit[1].startswith("during ")
                       else f", {hit[0]:.0f}s away"))
                induced[rec.get("ip", "?")].append(rec)
            else:
                vanish[rec.get("ip", "?")].append(rec)
            names[rec.get("ip", "?")] = rec.get("name", "")
        elif kind == "reboot":
            reboots[rec.get("ip", "?")].append(rec)
        elif kind == "sample":
            samples += 1
            for s in rec.get("samples", []):
                if s.get("phy_err_per_sec") is not None:
                    phy[s["ip"]].append(s["phy_err_per_sec"])
                    names[s["ip"]] = s.get("name", "")

    findings: list[Finding] = []
    # Before the samples check: a schedule alone still says what it can't discount.
    bad = _unreadable_alarms(schedules)
    if bad:
        named = "; ".join(
            f"{'alarm ' + b['id'] if b.get('id') else 'an alarm with no ID'}"
            f" ({b.get('room') or b.get('room_uuid') or 'no room'}): {', then '.join(b['reasons'])}"
            for b in bad)
        findings.append(Finding(
            "info", f"{len(bad)} alarm{'s' if len(bad) != 1 else ''} in the schedule "
                    "couldn't be read",
            f"{named}. Their fires and stops are not discounted, so a drop one "
            "of them caused would be scored as a fault. The other alarms are.",
        ))

    if not samples:
        return findings

    for ip, evs in sorted(vanish.items(), key=lambda kv: -len(kv[1])):
        reachable = sum(1 for e in evs if e.get("probe", {}).get("http_ok"))
        rebooted = len(reboots.get(ip, []))
        if rebooted:
            sev, verdict = "critical", (
                f"{rebooted} of these coincided with a BootSeq change, meaning "
                "the speaker actually restarted. Spontaneous restarts point at "
                "power or hardware, not the network.")
            disc = ("Hardware-leaning. If it restarts while on mains power, "
                    "raise it with Sonos support with the serial number.")
        elif reachable == len(evs):
            sev, verdict = "critical", (
                "In every case the speaker still answered ping and HTTP from "
                "this Mac while the household considered it gone. It did not "
                "lose power or crash -- it lost the Sonos sync heartbeat.")
            disc = ("Network-leaning: a reachable-but-absent speaker is an RF "
                    "or congestion problem, not a dead unit.")
        else:
            sev, verdict = "critical", (
                f"{reachable} of {len(evs)} drops were still reachable; the "
                "rest went fully off-network.")
            disc = "Mixed: measure again after wiring an anchor speaker."
        findings.append(Finding(
            sev,
            f"{names.get(ip, ip)} ({ip}) left the household {len(evs)}x "
            f"during monitoring",
            verdict, disc,
        ))

    for ip, evs in sorted(induced.items()):
        alarm = any(e["_induced_by"].startswith("alarm ") for e in evs)
        findings.append(Finding(
            "info",
            f"{names.get(ip, ip)} ({ip}) dropped {len(evs)}x right after "
            + ("an alarm or " if alarm else "") + "one of our own commands",
            "Discounted, not counted as a fault: "
            + "; ".join(e["_induced_by"] for e in evs) +
            ". Re-pointing a stereo pair's coordinator makes the pair "
            "re-synchronise, and the other half briefly leaves the household"
            + (" -- and an alarm going off or stopping re-points it too"
               if alarm else "") +
            ". Real-world evidence has to come from windows where nothing was "
            "sent to the speakers.",
        ))

    if not vanish and not induced:
        findings.append(Finding(
            "info", "No speaker left the household during this window",
            f"{samples} sample(s) with no vanish and no reboot. Useful as a "
            "baseline: whatever triggers the dropouts was not happening here.",
        ))

    if phy:
        ranked = sorted(((sum(v) / len(v), ip) for ip, v in phy.items()), reverse=True)
        lines = [f"{names.get(ip, ip)} ({ip}): {rate:.2f}/s" for rate, ip in ranked]
        findings.append(Finding(
            "info", "Radio error rate while monitoring (PHY errors/sec)",
            "; ".join(lines) +
            ". Compare the two halves of a stereo pair: identical units in "
            "similar spots should be within about 2x of each other.",
        ))
    return findings


def render(findings: list[Finding], title: str) -> str:
    if not findings:
        return f"{title}\n{'=' * len(title)}\n\nNothing notable found.\n"
    order = {"critical": 0, "warn": 1, "info": 2}
    findings = sorted(findings, key=lambda f: order[f.severity])
    body = "\n\n".join(f.render() for f in findings)
    return f"{title}\n{'=' * len(title)}\n\n{body}\n"


def diff_baselines(snapshots: list[dict], older: int = -2,
                   newer: int = -1) -> list[Finding]:
    """Compare two baseline snapshots to get a windowed error rate.

    Cumulative counters average over the speaker's whole uptime -- 200 days,
    for a portable that has been left on. Subtracting two snapshots gives the
    rate over just the interval between them, which is both current and free
    of the "averaged over every position it has ever been in" caveat.

    This is also how the swap test is read: take a snapshot, swap the
    speakers, wait, snapshot again. Serial numbers are recorded, so a unit is
    identifiable after it moves.
    """
    out: list[Finding] = []
    if len(snapshots) < 2:
        return out
    a, b = snapshots[older], snapshots[newer]

    rows: list[tuple[str, str, float, int, int, str]] = []
    for ip, new in b.get("speakers", {}).items():
        old = a.get("speakers", {}).get(ip)
        if not old or "error" in new or "error" in old:
            continue
        ln, lo = new.get("link", {}), old.get("link", {})
        # Models that expose no per-radio counters (the Play:1 surrounds here)
        # report bridge totals only, which carry no error count. Defaulting
        # those to zero would present "no data" as "no errors".
        if ln.get("bridge_only") or lo.get("bridge_only"):
            continue
        if any(d.get(k) is None for d in (ln, lo)
               for k in ("tx_packets", "tx_errors")):
            continue
        d_frames = ln["tx_packets"] - lo["tx_packets"]
        d_errors = ln["tx_errors"] - lo["tx_errors"]
        # A counter reset (reboot) makes the delta meaningless.
        if d_frames <= 0 or d_errors < 0:
            continue
        rows.append((new.get("zone_name") or ip, new.get("serial", ""),
                     100 * d_errors / d_frames, d_errors, d_frames,
                     new.get("model_number", "")))
    if not rows:
        return out

    rows.sort(key=lambda r: -r[2])
    window = f"{a.get('taken_utc', '?')} -> {b.get('taken_utc', '?')}"
    out.append(Finding(
        "info",
        "WiFi transmit errors over just the interval between two snapshots",
        f"Window: {window}. " + "; ".join(
            f"{name} ({serial}): {pct:.2f}% ({errs:,} of {frames:,} frames)"
            for name, serial, pct, errs, frames, _model in rows) +
        ". Unlike the cumulative figures this covers only this window, so it "
        "reflects current conditions rather than the speaker's whole uptime.",
    ))

    worst = rows[0]
    if worst[2] >= 5.0:
        # Only compare against the same model: different Sonos hardware
        # families use different WiFi chipsets and count errors differently,
        # so "796x worse than the Beam" would be a meaningless number.
        peers = [r for r in rows[1:] if r[5] and r[5] == worst[5]]
        if peers:
            best = min(peers, key=lambda r: r[2])
            ratio = worst[2] / max(best[2], 0.01)
            comparison = (f", against {best[2]:.2f}% for {best[0]} -- the "
                          f"identical model over the same interval, so "
                          f"{ratio:.0f}x worse")
        else:
            best, ratio = None, 0.0
            comparison = (" (no same-model peer in these snapshots to "
                          "compare against)")
        if best is None or ratio >= 3.0:
            out.append(Finding(
                "critical",
                f"{worst[0]} transmitted {worst[2]:.1f}% of its frames "
                "unacknowledged in this window",
                f"{worst[3]:,} of {worst[4]:,} frames failed{comparison}. "
                f"Measured fresh rather than accumulated. "
                f"Serial: {worst[1] or 'unknown'}.",
                "If this speaker coordinates a stereo pair, the other half "
                "depends on this transmit path. Swap the two units' positions "
                "and take another snapshot: a rate that follows the serial "
                "indicts the unit, one that stays with the room indicts the "
                "location.",
            ))
    return out
