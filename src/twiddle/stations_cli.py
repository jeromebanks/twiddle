"""`stations` -- the catalog, its tags, and finding new stations to add.

    twiddle stations [--tag T]          what `tune`/`dial` know, with tags
    twiddle stations tags               the tag vocabulary and how many carry each
    twiddle stations search <text>      look a station up in public directories
    twiddle stations probe <url>        what a stream is, and how we'd learn what's on

All read-only: they talk to the catalog files and public directories and
streams, never to a speaker. `search` and `probe` are what the
`add-radio-station` skill drives; adding the station is writing a file in
`stations/catalog/`, which neither command does.
"""
from __future__ import annotations

from . import stations
from .control_cli import emit, fail


def _tag_arg(args) -> str | None:
    tag = getattr(args, "tag", None)
    if tag and tag != "all" and tag not in stations.TAGS:
        raise SystemExit(f"twiddle: unknown tag {tag!r}; see `twiddle stations tags`")
    return tag


def cmd_list(args):
    tag = _tag_arg(args)
    rows = stations.with_tag(tag)
    return emit(args, {"stations": [{"key": s.key, "name": s.name, "url": s.url,
                                     "about": s.blurb, "tags": list(s.tags)} for s in rows]},
                "\n".join(f"{s.key:13} {s.blurb}\n{'':13} [{', '.join(s.tags)}]" for s in rows)
                + "\n\nplay one:  twiddle tune <key>      (or just `kexp` with radio.zsh)"
                + "\nnow:       twiddle np [key] [-i] [-d]"
                + "\nnarrow:    twiddle stations --tag <tag>   (tags: twiddle stations tags)")


def cmd_tags(args):
    counts = stations.tag_counts()
    rows = [{"tag": t, "stations": counts.get(t, 0), "means": m} for t, m in stations.TAGS.items()]
    width = max(len(t) for t in stations.TAGS)
    return emit(args, {"tags": rows},
                "\n".join(f"{r['tag']:{width}} {r['stations']:>3}  {r['means']}" for r in rows))


def cmd_search(args):
    from .stations import directory
    found, errors = directory.search(" ".join(args.text), source=args.source, genre=args.genre,
                                     country=args.country, limit=args.limit,
                                     catalog=stations.STATIONS)
    if not found and errors:
        return fail(args, "; ".join(errors))
    human = "\n\n".join(f.render() for f in found) or "(nothing found)"
    if errors:
        human += "\n\n(" + "; ".join(errors) + ")"
    human += ("\n\nnext: twiddle stations probe <stream> [--callsign X] [--homepage URL]"
              if found else "")
    return emit(args, {"results": [f.to_dict() for f in found], "errors": errors}, human)


def cmd_probe(args):
    from .stations import probe
    p = probe.probe(args.url, callsign=args.callsign, homepage=args.homepage,
                    samples=args.samples, every=args.every, logos=args.logo)
    return emit(args, p.to_dict() | {"draft": probe.draft_toml(p, args.key)
                                     if p.stream_url else None},
                probe.render(p, args.key))


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="stations",
                       help="the radio stations `tune`/`dial` know; tags; find new ones (read-only)")
    p.add_argument("--tag", default=None, help="only stations with this tag")
    p.set_defaults(func=cmd_list)
    ssub = p.add_subparsers(dest="stations_cmd", metavar="<command>")
    t = ssub.add_parser(**kw, name="tags", help="the tag vocabulary, with station counts")
    t.set_defaults(func=cmd_tags)

    from .stations.directory import SOURCES
    se = ssub.add_parser(**kw, name="search",
                         help="look a station up: radio-browser.info and SomaFM (TuneIn with --source tunein)")
    se.add_argument("text", nargs="*", help="a name, call sign or words ('kexp', 'dub techno')")
    se.add_argument("--source", choices=SOURCES, default=None, help="just one directory")
    se.add_argument("--genre", default=None,
                    help="the directory's own genre tag (radio-browser, SomaFM), e.g. jazz")
    se.add_argument("--country", default=None, help="two-letter country code (radio-browser)")
    se.add_argument("--limit", type=int, default=10, help="per directory (default 10)")
    se.set_defaults(func=cmd_search)

    pr = ssub.add_parser(**kw, name="probe",
                         help="what a stream is, whether a Sonos can play it, which fetcher")
    pr.add_argument("url", help="the stream (or a .pls/.m3u pointing at it)")
    pr.add_argument("--callsign", default=None,
                    help="the station's call sign, to look for its Spinitron page")
    pr.add_argument("--homepage", default=None,
                    help="the station's site, to spot its platform and a logo")
    pr.add_argument("--key", default=None, help="the catalog key to draft for")
    pr.add_argument("--logo", action="append", default=None,
                    help="a logo URL to measure too (repeatable)")
    pr.add_argument("--samples", type=int, default=2, help="ICY titles to read (default 2)")
    pr.add_argument("--every", type=float, default=8.0,
                    help="seconds between them (default 8)")
    pr.set_defaults(func=cmd_probe)
