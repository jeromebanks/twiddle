"""How a station builds its ICY title: a registry of named title shapes.

An ICY `StreamTitle` is free text, and stations fill it differently: most
send "Artist - Song", iHeart sends `title="Song",artist="Artist",url="..."`,
WFMU sends `"Song" by Artist on Show on WFMU`, comedy247 sends iHeart's
`Artist - text="Song" ... amgArtworkURL="..."`, Korean City Pop sends
`Artist · Song (year)` and KCRW names `Show-Host-join.kcrw.com`.
A catalog file names its shape with `fetch_args = { titles = "<shape>" }`;
without one, the `icy` fetcher reads iHeart attributes first and then
"Artist - Song", which is what every station got before shapes existed.

Each shape is a pure `str -> dict` of NowPlaying fields (artist, song,
show, hosts, art_url), and `{}` when the title doesn't have its shape. A
guess where the shape doesn't fit would send a nonsense artist on to
`discover` or an info lookup, so a shape only says what it is sure of.
"""
from __future__ import annotations

import re
from collections.abc import Callable


def artist_song(title: str) -> dict:
    """"Artist - Song", split at the first " - "; nothing without one.

    An ICY title can just as well be a show name or "Your DJ speaks over
    ..." (WFMU), so only that exact shape is trusted."""
    if not title or " - " not in title:
        return {}
    artist, song = (s.strip() for s in title.split(" - ", 1))
    return {k: v for k, v in (("artist", artist), ("song", song)) if v}


def iheart_rewrite(title: str | None) -> str | None:
    """iHeart's `title="Song",artist="Artist",url="..."` as "Artist - Song";
    any other title unchanged."""
    if not title:
        return title
    fields = dict(re.findall(r'(\w+)="(.*?)"(?:,|$)', title))
    if fields.get("artist") and fields.get("title"):
        return f"{fields['artist']} - {fields['title']}"
    return title


def iheart_attrs(title: str) -> dict:
    # Rewritten and then split, rather than read straight from the
    # attributes: an artist="A - B" has always come out as artist "A", and
    # changing that here would change what those stations show.
    tidied = iheart_rewrite(title)
    return artist_song(tidied) if tidied != title else {}


# A trailing `with "The Motations"` credits a backing band: shown as part of
# the artist, but not part of the name a lookup should search for.
_CREDIT = re.compile(r'^(?P<name>.+?)(?P<credit> with ".*")$')


def lookup_name(artist: str | None) -> str | None:
    """The artist as a lookup or search should ask for it: without a
    trailing `with "..."` credit, which no database files them under."""
    if not artist:
        return artist
    m = _CREDIT.match(artist)
    return m["name"] if m else artist


def the_first(artist: str) -> str:
    # Library order ("Pretenders, The") back to how the band is named,
    # keeping any credit after it.
    m = _CREDIT.match(artist)
    name, credit = (m["name"], m["credit"]) if m else (artist, "")
    if name.endswith(", The"):
        name = "The " + name[:-len(", The")]
    return name + credit


def _last_unquoted(text: str, sep: str) -> int | None:
    """Where the last `sep` outside "..." starts in `text`, or None."""
    found, quoted = None, False
    for i, ch in enumerate(text):
        if ch == '"':
            quoted = not quoted
        elif not quoted and text.startswith(sep, i):
            found = i
    return found


# The song runs to the first `" by `: later quotes belong to a credit or a
# show, and the artist between them is what gets looked up.
_WFMU_SONG = re.compile(r'^"(?P<song>.*?)" by (?P<rest>.+)$', re.DOTALL)
_WFMU_CREDITED = re.compile(r'^(?P<artist>[^"]+? with "[^"]*") on ')
_WFMU_SHOW = re.compile(r'^(?P<show>[^"]+?) with (?P<host>[^"]+?)(?: on WFMU)?$')


