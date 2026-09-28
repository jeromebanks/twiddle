"""How each station says what it is playing: one module per mechanism.

A catalog entry names one of these with `fetch = "<name>"` (default
"icy") and passes `fetch_args` as keyword arguments. Each fetcher takes the
Station (plus those arguments) and returns a NowPlaying. To add one: write
a module with a pure `parse_*` function (tested against a captured page)
and the fetcher around it, then list it here.

| name        | where it gets now-playing                         | fetch_args        |
|-------------|---------------------------------------------------|-------------------|
| icy         | the stream's ICY title (every station has this)   | note, music=false, encoding |
| talk        | ICY, shown as a segment, never an artist          |                   |
| spinitron   | spinitron.com/<CALLSIGN> (college/community)      | callsign          |
| somafm      | somafm.com/songs/<channel>.json                   |                   |
| radiofrance | api.radiofrance.fr/livemeta/pull/<id> (FIP)       | rf_id             |
| nts         | nts.live/api/v2/live (show, not song)             | channel           |
| kexp        | api.kexp.org (song, show, MusicBrainz ids)        |                   |
| kqed        | kqed.org/radio/schedule (program schedule)        |                   |
| wmbr        | wmbr.org/dynamic.xml (show and host)              |                   |
| wfmu        | ICY + wfmu.org playlist RSS (the DJ)              |                   |
| rainwave    | rainwave.cc/api4/info (song, game, art, recent)   | sid               |
| airtime     | <id>.airtime.pro/api/live-info-v2 (show, schedule)| id                |
| streamabc   | api.streamabc.net/metadata/channel/<key>.json     | channel           |
"""
from __future__ import annotations

from collections.abc import Callable

# Modules, not their functions: `fetchers.kqed` must stay the module, so
# tests (and anyone) can reach its cache and clock.
from . import airtime, icy, kexp, kqed, nts, radiofrance, rainwave, somafm, spinitron, streamabc, wfmu, wmbr

FETCHERS: dict[str, Callable] = {
    "icy": icy.icy, "talk": icy.talk, "spinitron": spinitron.spinitron,
    "somafm": somafm.somafm, "radiofrance": radiofrance.radiofrance, "nts": nts.nts,
    "kexp": kexp.kexp, "kqed": kqed.kqed, "wmbr": wmbr.wmbr, "wfmu": wfmu.wfmu,
    "rainwave": rainwave.rainwave, "airtime": airtime.airtime,
    "streamabc": streamabc.streamabc,
}
