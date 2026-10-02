"""`twiddle scene` (the TUI) and `twiddle scene list` (plain output).

`scene list` is read-only: it fetches listings and prints them. `scene`
writes only when you press play -- to Spotify, and to a speaker only when
the chosen device is the Roams' relay (journalled, like every write).
A Bandcamp track plays on this Mac (ffplay) or straight on the Roam, no
Spotify involved -- the Roam's switch journalled like any speaker write.
`scene login` signs this Mac's own player in to Spotify; no speaker.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

from ..control_cli import emit, fail
from ..scenespec import buildstatus, dataset
from ..scenespec import venue as venues_mod

BUILD_BUSY = 75         # EX_TEMPFAIL: another build holds the lock; try again later
LAUNCHD_LABEL = "com.twiddle.scene-build"


def _read_dataset(args):
    """The published dataset, or (None, the failure to report). Never touches the network."""
    try:
        snap = dataset.load()
    except dataset.DatasetError as exc:
        return None, fail(args, str(exc))
    if snap is None:
        return None, fail(args, "no dataset yet", "run `twiddle scene build` once "
                          "(then schedule it: `twiddle scene schedule`)")
    return snap, 0


def cmd_list(args) -> int:
    if args.refresh:        # an explicit request to run the builder first
        from ..scenedata.builder import BuildBusy, BuildError, build
        try:
            build(log=lambda m: print(m, file=sys.stderr))
        except BuildBusy as exc:
            print(f"note: {exc}; showing the last dataset", file=sys.stderr)
        except BuildError as exc:
            print(f"warning: build failed ({exc}); showing the last dataset", file=sys.stderr)
    snap, code = _read_dataset(args)
    if snap is None:
        return code
    watched = venues_mod.from_rows(snap.venues)
    shows = snap.shows
    errors = [f"{n}: {v.get('error')}" for n, v in snap.sources.items() if not v.get("ok", True)]
    if not shows:
        return fail(args, "no listings", "; ".join(errors))
    today = date.today()
    until = today + timedelta(days=args.days) if args.days else None
    want = None
    if args.venue:
        want = venues_mod.resolve(watched, args.venue)
    out = []
    for s in shows:
        if s.day < today or (until and s.day >= until):
            continue
        if args.venue:
            if not (want.matches(s.venue) if want else args.venue.lower() in s.venue.lower()):
                continue
        elif not args.all_venues and venues_mod.find(watched, s.venue) is None:
            continue
        out.append(s)
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "shows": [s.to_dict() for s in out], "errors": errors,
                          "generated_at": dataset.iso(snap.generated_at)
                          if snap.generated_at else None}, indent=2))
        return 0
    lines = []
    for s in out:
        meta = " ".join(x for x in (s.age, s.price, s.times) if x)
        extra = "; ".join(s.notes + s.flags)
        lines.append(f"{s.day:%a %b %d}  {venues_mod.display_name(watched, s.venue):<16} "
                     f"{s.billing}" + (f"   [{meta}]" if meta else "")
                     + (f"  ({extra})" if extra else ""))
        if args.links:
            lines += [f"            {u}" for u in dict.fromkeys(
                (s.source_url, s.tickets, s.flyer)) if u]
    for e in errors:
        lines.append(f"warning: {e}")
    if snap.stale():
        lines.append("warning: the dataset is over a day old; `twiddle scene build` refreshes it")
    return emit(args, {}, "\n".join(lines) or "no shows match")


def cmd_venue(args) -> int:
    """Where a venue is, what it is, its website. No speaker, no listings fetch."""
    import textwrap
    snap, code = _read_dataset(args)
    if snap is None:
        return code
    watched = venues_mod.from_rows(snap.venues)
    if not args.name:
        rows = [{"name": v.name, "address": v.info.address, "url": v.info.url}
                for v in watched]
        if not rows:
            return fail(args, "this dataset lists no venues")
        width = max(len(r["name"]) for r in rows)
        text = "\n".join(f"{r['name']:<{width}}  {r['address'] or '-'}" for r in rows)
        return emit(args, {"venues": rows}, text)
    v = venues_mod.resolve(watched, args.name)
    if v is None:
        return fail(args, f"no watched venue matches {args.name!r}",
                    "`twiddle scene venue` lists them")
    rec = snap.venue(v.name)
    wiki = (rec or {}).get("wikipedia_summary")
    lines = [v.name]
    for label, value in (("address", v.info.address), ("website", v.info.url),
                         ("instagram", v.info.instagram_url), ("map", v.info.map_url)):
        if value:
            lines.append(f"  {label:<8} {value}")
    if v.info.about:
        lines += ["", textwrap.fill(v.info.about, 78, initial_indent="  ",
                                    subsequent_indent="  ")]
    if wiki and wiki.get("extract"):
        lines += ["", textwrap.fill(wiki["extract"], 78, initial_indent="  ",
                                    subsequent_indent="  "),
                  f"  — Wikipedia, {wiki.get('url', '')}"]
    data = {"name": v.name, "address": v.info.address, "url": v.info.url,
            "map": v.info.map_url, "about": v.info.about,
            "wikipedia": wiki, "matches": list(v.match)}
    return emit(args, data, "\n".join(lines))


def tui_book(on_update, session_factory=None, bc_tracks=None):
    """The TUI's book. Who a band is comes from the dataset; the client only
    applies your pin and fetches song lists, both of which need your own sign-in."""
    from .. import bandcamp, spotify_ops
    from .book import BandBook, PinEnricher, TrackEnricher
    spotify = PinEnricher(session_factory or spotify_ops.session)
    return BandBook([spotify, TrackEnricher(spotify, bc_tracks=bc_tracks or bandcamp.tracks)],
                    on_update=on_update)


def cmd_scene(args) -> int:
    # Imported here so no other command pays for loading Textual.
    from .app import SceneApp
    from ..dial.output import Outputs
    from .local import LocalSpeaker
    from .players import SpotifyConnectPlayer

    app = SceneApp(book_factory=tui_book,
                   player=SpotifyConnectPlayer(room=args.room, dry_run=args.dry_run,
                                               local=LocalSpeaker()),
                   venue=args.venue, all_venues=args.all_venues,
                   dry_run=args.dry_run, outputs=Outputs(dry_run=args.dry_run))
    app.run()
    return 0


def cmd_build(args) -> int:
    """Collect, enrich and publish the dataset. Writes one file; no speaker."""
    from ..scenedata.builder import BuildBusy, BuildError, build
    try:
        r = build(days=args.days, all_venues=args.all_venues, use_spotify=not args.no_spotify,
                  dry_run=args.dry_run, log=lambda m: print(m, file=sys.stderr, flush=True))
    except BuildBusy as exc:
        fail(args, str(exc), "a scheduled build may be running; its data appears when it finishes")
        return BUILD_BUSY
    except BuildError as exc:
        return fail(args, str(exc))
    return emit(args, {"path": str(r.path), "shows": r.shows, "bands": r.bands,
                       "enriched": r.enriched, "reused": r.reused, "errors": r.errors,
                       "enrichers": r.enrichers},
                f"{r.shows} shows, {r.bands} bands ({r.enriched} looked up, "
                f"{r.reused} reused) -> {r.path}" + "".join(f"\nwarning: {e}" for e in r.errors))


def schedule_plist(project, hours: float, log) -> dict:
    """A launchd agent that runs the build every `hours` (and once at load)."""
    from .. import daemon
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [daemon._which("uv"), "run", "--project", str(project),
                             "twiddle", "scene", "build"],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "StartInterval": int(hours * 3600),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }


def cmd_status(args) -> int:
    """How a build is going (or how the last one went): phase, bands, ETA, and each
    service's request rate against its cap. Reads one small file; touches nothing else."""
    st = buildstatus.read()
    if st is None:
        return fail(args, "no build has reported yet", "run `twiddle scene build`")
    return emit(args, st, buildstatus.render(st))


