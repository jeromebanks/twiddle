"""`twiddle spotify discover` -- search an artist, sample tracks, play one.

The interactive counterpart to `spotify_cli.py`'s pure API layer: this is
the one command in the package that prompts, on purpose. Finding out what a
band you just heard sounds like, and picking something to play, is
inherently a back-and-forth -- search, look at a list, pick a number, maybe
search again -- not a single call an agent would make.

No ranked "top tracks" data is available here. `GET /artists/{id}/top-tracks`
returns a flat 403 for a personal (Development-mode) Spotify app, and every
artist/track object this app can read has `genres`/`followers`/`popularity`
stripped to null -- both measured against a real account, not assumed. So
track lists here come from Spotify's own search relevance ordering instead,
merged across two query shapes and filtered by artist id (never by name --
see `_artist_tracks`).
"""
from __future__ import annotations

import sys

import requests

from . import lookup, spotify
from .control_cli import _target
from .play import RADIO_SCHEME  # re-exported: the tests build URIs with it
from .spotify_cli import (
    _api,
    _ensure_room_on_relay,
    _play_and_check,
    _relay_device_or_preview,
    _session,
)

# ---- finding tracks for an artist -------------------------------------------


def _artist_tracks(sess, artist: dict) -> list[dict]:
    """A browsing list of this artist's tracks, id-filtered against collisions.

    Two tiers, merged: `artist:"Name"` (reliably attributed, but capped
    around 5 results regardless of `limit`, observed not documented) padded
    -- if thin -- by a plain name search (Spotify's own relevance ordering,
    good at surfacing well-known singles, but returns other same-named
    artists' tracks too). Both tiers are filtered by the resolved artist's
    own id, never by name: a plain search for "Momma" or "Spoon" mostly
    returns other, better-known artists' songs that happen to share the
    word, and filtering by name would silently keep those.
    """
    aid = artist.get("id", "")
    name = artist.get("name", "")

    def _mine(items):
        return [t for t in items
                if aid in [a.get("id") for a in t.get("artists", [])]]

    primary = _mine(sess.search(f'artist:"{name}"', "track", limit=10))
    if len(primary) < 5:
        seen_ids = {t.get("id") for t in primary}
        extra = _mine(sess.search(name, "track", limit=10))
        primary += [t for t in extra if t.get("id") not in seen_ids]

    seen_names: set[str] = set()
    out: list[dict] = []
    for t in primary:
        key = t.get("name", "").lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        out.append(t)
        if len(out) >= 10:
            break
    return out


def _pick_artist(candidates: list[dict]):
    for i, it in enumerate(candidates, 1):
        print(f"  {i}. {spotify.describe(it)}")
    choice = input("which one? [1]: ").strip() or "1"
    if not choice.isdigit() or not (1 <= int(choice) <= len(candidates)):
        print("cancelled")
        return None
    return candidates[int(choice) - 1]


def _search_and_list(args, sess, term: str):
    """Search for an artist, resolve ambiguity, print their tracks.

    Returns (artist, tracks, albums) on success -- `tracks` is the new
    (uri, description) list, `albums` the album name of each -- or None if
    nothing should change -- no match, the search failed, or the picker was
    cancelled. Shared by the initial `discover <artist>` argument and every
    later line typed at the prompt.
    """
    items, err = _api(args, lambda: sess.search(term, "artist", limit=5))
    if err is not None:
        return None
    if not items:
        print(f"no artist matching {term!r}")
        return None
    exact = [a for a in items if a.get("name", "").lower() == term.lower()]
    artist = exact[0] if len(exact) == 1 else _pick_artist(items)
    if artist is None:
        return None
    found, err = _api(args, lambda: _artist_tracks(sess, artist))
    if err is not None:
        return None
    if not found:
        print(f"no tracks found for {artist.get('name')!r} -- try "
              f"`twiddle spotify search --type track \"{artist.get('name')}\"`")
        return None
    tracks = [(t.get("uri", ""), spotify.describe(t)) for t in found]
    print(f"\n{artist.get('name')} - tracks:")
    for i, (_, desc) in enumerate(tracks, 1):
        print(f"  {i}. {desc}")
    print()
    return artist, tracks, [(t.get("album") or {}).get("name") for t in found]


def _info(artist: str, album: str | None) -> None:
    """Background on an artist; the album only sharpens *which* artist it is."""
    print(f"looking up {artist}...", file=sys.stderr)
    try:
        print(lookup.render(lookup.identify(artist, album), artist))
    except Exception as exc:  # never let a lookup take the REPL down with it
        print(f"error: {exc}", file=sys.stderr)


