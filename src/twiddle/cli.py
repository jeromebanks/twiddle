from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import (alarm_cli, comedy_cli, control_cli, radio_cli, relay_cli, report, spotify_cli,
               stations_cli, topology)
from .devices import discover, link_stats, load_device, sample_phy_rate
from .monitor import Monitor, http_alive, ping_stats
from .dial import cli as dial_cli
from .viz import cli as viz_cli
from .scene import cli as scene_cli

DEFAULT_LOG = Path("logs/monitor.jsonl")


def _inventory(ips: list[str]):
    devices = {}
    for ip in ips:
        try:
            devices[ip] = load_device(ip)
        except Exception as exc:
            print(f"  ! {ip}: {type(exc).__name__}", file=sys.stderr)
    return devices


def _anchor(topo: topology.Topology, devices: dict) -> str:
    """Pick a mains-powered speaker to ask about the household."""
    portable = {"S27", "S17", "S33"}
    for m in topo.members:
        dev = devices.get(m.ip)
        if m.ip and dev and dev.model_number not in portable:
            return m.ip
    return next((m.ip for m in topo.members if m.ip), "")


def cmd_scan(args):
    print("Discovering speakers...")
    ips = discover()
    if not ips:
        print("No Sonos speakers answered. Are you on the same network?")
        return 1
    topo = topology.fetch(_anchor_from_ips(ips))
    # Trust the household roster over SSDP, then verify each member ourselves.
    all_ips = sorted({m.ip for m in topo.members if m.ip} | set(ips))
    devices = _inventory(all_ips)
    print(f"\nMeasuring radio error rates over {args.rf_window:.0f}s...")
    rf = sample_phy_rate(all_ips, args.rf_window)
    print(f"\n{len(all_ips)} speaker(s) in household; {len(ips)} answered SSDP.\n")
    for gid, ms in topo.groups.items():
        coord = next((m for m in ms if m.coordinator_of), None)
        head = coord.zone_name if coord else "?"
        print(f"GROUP  {head}")
        for m in ms:
            dev = devices.get(m.ip)
            role = "satellite" if m.is_satellite else ("coordinator" if m.coordinator_of else "member")
            model = dev.model if dev else "?"
            rate = ""
            r = rf.get(m.ip, {})
            if r.get("phy_per_sec") is not None:
                rate = f" phy={r['phy_per_sec']:.0f}/s"
            # SSDP is lossy multicast, so confirm reachability with a real request
            reach = "" if http_alive(m.ip).get("http_ok") else "  <-- NOT REACHABLE"
            print(f"   {role:12} {model:16} {m.ip:14} sw={m.software:14} "
                  f"boot={m.boot_seq:>3}{rate}{reach}")
            if m.more_info:
                print(f"                {m.more_info}")
        print()

    if topo.vanished:
        print("VANISHED (household thinks these are gone):")
        for v in topo.vanished:
            probe = http_alive(v.last_known_ip)
            state = "still reachable!" if probe.get("http_ok") else "unreachable"
            print(f"   {v.zone_name} {v.model} {v.last_known_ip} "
                  f"reason={v.reason} last_seen={v.last_seen_utc}  [{state}]")
        print()

    links = {ip: link_stats(ip) for ip in all_ips}
    findings = report.analyse(topo, devices, rf,
                              ethernet_possible=not args.no_ethernet)
    findings += report.tx_error_findings(links, devices)
    print(report.render(findings, "Findings"))
    return 0


def _anchor_from_ips(ips: list[str]) -> str:
    for ip in ips:
        try:
            topology.fetch(ip)
            return ip
        except Exception:
            continue
    raise SystemExit("could not read topology from any speaker")