def wfmu(title: str) -> dict:
    """WFMU's `"Song" by Artist on Show on WFMU`, or a bare `Show with Host`
    at a show change; nothing for anything else ("Your DJ speaks over ...").

    The song is the leading quoted run up to the first `" by `, so its own
    " by ", " on " and inner quotes are safe (only a song containing `" by `
    itself would end early). Between artist and show, the first " on " after a
    quoted `with "..."` credit, else the last " on " outside quotes: an
    artist with " on " in the name keeps it, and a show with one loses its
    first half instead. The artist is what gets looked up; the
    show is only shown."""
    title = title.strip()
    if title.startswith("Your DJ "):
        return {}
    m = _WFMU_SONG.match(title)
    if m:
        artist, show = m["rest"].strip(), None
        if artist.endswith(" on WFMU"):
            artist = artist[:-len(" on WFMU")]
            # After a quoted credit the boundary is certain: the " on " right
            # past the credit's own closing quote. Without one, the last wins.
            credited = _WFMU_CREDITED.match(artist)
            cut = (credited.end("artist") if credited
                   else _last_unquoted(artist, " on "))
            if cut is not None:
                artist, show = artist[:cut], artist[cut + len(" on "):]
        fields = {"artist": the_first(artist.strip()), "song": m["song"].strip(),
                  "show": show.strip() if show else None}
        return {k: v for k, v in fields.items() if v}
    m = _WFMU_SHOW.match(title)
    if m:
        return {"show": m["show"].strip(), "hosts": [m["host"].strip()]}
    return {}


# iHeart's other variant: `Artist - text="Song" song_spot="M" ...
# amgArtworkURL="..." ...`, space-separated with no artist= of its own. A
# value runs to the quote that ends it before the next ` key="` or the end,
# so a quote inside the song doesn't cut it short.
_IHEART_SPACE = re.compile(r'^(?P<artist>.+?) - (?P<attrs>text=".*)$', re.DOTALL)
_IHEART_FIELD = re.compile(r'(\w+)="(.*?)"(?=\s+\w+="|\s*$)', re.DOTALL)


def iheart_space(title: str) -> dict:
    """comedy247's `Artist - text="Song" ... amgArtworkURL="..."`: the artist,
    the song out of text= and the artwork as the cover; iHeart's comma
    attributes as `iheart-attrs` reads them; nothing for anything else.

    Never a plain "Artist - Song" split: a title of this variant that lost its
    text= would hand on the whole attribute blob as the song."""
    title = title.strip()
    m = _IHEART_SPACE.match(title)
    if not m:
        return iheart_attrs(title)
    fields = dict(_IHEART_FIELD.findall(m["attrs"]))
    got = {"artist": m["artist"].strip(), "song": fields.get("text", "").strip(),
           "art_url": fields.get("amgArtworkURL", "").strip()}
    return {k: v for k, v in got.items() if v} if got["song"] else {}


_DOT_YEAR = re.compile(r"\s*\(\d{4}\)$")


def artist_dot_song(title: str) -> dict:
    """"Artist · Song (year)", split at the first " · " with a trailing
    four-digit year dropped (any other parentheses stay); nothing without one."""
    if " · " not in title:
        return {}
    artist, song = (s.strip() for s in title.split(" · ", 1))
    song = _DOT_YEAR.sub("", song)
    return {k: v for k, v in (("artist", artist), ("song", song)) if v}


# Anchored on the suffix, with exactly two hyphen-free fields before it, so a
# generic "A - B" (or KCRW's own "Song-Artist-Album" in a music show) never
# matches.
_KCRW = re.compile(r"^(?P<show>[^-]+)-(?P<host>[^-]+)-join\.kcrw\.com$")


def kcrw(title: str) -> dict:
    """KCRW's `Show-Host-join.kcrw.com`: the show and its host, never an
    artist; nothing for anything else."""
    m = _KCRW.match(title.strip())
    if not m or not m["show"].strip() or not m["host"].strip():
        return {}
    return {"show": m["show"].strip(), "hosts": [m["host"].strip()]}


SHAPES: dict[str, Callable[[str], dict]] = {
    "artist-song": artist_song,
    "iheart-attrs": iheart_attrs,
    "iheart-space": iheart_space,
    "artist-dot-song": artist_dot_song,
    "kcrw": kcrw,
    "wfmu": wfmu,
}


def parse(shape: str | None, title: str | None) -> dict:
    """The NowPlaying fields `title` yields under `shape`; None is the
    default (iHeart attributes, else "Artist - Song")."""
    if not title:
        return {}
    if shape is None:
        # Not `iheart_attrs or artist_song`: an iHeart title whose attributes
        # split to nothing must stay nothing, not fall back to a split of the
        # attribute text itself (a url="Promo - Break").
        return artist_song(iheart_rewrite(title))
    return SHAPES[shape](title)
