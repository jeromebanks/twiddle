"""`tune`, `np` and `info` -- radio, and finding out who you're hearing.

(`stations` -- the catalog, tags, search -- is `stations_cli.py`.)

`np` answers "what is this?" for whatever source was last chosen -- a radio
station or Spotify -- and `np --info` goes one step further: who the artist
is and what the album is, from MusicBrainz and Wikipedia (see `lookup.py`).
`np --discover` hands the artist to `spotify discover` to hear more of them.

Only `tune` writes to a speaker (and `np --discover`, once it gets there).
`np` and `info` talk to stations and public databases, never to
a speaker, so they are safe while waiting to capture a fault.
"""
from __future__ import annotations

import sys

from . import lookup, spotify, stations, termimage
from .control_cli import add_target_args, add_write_args, cmd_play_radio, emit, fail

SOURCES = [*stations.STATIONS, stations.SPOTIFY]


# ---- tune -----------------------------------------------------------


def cmd_tune(args):
    station = stations.STATIONS[args.station]
    args.url, args.title, args.art = station.url, station.name, station.logo
    rc = cmd_play_radio(args)
    if rc == 0 and not args.dry_run:
        stations.remember(station.key)
    return rc


# ---- np --------------------------------------------------------------------------


def _spotify_now(args):
    try:
        sess = spotify.Session.load()
    except spotify.AuthError as exc:
        return None, fail(args, str(exc), "run `twiddle spotify auth` once")
    try:
        np = spotify.now_playing(sess.current())
    except spotify.ApiError as exc:
        return None, fail(args, f"Spotify API {exc}")
    if not np.get("track"):
        return stations.NowPlaying("Spotify", raw_title="(nothing playing)"), None
    return stations.NowPlaying(
        "Spotify" + ("" if np.get("playing") else " (paused)"),
        artist=", ".join(np["artists"]) or None, song=np["track"],
        album=np.get("album") or None, art_url=np.get("art") or None), None


def _fetch(args, source: str):
    if source == stations.SPOTIFY:
        return _spotify_now(args)
    try:
        return stations.STATIONS[source].now_playing(), None
    except Exception as exc:  # any station's site can be down or reshaped
        return None, fail(args, f"{source}: couldn't fetch now-playing: "
                                f"{type(exc).__name__}: {exc}")


def _primary_artist(np: stations.NowPlaying) -> str | None:
    # Spotify's "A, B" is a list joined for display; the lookup wants one,
    # and without a `with "..."` credit (WFMU's backing bands).
    artist = np.artist.split(", ")[0] if np.artist and np.source.startswith("Spotify") \
        else np.artist
    return stations.lookup_name(artist)


def cmd_np(args):
    source = args.source or stations.last_source()
    if not source:
        return fail(args, "no source given, and none remembered yet",
                    "name one: twiddle np kexp  (or `twiddle stations`)")
    if source not in SOURCES:
        return fail(args, f"unknown source {source!r}", "one of: " + ", ".join(SOURCES))

    np, err = _fetch(args, source)
    if err is not None:
        return err
    artist = _primary_artist(np)
    payload = {"source": source, "now_playing": np.to_dict()}
    human = not getattr(args, "json", False)
    if human:
        if not args.no_art:
            logo = getattr(stations.STATIONS.get(source), "logo", None)
            termimage.show(np.art_url or logo)
        print(np.render())

    if args.info:
        if not artist:
            payload["info"] = None
            if human:
                print("\n(no artist to look up -- nothing is naming one right now)")
        else:
            if human:
                print(f"\nlooking up {artist}...", file=sys.stderr)
            try:
                result = lookup.identify(
                    artist, np.album, np.song,
                    mb_artist_id=np.mb_artist_id,
                    mb_release_group_id=np.mb_release_group_id)
            except lookup.LookupFailed as exc:
                return fail(args, str(exc), "try again in a moment", **payload)
            payload["info"] = result.to_dict()
            if human:
                print(lookup.render(result, artist))

    if args.discover:
        if not artist:
            return fail(args, f"couldn't tell who's playing on {source}",
                        "run `twiddle spotify discover` and type the name")
        # The same parser the shell uses, so discover gets every default it
        # would from the command line rather than a hand-built copy.
        from .cli import build_parser
        argv = ["spotify", "discover", artist, "--room", args.room or "roam"]
        if args.dry_run:
            argv.append("--dry-run")
        dargs = build_parser().parse_args(argv)
        return dargs.func(dargs)

    return 0 if human else emit(args, payload)


# ---- info --------------------------------------------------------------------------


def cmd_info(args):
    if args.save_discogs_token:
        path = lookup.save_discogs_token(args.save_discogs_token)
        if not args.artist:
            return emit(args, {"saved": str(path)}, f"Discogs token saved to {path}")
    if not args.artist:
        return fail(args, "name an artist", 'e.g. twiddle info low --album "Secret Name"')
    name = " ".join(args.artist)
    try:
        result = lookup.identify(name, args.album, args.song,
                                 use_cache=not args.fresh)
    except lookup.LookupFailed as exc:
        return fail(args, str(exc), "try again in a moment")
    if result.artist is None and not getattr(args, "json", False):
        print(lookup.render(result, name))
        return 1
    return emit(args, result.to_dict(), lookup.render(result, name))


# ---- parser wiring ------------------------------------------------------------------


def register(sub, parents=None):
    kw = {"parents": parents} if parents else {}

    p = sub.add_parser(**kw, name="tune", help="play a radio station on a room (WRITES)")
    p.add_argument("station", choices=list(stations.STATIONS), metavar="station",
                   help="station key -- see `twiddle stations`")
    p.add_argument("--volume", type=int, default=None)
    add_target_args(p, default="roam")
    add_write_args(p)
    p.set_defaults(func=cmd_tune)

    p = sub.add_parser(**kw, name="np",
                       help="what's playing: a station, or Spotify (read-only)")
    p.add_argument("source", nargs="?", default=None, metavar="source",
                   help="a station key or `spotify`; default: whatever was last "
                        "tuned or played")
    p.add_argument("-i", "--info", action="store_true",
                   help="also: who is this artist, what is this album "
                        "(MusicBrainz + Wikipedia)")
    p.add_argument("-d", "--discover", action="store_true",
                   help="then hand the artist to `spotify discover` (WRITES once "
                        "you pick a track)")
    p.add_argument("--no-art", action="store_true",
                   help="don't draw the cover / station logo (drawn only in "
                        "Ghostty, kitty or WezTerm)")
    add_target_args(p, default="roam")
    add_write_args(p)
    p.set_defaults(func=cmd_np)

    p = sub.add_parser(**kw, name="info",
                       help="who is this artist? MusicBrainz + Wikipedia (read-only)")
    p.add_argument("artist", nargs="*")
    p.add_argument("--album", default=None, help="an album by them, to pin down which artist")
    p.add_argument("--song", default=None, help="a song by them, same purpose")
    p.add_argument("--fresh", action="store_true", help="skip the month-long cache")
    p.add_argument("--save-discogs-token", default=None, metavar="TOKEN",
                   help="store a personal Discogs token (discogs.com/settings/"
                        "developers) under ~/.cache/twiddle, outside the repo")
    p.set_defaults(func=cmd_info)
