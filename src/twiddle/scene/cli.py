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
from ..scenespec import dataset
from . import venues as venues_mod

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
    try:
        watched = venues_mod.watched()
    except ValueError as exc:
        return fail(args, f"bad venue config: {exc}")
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
    try:
        watched = venues_mod.watched()
    except ValueError as exc:
        return fail(args, f"bad venue config: {exc}")
    if not args.name:
        rows = [{"name": v.name, "address": v.info.address, "url": v.info.url}
                for v in watched]
        width = max(len(r["name"]) for r in rows)
        text = "\n".join(f"{r['name']:<{width}}  {r['address'] or '-'}" for r in rows)
        return emit(args, {"venues": rows}, text)
    v = venues_mod.resolve(watched, args.name)
    if v is None:
        return fail(args, f"no watched venue matches {args.name!r}",
                    "`twiddle scene venue` lists them")
    snap = None
    try:
        snap = dataset.load()
    except dataset.DatasetError:
        pass                # the venue's own details need no dataset
    rec = snap.venue(v.name) if snap else None
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


def cmd_scene(args) -> int:
    # Imported here so no other command pays for loading Textual.
    from .. import spotify_ops
    from .app import SceneApp
    from ..dial.output import Outputs
    from .book import BandBook, TrackEnricher
    from ..scenedata.bands import BandcampEnricher, LookupEnricher, SpotifyEnricher
    from .local import LocalSpeaker
    from .players import SpotifyConnectPlayer

    try:
        watched = venues_mod.watched()
    except ValueError as exc:
        return fail(args, f"bad venue config: {exc}")

    def book_factory(on_update):
        # TrackEnricher alone fetches song lists; the identity enrichers must not
        # too, or a band with no dataset record has its songs requested twice.
        spotify = SpotifyEnricher(spotify_ops.session, tracks=False)
        return BandBook([LookupEnricher(), spotify, BandcampEnricher(fetch_tracks=False),
                         TrackEnricher(spotify)], on_update=on_update)

    app = SceneApp(book_factory=book_factory,
                   player=SpotifyConnectPlayer(room=args.room, dry_run=args.dry_run,
                                               local=LocalSpeaker()),
                   watched=watched, venue=args.venue, all_venues=args.all_venues,
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