def cmd_baseline_diff(args):
    """Compare two recorded snapshots."""
    import json
    path = Path(args.out)
    if not path.exists():
        print(f"No snapshots at {path}. Run `twiddle baseline` first.")
        return 1
    snaps = json.loads(path.read_text())
    if not isinstance(snaps, list):
        snaps = [snaps]
    if len(snaps) < 2:
        print(f"Only {len(snaps)} snapshot(s) in {path}; need two to compare.")
        print("Take another with: uv run twiddle baseline --label '<what changed>'")
        return 1
    print(f"{len(snaps)} snapshots in {path}:")
    for i, s in enumerate(snaps):
        print(f"  [{i}] {s.get('taken_utc','?')}  {s.get('label','') or '(no label)'}")
    print()
    findings = report.diff_baselines(snaps, args.older, args.newer)
    print(report.render(findings, "Change between snapshots "
                                  f"[{args.older}] and [{args.newer}]"))
    return 0


def cmd_baseline(args):
    """Snapshot every speaker's counters, stamped, for later comparison.

    The swap test needs a documented "before". Cumulative counters only mean
    something against an earlier reading of the same counter, and serial
    numbers are recorded so a swapped speaker can still be identified by unit
    rather than by position.
    """
    import json
    from datetime import datetime, timezone

    ips = discover()
    topo = topology.fetch(_anchor_from_ips(ips))
    all_ips = sorted({m.ip for m in topo.members if m.ip} | set(ips))
    print(f"Measuring {len(all_ips)} speaker(s) over {args.rf_window:.0f}s...")
    rf = sample_phy_rate(all_ips, args.rf_window)

    snap = {"taken_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "label": args.label, "speakers": {}}
    for ip in all_ips:
        try:
            dev = load_device(ip)
        except Exception as exc:
            snap["speakers"][ip] = {"error": type(exc).__name__}
            continue
        m = next((x for x in topo.members if x.ip == ip), None)
        snap["speakers"][ip] = {
            "zone_name": dev.zone_name, "model": dev.model,
            "model_number": dev.model_number,
            # Serial identifies the unit even after a physical swap.
            "serial": dev.serial, "mac": dev.mac,
            "software": dev.software, "uptime_s": dev.uptime_s,
            "boot_seq": m.boot_seq if m else None,
            "is_satellite": m.is_satellite if m else None,
            "coordinator": bool(m.coordinator_of) if m else None,
            "channel_freq": m.channel_freq if m else None,
            "link": link_stats(ip),
            "phy": rf.get(ip, {}),
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if out.exists():
        try:
            history = json.loads(out.read_text())
        except Exception:
            history = []
    if not isinstance(history, list):
        history = [history]
    history.append(snap)
    out.write_text(json.dumps(history, indent=2))

    print(f"\n{'speaker':22} {'serial':22} {'TX err%':>8} {'TX frames':>12} {'up (h)':>8}")
    for ip, s in snap["speakers"].items():
        if "error" in s:
            print(f"{ip:22} ERROR {s['error']}")
            continue
        link = s["link"]
        pct = link.get("tx_error_pct")
        up = s["uptime_s"] / 3600 if s.get("uptime_s") else 0
        print(f"{(s['zone_name'] or ip)[:20]:22} {s['serial'][:20]:22} "
              f"{(f'{pct:.2f}%' if pct is not None else 'n/a'):>8} "
              f"{link.get('tx_packets', 0):12,} {up:8.1f}")
    print(f"\nAppended snapshot {len(history)} to {out}")
    print("After swapping two speakers, run this again and compare: a rate "
          "that\nfollows the serial is the unit; one that stays put is the "
          "location.")
    return 0


def cmd_ping(args):
    ips = discover() if not args.ip else args.ip
    print(f"{'speaker':28} {'loss':>6} {'min':>8} {'avg':>8} {'max':>8} {'jitter':>8}")
    for ip in ips:
        try:
            name = load_device(ip).zone_name or ip
        except Exception:
            name = ip
        s = ping_stats(ip, count=args.count)
        print(f"{name[:26]:28} {s.get('loss_pct', 100):5.0f}% "
              f"{s.get('rtt_min', float('nan')):8.1f} {s.get('rtt_avg', float('nan')):8.1f} "
              f"{s.get('rtt_max', float('nan')):8.1f} {s.get('rtt_stddev', float('nan')):8.1f}")
    return 0


def cmd_watch(args):
    # An explicit anchor makes multicast optional, which matters under
    # launchd: that context has no Local Network grant, so SSDP fails while
    # ordinary unicast HTTP to a known speaker still works.
    if args.anchor:
        anchor = args.anchor
        topology.fetch(anchor)     # fail fast if it is not a live speaker
    else:
        ips = discover()
        if not ips:
            print("No speakers found. If multicast is unavailable (e.g. under "
                  "launchd), pass --anchor <speaker-ip>.")
            return 1
        devices = _inventory(ips)
        anchor = _anchor(topology.fetch(_anchor_from_ips(ips)), devices)
    log = Path(args.log)
    print(f"Watching household via {anchor}, logging to {log}")
    if args.duration <= 0:
        print("Running until stopped (Ctrl-C).\n")
    else:
        print("Leave music playing. Ctrl-C to stop early.\n")
    Monitor(anchor, log, interval=args.interval,
            max_bytes=args.max_bytes, keep=args.keep,
            quiet=args.quiet).run(args.duration * 60)
    print("\n" + report.render(report.summarise_log(log), "What happened while watching"))
    return 0


def cmd_daemon(args):
    """Install, remove or inspect the background monitor."""
    from . import daemon

    project = Path(__file__).resolve().parents[2]
    if args.action == "status":
        print(f"launchd job {daemon.LABEL}: {daemon.status()}")
        print(f"plist: {daemon.plist_path()}")
        log = Path(args.log)
        if log.exists():
            n = sum(1 for _ in log.open())
            fresh = daemon.log_is_fresh(log, args.interval)
            print(f"log:   {log} ({n} records, "
                  f"{'updating' if fresh else 'STALE'})")
        if daemon.blocked_by_local_network(project, log, args.interval):
            print(daemon.LOCAL_NETWORK_HINT)
            return 1
        return 0
    if args.action == "uninstall":
        removed = daemon.uninstall()
        print("Removed." if removed else "Was not installed.")
        return 0

    log = Path(args.log).resolve()
    anchor = args.anchor or daemon.pick_anchor()
    if not anchor:
        print("Could not find a speaker to anchor to. Pass --anchor <ip>.")
        return 1
    path = daemon.install(project, args.interval, log, args.max_bytes, anchor)
    print(f"Installed {path}")
    print(f"  anchored to {anchor} (mains-powered speaker, survives Roam sleep)")
    print(f"  watching every {args.interval:.0f}s, logging to {log}")
    print(f"  rotating past {args.max_bytes / 1_000_000:.0f}MB, "
          f"restarted by launchd if it exits")
    print(f"  status: {daemon.status()}")

    # Give it a moment to either start logging or fail on permissions.
    time.sleep(15)
    if daemon.blocked_by_local_network(project, log, args.interval):
        print("\n!! The agent started but cannot reach the LAN.")
        print(daemon.LOCAL_NETWORK_HINT)
        return 1

    print("\nIt is running now and will start again at login.")
    print("When a dropout happens, ask it what it saw:")
    print(f"  uv run twiddle analyse --log {log}")
    print("\nTo stop it:  uv run twiddle daemon uninstall")
    return 0


def cmd_analyse(args):
    log = Path(args.log)
    sources = report.rotated_logs(log) if not args.current_only else [log]
    findings = report.summarise_log(log, include_rotated=not args.current_only)
    title = f"Monitor log: {log}"
    if len(sources) > 1:
        title += f"  (+{len(sources) - 1} rotated)"
    print(report.render(findings, title))
    return 0


def cmd_serve(args):
    from . import play
    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 1
    tracks = play.find_audio(root)
    if not tracks:
        print(f"No audio files under {root}")
        return 1
    ips = discover()
    target = args.ip or _room_ip(args) or _pick_target(ips)
    if not target:
        return 1

    srv = play.FileServer(root)
    port = srv.start()
    print(f"Serving {len(tracks)} track(s) from {root} on port {port}")

    dev = load_device(target)
    if args.volume is not None:
        play.set_volume(target, args.volume)
    print(f"Target: {dev.zone_name or target} (volume {play.get_volume(target)})")

    play.clear_queue(target)
    for t in tracks[: args.limit]:
        play.add_to_queue(target, srv.url_for(t, target))
    play.play_queue(target, dev.uuid)
    print(f"Queued {min(len(tracks), args.limit)} track(s) and started playback.")
    print("This path is: Mac -> your LAN -> speaker. No Spotify, no Sonos cloud.")
    print("Ctrl-C to stop serving (playback stops when the server goes away).\n")

    try:
        while True:
            info = play.transport_info(target)
            print(f"  {info.get('CurrentTransportState','?'):10} "
                  f"track {info.get('Track','?')} {info.get('RelTime','')}   ", end="\r")
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopping.")
        if args.stop_on_exit:
            play.stop(target)
        srv.stop()
    return 0


def cmd_radio(args):
    """Play an internet stream, optionally watching it for drops.

    The speaker pulls this stream from the internet itself, so this Mac is not
    in the audio path. That makes it the fair comparison against Spotify: same
    delivery shape, same bitrate class, no local server to confound it.
    """
    import json
    from datetime import datetime, timezone

    from . import play

    ips = discover()
    target = args.ip or _room_ip(args) or _pick_target(ips)
    if not target:
        return 1
    if args.volume is not None:
        play.set_volume(target, args.volume)
    play.play_radio(target, args.url, args.title)
    print(f"Started {args.url}")
    print(f"  on {target}: {play.transport_info(target)}")

    if not args.watch:
        return 0

    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = log.open("a")

    def emit(rec):
        rec["ts"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        fh.write(json.dumps(rec) + "\n")
        fh.flush()

    emit({"kind": "stream_start", "url": args.url, "target": target,
          "title": args.title})
    gaps = unknown = 0
    # Until the first PLAYING sample, TRANSITIONING just means "buffering".
    started = False
    print(f"\nWatching for {args.watch:.0f} min. Nothing is served from this "
          "Mac, so the audio path is speaker <- internet.\n")
    deadline = time.monotonic() + args.watch * 60
    try:
        while time.monotonic() < deadline:
            info = play.transport_info(target)
            state = info.get("CurrentTransportState", "?")
            rec = {"kind": "stream_sample", "state": state,
                   "rel": info.get("RelTime"),
                   "uri": info.get("TrackURI"),
                   "status": info.get("CurrentTransportStatus")}
            if "error" in info or state == "?":
                unknown += 1
                rec |= {"kind": "stream_unknown", "why": info.get("error", "")}
                print(f"\n  ?? could not read state ({info.get('error','')})")
            elif state == "TRANSITIONING" and not started:
                rec["kind"] = "stream_buffering"
            elif state != "PLAYING":
                gaps += 1
                rec["kind"] = "stream_gap"
                print(f"\n  !! {state} -- stream not playing")
            else:
                started = True
            emit(rec)
            mins = (deadline - time.monotonic()) / 60
            print(f"  {state:10} {info.get('RelTime',''):>10} | gaps {gaps} "
                  f"unread {unknown} | {mins:4.1f} min left     ", end="\r")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        emit({"kind": "stream_end", "gaps": gaps, "unknown": unknown})
        fh.close()

    print("\n\n" + "=" * 62)
    kbps = args.title
    if gaps == 0:
        print(f"No playback gap in {args.watch:.0f} min on an internet-pulled "
              "stream.")
        print("The speaker sustained this bitrate with no local server in the")
        print("path. Compare against how Spotify behaves over the same span.")
    else:
        print(f"{gaps} gap(s) in {args.watch:.0f} min on a plain internet "
              "stream.")
        print("Spotify is not implicated: this path never touched it.")
    print("=" * 62)
    print(f"\nDetail: {log}")
    return 0



def cmd_soak(args):
    """Play a purely local file for a long stretch and record every gap.

    This is the control condition: the audio comes off this Mac over the LAN,
    so Spotify, the Sonos cloud queue and the Connect handoff are all out of
    the path. A drop here cannot be blamed on a music service.
    """
    import json
    from datetime import datetime, timezone

    from . import play
    from .monitor import http_alive, ping_stats
    from .tone import write_soak_wav

    ips = discover()
    target = args.ip or _room_ip(args) or _pick_target(ips)
    if not target:
        return 1

    media = Path(args.file) if args.file else Path("media/soak_3min.wav")
    if not media.exists():
        print(f"Generating {media} ...")
        write_soak_wav(media, minutes=args.clip_minutes)

    dev = load_device(target)
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = log.open("a")

    def emit(rec):
        rec["ts"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        fh.write(json.dumps(rec) + "\n")
        fh.flush()

    srv = play.FileServer(media.parent)
    port = srv.start()
    url = srv.url_for(media, target)
    reps = max(1, int(args.duration / args.clip_minutes) + 1)

    prev_vol = play.get_volume(target)
    print(f"Target   : {dev.zone_name or target} ({target})")
    print(f"Source   : {url}")
    print(f"Plan     : {reps} x {args.clip_minutes:.0f} min clip "
          f"(~{args.duration:.0f} min), volume {args.volume}")
    print(f"Previous volume was {prev_vol}; restoring it on exit.\n")

    emit({"kind": "soak_start", "target": target, "name": dev.zone_name,
          "url": url, "reps": reps, "duration_min": args.duration,
          "prev_volume": prev_vol})

    # From here until we stop, this Mac is part of the audio path.
    play.journal_span("serving_start", target, url=url)
    play.set_volume(target, args.volume)
    play.clear_queue(target)
    for _ in range(reps):
        play.add_to_queue(target, url)
    play.play_queue(target, dev.uuid)

    gaps = 0
    stalls = 0
    unknown = 0
    started = False
    last_rel = None
    # A speaker we are NOT streaming to, so loss numbers mean something.
    control_ip = next((ip for ip in ips if ip != target), None)
    if control_ip:
        print(f"Control  : pinging {control_ip} (not in the audio path)")
    deadline = time.monotonic() + args.duration * 60
    try:
        while time.monotonic() < deadline:
            info = play.transport_info(target)
            state = info.get("CurrentTransportState", "?")
            rel = info.get("RelTime", "")
            rec = {"kind": "soak_sample", "state": state,
                   "track": info.get("Track"), "rel": rel,
                   "status": info.get("CurrentTransportStatus")}
            if "error" in info or state == "?":
                # We could not ask the speaker. That is a measurement failure,
                # not evidence that playback stopped -- keep them apart.
                unknown += 1
                rec |= {"kind": "soak_unknown", "why": info.get("error", "no state")}
                print(f"\n  ?? could not read transport state ({info.get('error','')})")
            elif state == "TRANSITIONING" and not started:
                rec |= {"kind": "soak_buffering"}
            elif state != "PLAYING":
                gaps += 1
                rec |= {"kind": "soak_gap"}
                # Deliberately no ping here: this Mac is serving the audio, so
                # probing the same path would measure our own traffic.
                if control_ip:
                    rec["control"] = ping_stats(control_ip, count=3)
                print(f"\n  !! {state} at {rel} -- playback not running")
            elif rel and rel == last_rel and started:
                stalls += 1
                rec |= {"kind": "soak_stall"}
                print(f"\n  !! position stuck at {rel} -- stalled")
            else:
                started = True
            emit(rec)
            last_rel = rel
            mins = (deadline - time.monotonic()) / 60
            print(f"  {state:10} track {info.get('Track','?'):>3} {rel:>10} "
                  f"| gaps {gaps} stalls {stalls} unread {unknown} "
                  f"| {mins:4.1f} min left   ",
                  end="\r")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        emit({"kind": "soak_end", "gaps": gaps, "stalls": stalls,
              "unknown": unknown})
        play.stop(target)
        play.journal_span("serving_end", target)
        play.set_volume(target, prev_vol)
        srv.stop()
        fh.close()

    print("\n\n" + "=" * 60)
    print(f"({unknown} sample(s) could not be read -- not counted as gaps.)")
    if gaps == 0 and stalls == 0:
        print(f"CONTROL PASSED: {args.duration:.0f} min of locally served audio "
              "with no gap.")
        print("A local file over your LAN is solid. That points away from the")
        print("speaker hardware and toward whatever is different about the")
        print("streaming path (service, cloud queue, or handoff).")
    else:
        print(f"CONTROL FAILED: {gaps} gap(s), {stalls} stall(s) on a purely "
              "local source.")
        print("Spotify is not the cause -- this path never touched it. The")
        print("problem is the network or the speakers themselves.")
    print("=" * 60)
    print(f"\nDetail: {log}")
    return 0


def _room_ip(args) -> str | None:
    """Turn --room into the coordinator IP these older commands expect.

    They predate the household model and address speakers by IP. Rather than
    rewrite them mid-investigation, give them the same naming as everything
    else and let resolution hand back a coordinator -- which also stops a
    soak test being aimed at a bonded follower that would never play it.
    """
    room = getattr(args, "room", None)
    if not room:
        return None
    from .household import Household
    try:
        res = Household.load().resolve(room)
    except Exception as exc:
        print(f"could not resolve --room {room!r}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if res.redirected:
        print(f"note: {res.reason}", file=sys.stderr)
    return res.group.ip


def _pick_target(ips: list[str]) -> str | None:
    print("Pick a speaker with --ip. Available:")
    for ip in ips:
        try:
            d = load_device(ip)
            print(f"   {ip:14} {d.zone_name or d.room} ({d.model})")
        except Exception:
            print(f"   {ip:14} ?")
    return None


def _add_diag_parsers(sub, hidden: bool = False):
    """Build the diagnostic subcommands.

    Called twice: once under `twiddle diag ...`, which is where they belong
    now, and once at the top level with help suppressed. The aliases are not
    decoration -- the installed launchd agent invokes `twiddle watch
    --duration 0 ...` by absolute argv, and `tools/` and docs/GUIDE.md use the
    old spellings too. Renaming without them would silently stop the daemon
    that is the best evidence source in this repo.
    """
    def add(name, **kw):
        # Omitting `help` entirely is what hides an alias: argparse renders
        # help=SUPPRESS literally as "==SUPPRESS==" rather than skipping it.
        if hidden:
            kw.pop("help", None)
        return sub.add_parser(name, **kw)

    s = add("scan", help="inventory the household and report findings")
    s.add_argument("--rf-window", type=float, default=20,
                   help="seconds to measure PHY error rates over")
    s.add_argument("--no-ethernet", action="store_true",
                   help="router is out of cable reach; rank WiFi fixes first")
    s.set_defaults(func=cmd_scan)

    s = add("baseline", help="stamped snapshot of counters, for the swap test")
    s.add_argument("--rf-window", type=float, default=20)
    s.add_argument("--label", default="", help="e.g. 'before swap'")
    s.add_argument("--out", default="logs/baseline.json")
    s.set_defaults(func=cmd_baseline)

    s = add("baseline-diff", help="compare two baseline snapshots (the swap test)")
    s.add_argument("--out", default="logs/baseline.json")
    s.add_argument("--older", type=int, default=-2,
                   help="index of the earlier snapshot (default -2)")
    s.add_argument("--newer", type=int, default=-1,
                   help="index of the later snapshot (default -1)")
    s.set_defaults(func=cmd_baseline_diff)

    s = add("ping", help="latency/jitter to each speaker")
    s.add_argument("--ip", nargs="*", default=None)
    s.add_argument("--count", type=int, default=10)
    s.set_defaults(func=cmd_ping)

    s = add("watch", help="monitor for dropouts and explain them")
    s.add_argument("--duration", type=float, default=30, help="minutes")
    s.add_argument("--interval", type=float, default=10,
                   help="seconds between samples")
    s.add_argument("--anchor", default=None,
                   help="speaker IP to ask about topology")
    s.add_argument("--log", default=str(DEFAULT_LOG))
    s.add_argument("--max-bytes", type=int, default=0,
                   help="rotate the log past this size (0 = never)")
    s.add_argument("--keep", type=int, default=5, help="rotated logs to retain")
    s.add_argument("--quiet", action="store_true",
                   help="print only drops and reboots (for daemon logs)")
    s.set_defaults(func=cmd_watch)

    s = add("daemon", help="run the monitor in the background via launchd")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--interval", type=float, default=30,
                   help="seconds between samples (default 30)")
    s.add_argument("--log", default="logs/daemon.jsonl")
    s.add_argument("--max-bytes", type=int, default=50_000_000)
    s.add_argument("--anchor", default=None,
                   help="speaker IP to query (default: a mains-powered one)")
    s.set_defaults(func=cmd_daemon)

    s = add("analyse", help="re-read an existing monitor log")
    s.add_argument("--log", default=str(DEFAULT_LOG))
    s.add_argument("--current-only", action="store_true",
                   help="ignore rotated logs")
    s.set_defaults(func=cmd_analyse)

    s = add("serve", help="play local files, blocking (WRITES to the speaker)")
    s.add_argument("dir")
    s.add_argument("--ip", default=None)
    s.add_argument("--room", default=None, help="name instead of an IP")
    s.add_argument("--volume", type=int, default=None)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--leave-playing", dest="stop_on_exit",
                   action="store_false", default=True,
                   help="leave audio running when this exits")
    s.set_defaults(func=cmd_serve)

    s = add("soak", help="control test: long local-file playback (WRITES)")
    s.add_argument("--ip", default=None)
    s.add_argument("--room", default=None, help="name instead of an IP")
    s.add_argument("--duration", type=float, default=20, help="minutes")
    s.add_argument("--clip-minutes", type=float, default=3)
    s.add_argument("--interval", type=float, default=10)
    s.add_argument("--volume", type=int, default=25)
    s.add_argument("--file", default=None)
    s.add_argument("--log", default="logs/soak.jsonl")
    s.set_defaults(func=cmd_soak)

    s = add("radio", help="play a stream and watch it for drops (WRITES)")
    s.add_argument("url")
    s.add_argument("--ip", default=None)
    s.add_argument("--room", default=None, help="name instead of an IP")
    s.add_argument("--title", default="Stream")
    s.add_argument("--volume", type=int, default=None)
    s.add_argument("--watch", type=float, default=0,
                   help="minutes to watch the stream for drops")
    s.add_argument("--interval", type=float, default=10)
    s.add_argument("--log", default="logs/stream.jsonl")
    s.set_defaults(func=cmd_radio)


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface, built once and shared with the tests.

    Exposed rather than inlined into main() so the contract tests exercise the
    real parser. A test that rebuilds its own copy tests the copy, and would
    happily pass while the installed launchd agent could no longer parse its
    own arguments.
    """
    p = argparse.ArgumentParser(
        prog="twiddle",
        description="twiddle: every radio station at once (`dial`), tonight's local shows (`scene`), and your Sonos by room name (dropout diagnostics under `diag`).")
    # A parent rather than a top-level flag, so both `--json status` and the
    # far more natural `status --json` work. Agents write the latter.
    base = argparse.ArgumentParser(add_help=False)
    # SUPPRESS, not False: a subparser default overwrites whatever the
    # top-level parser already stored, so an unsuppressed default here would
    # silently undo `twiddle --json status`.
    base.add_argument("--json", action="store_true",
                      default=argparse.SUPPRESS,
                      help="machine-readable output")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    # A metavar keeps the hidden aliases out of the usage line; without it
    # argparse prints every choice, which defeats hiding them.
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    control_cli.register(sub, parents=[base])
    relay_cli.register(sub, parents=[base])
    spotify_cli.register(sub, parents=[base])
    radio_cli.register(sub, parents=[base])
    stations_cli.register(sub, parents=[base])
    comedy_cli.register(sub, parents=[base])
    scene_cli.register(sub, parents=[base])
    dial_cli.register(sub, parents=[base])
    viz_cli.register(sub, parents=[base])
    alarm_cli.register(sub, parents=[base])

    d = sub.add_parser("diag", help="speaker and network diagnostics")
    dsub = d.add_subparsers(dest="diag_cmd", required=True, metavar="<command>")
    _add_diag_parsers(dsub)
    # Same commands at the top level, unlisted, so existing callers keep working.
    _add_diag_parsers(sub, hidden=True)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    argv = control_cli.normalize_argv(argv)
    args = build_parser().parse_args(argv)
    return args.func(args)
