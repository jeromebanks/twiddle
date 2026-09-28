"""A best guess at what a band -- and so a show -- sounds like.

No AI: bands tag themselves on Bandcamp ("doom", "sludge", "post-metal"),
MusicBrainz users tag artists, and those tags fold into a dozen coarse
families -- enough to tell a metal night from a reggae one.

Tags are looked up *whole* in `TAGS`, never by substring: "post-punk" is not
punk, "dubstep" is not dub, "folk punk" is punk, and a tag that isn't in the
table ("San Francisco", "soundtrack") counts for nothing. A multi-word tag
not in the table falls back to its last word ("atmospheric sludge" ->
"sludge"), which is how genre names are built.

Bandcamp's own `genre_name` (the one genre the artist picked) counts double.

A guess is only as good as the band it came from. `sure` is False when the
Bandcamp page was picked by name alone and isn't local -- measured on the
2026-09-25 listing: "Inayah" at the Great American (an R&B singer) is, by
name, a French death metal band. Unsure guesses are shown with a "?".
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# family -> (short label, Rich colour)
FAMILIES = {
    "metal": ("metal", "red"),
    "punk": ("punk", "bright_red"),
    "hardcore": ("hardcore", "dark_orange"),
    "rock": ("rock", "yellow"),
    "indie": ("indie", "khaki1"),
    "pop": ("pop", "hot_pink"),
    "electronic": ("electronic", "cyan"),
    "hiphop": ("hip-hop", "orange1"),
    "reggae": ("reggae/ska", "green"),
    "jazz": ("jazz", "medium_purple1"),
    "folk": ("folk/country", "tan"),
    "soul": ("soul/funk", "magenta"),
    "latin": ("latin", "spring_green2"),
    "experimental": ("experimental", "grey70"),
    "blues": ("blues", "steel_blue1"),
    "classical": ("classical", "light_sky_blue1"),
    "comedy": ("comedy", "wheat1"),
}

_F = {
    "metal": """metal, heavy metal, doom, doom metal, sludge, sludge metal, stoner,
        stoner metal, stoner rock, black metal, death metal, thrash, thrash metal,
        grindcore, grind, post-metal, drone metal, speed metal, power metal,
        death doom, blackened, deathcore, metalcore, goregrind, war metal,
        occult rock, heavy psych""",
    "punk": """punk, punk rock, hardcore punk, pop punk, folk punk, skate punk,
        street punk, streetpunk, oi, oi!, crust, crust punk, d-beat, anarcho-punk,
        garage punk, queercore, riot grrrl, egg punk, synth punk, horror punk,
        ska punk, dbeat""",
    "hardcore": """hardcore, powerviolence, beatdown, straight edge, metallic hardcore,
        youth crew, screamo, emoviolence, mathcore""",
    "rock": """rock, garage rock, garage, psych, psychedelic, psychedelic rock,
        psych rock, hard rock, surf, surf rock, rock and roll, rock n roll,
        rock'n'roll, grunge, noise rock, post-rock, blues rock, southern rock,
        glam, glam rock, power pop, rockabilly, space rock, krautrock,
        alternative rock, alternative, garage psych""",
    "indie": """indie, indie rock, indie pop, post-punk, shoegaze, dream pop,
        slowcore, emo, midwest emo, math rock, jangle, jangle pop, lo-fi,
        bedroom pop, twee, new wave, coldwave, darkwave, goth, gothic rock,
        deathrock, art rock, noise pop, sadcore""",
    "pop": """pop, synthpop, synth-pop, electropop, art pop, hyperpop, k-pop,
        dance pop, pop rock, chamber pop""",
    "electronic": """electronic, electronica, techno, house, deep house, tech house,
        ambient, idm, edm, dance, electro, breakbeat, drum and bass, drum & bass,
        dnb, jungle, dubstep, trance, synthwave, vaporwave, downtempo, trip-hop,
        trip hop, footwork, industrial, ebm, dark ambient, experimental electronic,
        club, acid, beats, electronic music""",
    "hiphop": """hip hop, hip-hop, rap, trap, boom bap, underground hip hop, drill,
        conscious hip hop, instrumental hip hop, grime, hip hop/rap, hip-hop/rap""",
    "reggae": """reggae, ska, dub, rocksteady, dancehall, roots reggae, ska revival,
        two tone, 2 tone, lovers rock""",
    "jazz": """jazz, free jazz, jazz fusion, fusion, bebop, avant-jazz,
        spiritual jazz, big band, swing, smooth jazz, latin jazz""",
    "folk": """folk, americana, country, alt-country, bluegrass, singer-songwriter,
        acoustic, indie folk, freak folk, folk rock, old-time, country rock,
        outlaw country, honky tonk""",
    "soul": """soul, r&b, rnb, r&b/soul, funk, disco, gospel, neo-soul, motown,
        northern soul, rhythm and blues""",
    "latin": """latin, cumbia, salsa, reggaeton, bossa nova, bachata, tropical,
        latin rock, rock en español, norteño, son, banda""",
    "experimental": """experimental, noise, avant-garde, avant garde, improvisation,
        improv, free improvisation, drone, sound art, musique concrete,
        harsh noise, power electronics, field recordings""",
    "blues": """blues, delta blues, chicago blues, electric blues""",
    "classical": """classical, contemporary classical, modern classical, opera,
        orchestral, chamber music, minimalism, neoclassical""",
    "comedy": """comedy, stand-up, stand-up comedy, spoken word""",
}

TAGS: dict[str, str] = {tag.strip(): fam for fam, blob in _F.items()
                        for tag in " ".join(blob.split()).split(",") if tag.strip()}


def family_of(tag: str) -> str | None:
    t = " ".join(tag.lower().replace("_", " ").split())
    return TAGS.get(t) or (TAGS.get(t.rsplit(" ", 1)[-1]) if " " in t else None)


@dataclass
class Guess:
    scores: Counter = field(default_factory=Counter)
    tags: list[str] = field(default_factory=list)     # the evidence, as tagged
    sources: set[str] = field(default_factory=set)
    sure: bool = True

    @property
    def top(self) -> list[str]:
        """The leading family, and the runner-up when it's close behind."""
        ranked = self.scores.most_common(2)
        if not ranked:
            return []
        if len(ranked) == 2 and ranked[1][1] >= 0.6 * ranked[0][1]:
            return [ranked[0][0], ranked[1][0]]
        return [ranked[0][0]]

    def label(self) -> str:
        return "/".join(FAMILIES[f][0] for f in self.top) + ("" if self.sure else "?")

    def __bool__(self) -> bool:
        return bool(self.scores)


