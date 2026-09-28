"""KEXP's own API: song, artist, album, show, hosts, and MusicBrainz ids."""
from __future__ import annotations

from .. import net
from ..model import NowPlaying, Station


def _recent(plays: list[dict]) -> list[dict]:
    out = []
    for p in plays:
        airbreak = p.get("play_type") == "airbreak"
        out.append({k: v for k, v in {
            "time": (p.get("airdate") or "")[11:16] or None,
            "artist": None if airbreak else p.get("artist"),
            "song": "(air break)" if airbreak else p.get("song"),
            "album": p.get("album"),
            "art_url": p.get("thumbnail_uri") or None}.items() if v})
    return out


def kexp(station: Station) -> NowPlaying:
    plays = net.get_json("https://api.kexp.org/v2/plays/?limit=8")["results"]
    play = plays[0]
    np = NowPlaying(station.name, artist=play.get("artist"), song=play.get("song"),
                    album=play.get("album"),
                    mb_artist_id=(play.get("artist_ids") or [None])[0],
                    mb_release_group_id=play.get("release_group_id"),
                    art_url=play.get("thumbnail_uri") or play.get("image_uri") or None,
                    recent=_recent(plays[1:]))
    if play.get("play_type") == "airbreak":
        np.raw_title = "(air break)"
    try:
        show = net.get_json(play["show_uri"])
        np.show = show.get("program_name")
        np.hosts = show.get("host_names") or []
    except Exception:
        pass  # the song is what matters; the show is garnish
    return np
