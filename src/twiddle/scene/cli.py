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
from datetime import date, timedelta

from ..control_cli import emit, fail
from . import cache
from . import venues as venues_mod
from .model import Show
from .sources import fetch_all, sources


def _load_shows(refresh: bool) -> tuple[list[Show], list[str]]:
    shows, at = cache.load_listings()
    names = list(sources())
    if shows and cache.listings_fresh(at, names) and not refresh:
        return shows, []
    fresh, errors = fetch_all(stale=shows)
    if fresh:
        cache.save_listings(fresh, names)
        return fresh, errors
    return shows, errors        # stale beats nothing, and the error says why


def cmd_list(args) -> int:
    try:
        watched = venues_mod.watched()
    except ValueError as exc:
        return fail(args, f"bad venue config: {exc}")
    shows, errors = _load_shows(args.refresh)
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
        print(json.dumps({"ok": True, "shows": [s.to_dict() for s in out],
                          "errors": errors}, indent=2))
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
    return emit(args, {}, "\n".join(lines) or "no shows match")


def cmd_venue(args) -> int:
    """Where a venue is, what it is, its website. No speaker, no listings fetch."""
    import textwrap
    from .venue_info import wiki_summary
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
    wiki = wiki_summary(v.info.wikipedia) if v.info.wikipedia else None
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
    from .bands import BandBook, BandcampEnricher, LookupEnricher, SpotifyEnricher
    from .local import LocalSpeaker
    from .players import SpotifyConnectPlayer

    try:
        watched = venues_mod.watched()
    except ValueError as exc:
        return fail(args, f"bad venue config: {exc}")

    def book_factory(on_update):
        return BandBook([LookupEnricher(), SpotifyEnricher(spotify_ops.session),
                         BandcampEnricher()], on_update=on_update)

    app = SceneApp(book_factory=book_factory,
                   player=SpotifyConnectPlayer(room=args.room, dry_run=args.dry_run,
                                               local=LocalSpeaker()),
                   watched=watched, venue=args.venue, all_venues=args.all_venues,
                   dry_run=args.dry_run, outputs=Outputs(dry_run=args.dry_run))
    app.run()
    return 0


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
    ls.add_argument("--refresh", action="store_true", help="ignore the 6h cache")
    ls.add_argument("--links", action="store_true",
                    help="each show's listing link under it (The List's page, Yoshi's event)")
    ls.set_defaults(func=cmd_list)
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