def guess(bandcamp_genre: str | None = None, bandcamp_tags=(), mb_genres=()) -> Guess:
    g = Guess()
    for tag, weight, src in ([(bandcamp_genre, 2.0, "Bandcamp")] if bandcamp_genre else []) \
            + [(t, 1.0, "Bandcamp") for t in bandcamp_tags or ()] \
            + [(t, 1.0, "MusicBrainz") for t in mb_genres or ()]:
        fam = family_of(tag or "")
        if fam:
            g.scores[fam] += weight
            g.sources.add(src)
            if tag.lower() not in (x.lower() for x in g.tags):
                g.tags.append(tag)
    return g


def for_band(bc: dict | None, mb_genres=(), sure: bool = True) -> Guess:
    """From a chosen Bandcamp band (`bandcamp.choose`) and MusicBrainz genres."""
    bc = bc or {}
    g = guess(bc.get("genre_name"), bc.get("tag_names") or (), mb_genres)
    g.sure = sure
    return g


def for_candidates(bands: list[dict]) -> Guess | None:
    """Several same-named Bandcamp bands, none of them chosen: if they all
    sound alike, that's the answer anyway (measured: Thelma And The Sleaze
    has two pages, both Nashville rock). Never sure -- it's not one band."""
    guesses = [for_band(b) for b in bands if not b.get("is_label")]
    guesses = [g for g in guesses if g]
    if not guesses or len({g.top[0] for g in guesses}) != 1:
        return None
    g = guesses[0]
    g.sure = False
    return g


def for_show(band_guesses: list[Guess | None]) -> Guess:
    """A show's sound: its bands' families, the headliner weighted most."""
    g = Guess()
    for i, b in enumerate(band_guesses):
        if not b:
            continue
        total = sum(b.scores.values())
        w = 1.5 if i == 0 else 1.0
        for fam, s in b.scores.items():
            g.scores[fam] += w * s / total      # each band votes once, however tagged
        g.sources |= b.sources
    # Sure if a band behind the *leading* genre is: a sure opener in another
    # genre must not vouch for a headliner's by-name guess.
    lead = g.top[0] if g.scores else None
    backers = [b for b in band_guesses if b and lead in b.scores]
    g.sure = any(b.sure for b in backers) if backers else True
    return g
