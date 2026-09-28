"""The tags a station may carry, and what each means.

Deliberately small: a tag is only useful as a filter if it cuts the list
into a handful, so add one here when a real group of stations needs it,
not per station. The catalog test refuses a tag that isn't listed.
"""
from __future__ import annotations

TAGS: dict[str, str] = {
    "college": "run by a college or university station",
    "community": "listener-supported community / independent station",
    "public": "public radio (NPR member, BBC, listener-funded big station)",
    "freeform": "DJs pick anything: eclectic, no format",
    "indie": "indie, alternative, punk, underground rock",
    "jazz": "jazz programming",
    "classical": "classical / contemporary composition",
    "electronic": "electronic: house, techno, downtempo, experimental",
    "ambient": "ambient, drone, beatless",
    "sleep": "meant for falling asleep to",
    "dj": "DJ shows: names the show, rarely the song",
    "news": "news",
    "talk": "spoken word: talk, interviews, segments",
    "comedy": "stand-up comedy",
    "country": "country, Americana, honky-tonk, Texas / red dirt",
    "somafm": "SomaFM's commercial-free channels",
    "international": "outside the US, or playing Japanese/Korean music (an extra, like somafm)",
    "japanese": "Japanese music or a station in Japan",
    "korean": "Korean music or a station in Korea",
    "kpop": "K-pop",
    "anime": "anime songs, J-pop/J-rock, Vocaloid, Touhou",
    "games": "video game music: soundtracks, remixes, chiptune, demoscene",
    "80s": "mostly music from the 1980s: synthpop, new wave, hits",
    "90s": "mostly music from the 1990s: alternative, pop, eurodance",
}