def _help() -> str:
    return ("  <artist name>    search Spotify for an artist\n"
            "  <number>         play that track from the last list\n"
            "  q<number>        queue that track instead\n"
            "  i / info         who is this? (MusicBrainz + Wikipedia)\n"
            "  i<number>        ...about that track's album\n"
            "  now              what's playing\n"
            "  n / next         skip forward\n"
            "  b / prev         skip back\n"
            "  p / pause        pause, or resume if paused\n"
            "  q / quit / exit  leave")


# ---- the REPL ----------------------------------------------------------------


def cmd_discover(args) -> int:
    sess, err = _session(args)
    if err is not None:
        return err

    res, err = _target(args)
    if err is not None:
        return err

    dry_run = bool(getattr(args, "dry_run", False))
    dev, err = _relay_device_or_preview(args, sess, res.group.name)
    if err is not None:
        return err
    device_id = dev.get("id", "")

    print(f"Discovering on {res.group.name}. Type an artist name; "
          "'help' for commands, 'q' to quit.")

    tracks: list[tuple[str, str]] = []  # (uri, description), last shown list
    albums: list[str | None] = []       # album of each entry in `tracks`
    current: dict = {}                  # the artist `tracks` belongs to

    def search(term: str) -> None:
        nonlocal tracks, albums, current
        found = _search_and_list(args, sess, term)
        if found is not None:
            current, tracks, albums = found

    def write(what: str, fn) -> bool:
        """Every Spotify write goes through here, so --dry-run means it."""
        if dry_run:
            print(f"[dry-run] would {what}")
            return False
        _res, err = _api(args, fn)
        return err is None

    def pick(line: str, prefix: str) -> int | None:
        """"q3" -> 3 when 3 is on the current list, else None."""
        rest = line[len(prefix):]
        if line.startswith(prefix) and rest.isdigit() and 1 <= int(rest) <= len(tracks):
            return int(rest)
        return None

    if args.artist:
        try:
            search(" ".join(args.artist))
        except (spotify.ApiError, requests.exceptions.RequestException) as exc:
            print(f"error: {exc}", file=sys.stderr)

    while True:
        try:
            line = input("discover> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        low = line.lower()

        try:
            if low in ("q", "quit", "exit"):
                return 0
            elif low in ("h", "help", "?"):
                print(_help())
            elif low == "now":
                state, err = _api(args, sess.current)
                if err is not None:
                    continue
                np = spotify.now_playing(state)
                if not np.get("track"):
                    print("Nothing is playing.")
                else:
                    print(f"{'▶' if np['playing'] else '❚❚'} {np['track']} - "
                          f"{', '.join(np['artists'])}")
            elif low in ("i", "info") or pick(low, "i"):
                n = pick(low, "i")
                if current:
                    album = albums[n - 1] if n else next((a for a in albums if a), None)
                    _info(current.get("name", ""), album)
                else:
                    # Nothing searched yet: whoever Spotify is playing.
                    state, err = _api(args, sess.current)
                    np = spotify.now_playing(state) if err is None else {}
                    if np.get("artists"):
                        _info(np["artists"][0], np.get("album") or None)
                    else:
                        print("search for an artist first, or play something")
            elif low in ("n", "next"):
                write("skip forward", sess.next_track)
            elif low in ("b", "prev"):
                write("skip back", sess.previous_track)
            elif low in ("p", "pause"):
                state, err = _api(args, sess.current)
                if err is not None:
                    continue
                if spotify.now_playing(state).get("playing"):
                    write("pause", sess.pause)
                else:
                    write("resume", lambda: sess.play(device_id=device_id))
            elif (n := pick(low, "q")) is not None:
                uri, label = tracks[n - 1]
                if write(f"queue {label}", lambda: sess.queue(uri)):
                    print(f"queued {label}")
            elif low.isdigit() and 1 <= int(low) <= len(tracks):
                n = int(low)
                _ensure_room_on_relay(res, dry_run)
                if dry_run:
                    print(f"[dry-run] would play {tracks[n - 1][1]} "
                          f"on {res.group.name}")
                    continue
                np, err = _play_and_check(
                    args, sess, dev,
                    lambda: sess.play(uris=[u for u, _ in tracks],
                                      offset=n - 1, device_id=device_id))
                if err is not None:
                    continue
                label = (np.get("track") if np else None) or tracks[n - 1][1]
                print(f"▶ {label}\n   on {res.group.name}")
            else:
                search(line)
        except (spotify.ApiError, requests.exceptions.RequestException) as exc:
            print(f"error: {exc}", file=sys.stderr)