def cmd_dlq(args) -> int:
    """The dead-letter queue: bands and venues no build could identify, with what was tried.
    Reads and writes only `dead-letters.json` next to the dataset; no network."""
    # The queue is the producer's; the client only starts it, like `build`.
    from ..scenedata import deadletters as dl
    sub = getattr(args, "dlq_cmd", None) or "list"
    try:
        if sub in ("resolve", "abandon", "reopen"):
            if sub == "resolve":
                try:
                    res = json.loads(args.resolution) if args.resolution else {}
                except json.JSONDecodeError as exc:
                    return fail(args, f"--resolution is not valid JSON ({exc})")
                if args.alias:
                    res["alias"] = args.alias
                if not res:
                    return fail(args, "nothing to record", "give --alias NAME or --resolution '{...}'")
                l = dl.resolve(args.id, res, by=args.by)
            elif sub == "abandon":
                l = dl.abandon(args.id, args.note)
            else:
                l = dl.reopen(args.id)
            return emit(args, {"id": args.id, "status": l["status"]}, f"{args.id}: {l['status']}")
    except KeyError:
        return fail(args, f"no dead letter {args.id!r}", "`twiddle scene dlq` lists them")
    if sub == "scan":                   # re-derive the queue from the dataset: no network
        snap, code = _read_dataset(args)
        if snap is None:
            return code
        watched = venues_mod.from_rows(snap.venues)
        billed = dl.billed_ids(snap.bands, snap.shows, watched)
        counts = dl.sync({**dl.band_letters(snap.bands, snap.shows),
                          **dl.venue_letters(snap.shows, watched)}, billed=billed)
        return emit(args, counts, "queue updated: " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    doc = dl.load()
    status = None if getattr(args, "all", False) else (getattr(args, "status", None) or dl.PENDING)
    rows = dl.select(doc, kind=getattr(args, "kind", None), status=status,
                     limit=getattr(args, "limit", 20))
    if sub == "export":                 # one JSON object per line, for an AI or a script
        for lid, l in rows:
            print(json.dumps(dict(l, id=lid), ensure_ascii=False))
        return 0
    counts: dict[str, int] = {}
    for l in doc["letters"].values():
        k = f"{l['kind']}/{l['reason']}/{l['status']}"
        counts[k] = counts.get(k, 0) + 1
    lines = [f"{n:>5}  {k}" for k, n in sorted(counts.items())] or ["the queue is empty"]
    lines.append("")
    for lid, l in rows:
        lines.append(f"{lid}  ({l['reason']}, {l.get('show_count', 0)} shows)  {l['name']}")
    return emit(args, {"counts": counts, "letters": [dict(l, id=lid) for lid, l in rows]},
                "\n".join(lines))


def cmd_schedule(args) -> int:
    """Print the launchd agent that keeps the dataset fresh. Installs nothing."""
    import plistlib
    from pathlib import Path
    project = Path(__file__).resolve().parents[3]
    log = Path.home() / "Library" / "Logs" / "twiddle-scene-build.log"
    xml = plistlib.dumps(schedule_plist(project, args.hours, log)).decode()
    target = f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist"
    text = (f"{xml}\nSave that as {target}, then:\n"
            f"  launchctl bootstrap gui/$(id -u) {target}\n"
            f"undo with:\n  launchctl bootout gui/$(id -u)/{LAUNCHD_LABEL}\n"
            f"Every {args.hours:g}h it runs `twiddle scene build`; log: {log}")
    return emit(args, {"label": LAUNCHD_LABEL, "interval_hours": args.hours}, text)


def cmd_login(args) -> int:
    """Sign this Mac's speaker player in to Spotify: `relay login`, own cache."""
    from ..relay_cli import cmd_login as relay_login
    from .local import LOCAL_CACHE, machine_name
    args.cache = str(LOCAL_CACHE)
    args.device_name = machine_name()
    return relay_login(args)


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="scene",
                       help="local shows: browse venues, look up bands, play them")
    p.add_argument("--venue", default=None, help="start on this venue (partial name)")
    p.add_argument("--all-venues", action="store_true",
                   help="every venue listed, not just the watched ones")
    p.add_argument("--room", default="roam",
                   help="the room the relay plays on (default: roam)")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve everything, play nothing")
    p.set_defaults(func=cmd_scene)
    ssub = p.add_subparsers(dest="scene_cmd", metavar="<command>")
    ls = ssub.add_parser(**kw, name="list", help="upcoming shows as text (read-only)")
    ls.add_argument("--venue", default=None, help="one venue (partial name)")
    ls.add_argument("--days", type=int, default=None, help="only the next N days")
    ls.add_argument("--all-venues", action="store_true")
    ls.add_argument("--refresh", action="store_true",
                    help="run `scene build` first (otherwise this only reads the dataset)")
    ls.add_argument("--links", action="store_true",
                    help="each show's listing link under it (The List's page, Yoshi's event)")
    ls.set_defaults(func=cmd_list)
    bd = ssub.add_parser(**kw, name="build",
                         help="collect and enrich the local event dataset that scene reads "
                              "(network; writes only the dataset file)")
    bd.add_argument("--days", type=int, default=31,
                    help="enrich bands playing your venues this many days ahead (default 31)")
    bd.add_argument("--all-venues", action="store_true",
                    help="enrich bands at every venue listed, not just the watched ones")
    bd.add_argument("--no-spotify", action="store_true",
                    help="skip Spotify identity even if this Mac is signed in")
    bd.add_argument("--dry-run", action="store_true",
                    help="collect and enrich, but publish nothing")
    bd.set_defaults(func=cmd_build)
    st = ssub.add_parser(**kw, name="status",
                         help="how `scene build` is going: bands done, ETA, request rates "
                              "against their caps (read-only)")
    st.set_defaults(func=cmd_status)
    dq = ssub.add_parser(**kw, name="dlq",
                         help="bands and venues no build could identify, with what was tried: "
                              "list, export for an AI, resolve, abandon (writes only the queue file)")
    dq.set_defaults(func=cmd_dlq, dlq_cmd="list")
    dsub = dq.add_subparsers(dest="dlq_cmd", metavar="<command>")
    for name, hlp in (("list", "counts, then the busiest pending letters"),
                      ("export", "pending letters as JSON lines (for an AI or a script)")):
        y = dsub.add_parser(**kw, name=name, help=hlp)
        y.set_defaults(func=cmd_dlq, dlq_cmd=name)
        y.add_argument("--kind", choices=("band", "venue"), default=None)
        y.add_argument("--status", default=None,
                       help="pending (default), resolved, abandoned or expired")
        y.add_argument("--all", action="store_true", help="every status")
        y.add_argument("--limit", type=int, default=20 if name == "list" else None)
    sn = dsub.add_parser(**kw, name="scan", help="re-derive the queue from the current dataset "
                                                 "(a build does this too; no network)")
    sn.set_defaults(func=cmd_dlq, dlq_cmd="scan")
    rs = dsub.add_parser(**kw, name="resolve", help="record what a letter turned out to be")
    rs.add_argument("id")
    rs.add_argument("--alias", help="a band: the name to search for instead of the billing")
    rs.add_argument("--resolution", help="any resolution as JSON, e.g. a venue's address/url/about")
    rs.add_argument("--by", choices=("ai", "human"), default="human")
    rs.set_defaults(func=cmd_dlq, dlq_cmd="resolve")
    ab = dsub.add_parser(**kw, name="abandon", help="give up on a letter (a build never reopens it)")
    ab.add_argument("id")
    ab.add_argument("--note", default="")
    ab.set_defaults(func=cmd_dlq, dlq_cmd="abandon")
    ro = dsub.add_parser(**kw, name="reopen", help="make a resolved or abandoned letter pending again")
    ro.add_argument("id")
    ro.set_defaults(func=cmd_dlq, dlq_cmd="reopen")
    sc = ssub.add_parser(**kw, name="schedule",
                         help="print a launchd agent that runs `scene build` on a timer "
                              "(read-only; installs nothing)")
    sc.add_argument("--hours", type=float, default=6, help="how often (default 6)")
    sc.set_defaults(func=cmd_schedule)
    vn = ssub.add_parser(**kw, name="venue",
                         help="a venue's address, website and description; "
                              "no name lists them all (read-only)")
    vn.add_argument("name", nargs="?", help="partial name: gilman, fox, yosh")
    vn.set_defaults(func=cmd_venue)
    lg = ssub.add_parser(**kw, name="login",
                         help="sign this Mac's speaker player in to Spotify (browser; "
                              "Premium only)")
    lg.add_argument("--timeout", type=float, default=300)
    lg.add_argument("--force", action="store_true",
                    help="sign in again, e.g. as a different account")
    lg.set_defaults(func=cmd_login)
