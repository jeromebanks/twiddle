"""`twiddle dial` (the TUI) and `twiddle dial list` (plain output).

`dial list` is read-only: it asks every station what it is playing. `dial`
writes only when you tune, stop, change volume or mute -- to the chosen
Sonos room (journalled, like every speaker write), or to this Mac.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..control_cli import emit
from ..stations import TAGS, with_tag
from . import state


def _tag(args) -> str | None:
    # `dial list --tag` or `dial --tag … list`: either spelling narrows it.
    tag = getattr(args, "list_tag", None) or getattr(args, "tag", None)
    if tag and tag != "all" and tag not in TAGS:
        raise SystemExit(f"twiddle: unknown tag {tag!r}; see `twiddle stations tags`")
    return tag


def cmd_list(args) -> int:
    def one(s):
        try:
            return s, s.now_playing(), None
        except Exception as exc:  # any station's site can be down or reshaped
            return s, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(6) as pool:
        rows = list(pool.map(one, with_tag(_tag(args))))
    payload = {"stations": [{"key": s.key, "name": s.name,
                             "now_playing": np.to_dict() if np else None,
                             "error": err} for s, np, err in rows]}
    lines = []
    for s, np, err in rows:
        if err:
            what = f"(couldn't reach: {err[:60]})"
        elif np.artist or np.song:
            what = f"{np.artist or '?'} - {np.song or ''}"
        else:
            what = np.raw_title or "(no title)"
        lines.append(f"{s.key:13} {what}" + (f"   [{np.show}]" if np and np.show else ""))
    return emit(args, payload, "\n".join(lines))


def cmd_dial(args) -> int:
    # Imported here so no other command pays for Textual -- and, for
    # textual-image, *before* the app takes over the terminal (see app.py).
    from .app import DialApp
    from .output import MAC, Outputs

    if args.bluetooth:
        output_id = "bt:" + args.bluetooth
    elif args.output == "mac":
        output_id = MAC
    elif args.room:
        output_id = f"room:{args.room}"
    else:
        # This Mac until you choose another with `d` (it's remembered).
        output_id = state.get_state("output") or MAC
    tag = _tag(args)
    app = DialApp(outputs=Outputs(dry_run=args.dry_run), output_id=output_id,
                  dry_run=args.dry_run, tag=tag, splash=not args.no_splash)
    app.run()
    return 0


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}
    p = sub.add_parser(**kw, name="dial",
                       help="radio TUI: every station's now-playing, tune with enter")
    p.add_argument("--room", default=None,
                   help="the Sonos room to play on (default: last used, else this Mac)")
    p.add_argument("--output", choices=["mac"], default=None,
                   help="play on this Mac's speakers instead of a Sonos room")
    p.add_argument("--bluetooth", metavar="NAME", default=None,
                   help="play on paired Bluetooth headphones/speaker, by its name as "
                        "the Mac shows it (d lists them)")
    p.add_argument("--dry-run", action="store_true",
                   help="everything works, but nothing is tuned or changed")
    p.add_argument("--tag", default=None,
                   help="show only stations with this tag (default: the last one "
                        "chosen with t); `twiddle stations tags` lists them")
    p.add_argument("--no-splash", action="store_true",
                   help="skip the opening card (Twiddle waving; any key closes it anyway)")
    p.set_defaults(func=cmd_dial)
    dsub = p.add_subparsers(dest="dial_cmd", metavar="<command>")
    ls = dsub.add_parser(**kw, name="list",
                         help="what every station is playing, as text (read-only)")
    ls.add_argument("--tag", dest="list_tag", default=None, help="only stations with this tag")
    ls.set_defaults(func=cmd_list)
