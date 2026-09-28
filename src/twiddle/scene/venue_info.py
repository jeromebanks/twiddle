"""Where a venue is, what it is, and its website -- for `i` and `scene venue`.

Hand-kept, like the icons, because nothing machine-readable covers small
rooms: Wikipedia has 19 of these, and none of the DIY spaces. Checked
2026-09-25: every address was confirmed on the venue's own site, its
Wikipedia article, or listings sites, and every website loaded. Two
corrections came out of that: Regency's official address is 1300 Van Ness
(Wikipedia gives 1270 Sutter; the building is on the corner), and Stay Gold
Deli's domain now redirects to a running club, so it links Instagram.

`wikipedia` is an article *title*, set only where the article is about this
room; its summary is fetched on demand (`wiki_summary`) and cached.

Pussy Palace is a private house. Its street is public (it's how the shows
are described), its number is not, so none is recorded here.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, replace

import requests

from . import cache


@dataclass(frozen=True)
class VenueInfo:
    address: str = ""
    url: str = ""
    about: str = ""
    wikipedia: str = ""         # an article title, only where one is about this room
    instagram: str = ""         # a handle, from INSTAGRAM below

    @property
    def instagram_url(self) -> str:
        return f"https://www.instagram.com/{self.instagram}/" if self.instagram else ""

    @property
    def map_url(self) -> str:
        if not self.address:
            return ""
        return "https://www.google.com/maps/search/?api=1&query=" + \
            urllib.parse.quote(self.address)

    def __bool__(self) -> bool:
        return bool(self.address or self.url or self.about or self.wikipedia)


I = VenueInfo
INFO: dict[str, VenueInfo] = {
    # ---- East Bay ------------------------------------------------------------
    "Stork Club": I(
        "2330 Telegraph Ave, Oakland, CA 94612", "https://theestorkclub.com/",
        "A nearly century-old dive bar in KONO, run since 2022 by the people behind "
        "Eli's and Mosswood Meltdown. Punk, garage and noise. 21+."),
    "Ivy Room": I(
        "860 San Pablo Ave, Albany, CA 94706", "https://www.ivyroom.com/",
        "Albany bar with a small stage: punk, rock, country and soul nights."),
    "Stay Gold Deli": I(
        "2635 San Pablo Ave, Oakland, CA 94612", "https://www.instagram.com/staygoldoakland/",
        "A deli by day; punk shows in the beer garden at night."),
    "Eli's Mile High": I(
        "3629 Martin Luther King Jr Way, Oakland, CA 94609",
        "https://www.elismilehighclub.com/",
        "Small North Oakland bar with a long blues history, now mostly punk, garage "
        "and rock."),
    "924 Gilman": I(
        "924 Gilman St, Berkeley, CA 94710", "https://www.924gilman.org/",
        "All-ages, volunteer-run punk club since 1986. No alcohol.",
        "924 Gilman Street"),
    "Yoshi's": I(
        "510 Embarcadero West, Oakland, CA 94607", "https://yoshis.com/",
        "Jazz club and restaurant in Jack London Square: jazz, R&B, soul and funk, "
        "seated, often two sets a night.", "Yoshi's"),
    "Greek Theatre": I(
        "2001 Gayley Rd, Berkeley, CA 94720", "https://thegreekberkeley.com/",
        "UC Berkeley's open-air amphitheatre in the hills above campus.",
        "Hearst Greek Theatre"),
    "Fox Theater": I(
        "1807 Telegraph Ave, Oakland, CA 94612", "https://thefoxoakland.com/",
        "A restored 1928 movie palace, now a 2,800-capacity concert hall.",
        "Fox Oakland Theatre"),
    "Paramount": I(
        "2025 Broadway, Oakland, CA 94612", "https://www.paramountoakland.org/",
        "Art Deco concert hall, mostly seated shows.",
        "Paramount Theatre (Oakland, California)"),
    "UC Theatre": I(
        "2036 University Ave, Berkeley, CA 94704", "https://www.theuctheatre.org/",
        "A former movie theatre in downtown Berkeley, now a nonprofit music hall.",
        "UC Theatre"),
    "Cornerstone": I(
        "2367 Shattuck Ave, Berkeley, CA 94704", "https://www.cornerstoneberkeley.com/",
        "Craft-beer bar and music room in downtown Berkeley."),
    "Freight": I(
        "2020 Addison St, Berkeley, CA 94704", "https://thefreight.org/",
        "Freight & Salvage: a nonprofit folk and roots music hall, running since "
        "1968. Only some of its shows are on The List."),
    "Starry Plough": I(
        "3101 Shattuck Ave, Berkeley, CA 94705", "https://thestarryplough.com/",
        "Irish pub in South Berkeley with folk, punk and poetry nights."),
    "Crybaby": I(
        "1928 Telegraph Ave, Oakland, CA 94612", "https://crybaby.live/",
        "Club across the street from the Fox: dance nights and smaller live shows."),
    "Buzzard": I(
        "2601 Adeline St, Oakland, CA 94607",
        "https://www.facebook.com/profile.php?id=128025580608683",
        "First Church of the Buzzard: a DIY space in West Oakland for deathrock, "
        "goth and punk, plus art and poetry."),
    "Oakland Secret": I(
        "577 5th St, Oakland, CA 94607", "https://www.instagram.com/oakland.secret/",
        "A community art and music space in West Oakland's industrial zone: shows "
        "and parties, some outdoors. Listings are on its Instagram and RA, which "
        "the app can't read, so shows appear here only when The List carries them."),
    "Pussy Palace": I(
        "34th St, West Oakland, CA",
        "https://www.facebook.com/p/Pussy-Palace-100068552886969/",
        "A punk house: a private home, so ask for the number. Monthly backyard shows "
        "on Sunday afternoons, all ages, and a basement record shop (Cat-a-Comb "
        "Records). Shows are announced on its socials."),
    "Ashkenaz": I(
        "1317 San Pablo Ave, Berkeley, CA 94702", "https://www.ashkenaz.com/",
        "Berkeley's folk and world music dance hall since 1973: Balkan, Cajun, "
        "reggae, salsa and swing, often with a dance lesson first."),
    "Sound Room": I(
        "3022 Broadway, Oakland, CA 94611", "https://www.soundroom.org/",
        "A listening room for jazz, run by the nonprofit Bay Area Jazz & Arts: "
        "the performance is the point, so it's quiet during sets."),
    "Spats": I(
        "1974 Shattuck Ave, Berkeley, CA 94704", "https://www.instagram.com/spatsbar/",
        "Old downtown Berkeley bar, now with comedy and music in its back room. "
        "No website or readable calendar; shows are announced on Instagram."),
    # ---- San Francisco -------------------------------------------------------
    "The DeLuxe": I(
        "1511 Haight St, San Francisco, CA 94117", "https://thedeluxesf.com/",
        "Club Deluxe reopened (it closed in 2023): a Haight Street bar with live "
        "jazz, swing, blues and rockabilly most nights, cheap covers."),
    "Gray Area": I(
        "2665 Mission St, San Francisco, CA 94110", "https://grayarea.org/",
        "Art and technology center in the old Grand Theater: electronic and "
        "experimental concerts, immersive shows, talks and courses."),
    "Make-Out Room": I(
        "3225 22nd St, San Francisco, CA 94110", "https://www.makeoutroom.com/",
        "Mission bar and legacy business: early-evening bands, readings and "
        "comedy, then DJ dance nights until 2am, most of them free."),
    "Great American": I(
        "859 O'Farrell St, San Francisco, CA 94109", "https://gamh.com/",
        "Ornate concert hall in the Tenderloin, with a balcony.",
        "Great American Music Hall"),
    "The Midway": I(
        "900 Marin St, San Francisco, CA 94124", "https://themidwaysf.com/",
        "A warehouse arts and nightlife complex near the Bayview waterfront: "
        "mostly electronic and dance, some bands."),
    "Bottom of the Hill": I(
        "1233 17th St, San Francisco, CA 94107", "https://www.bottomofthehill.com/",
        "Potrero Hill rock club since 1991, where Green Day, the Strokes and the "
        "White Stripes played early. Closing after New Year's Eve 2026.",
        "Bottom of the Hill"),
    "Independent": I(
        "628 Divisadero St, San Francisco, CA 94117", "https://www.theindependentsf.com/",
        "Mid-size club on Divisadero: indie, rock and hip-hop."),
    "Rickshaw Stop": I(
        "155 Fell St, San Francisco, CA 94102", "https://rickshawstop.com/",
        "Small club by Hayes Valley: indie bands and dance nights."),
    "Fillmore": I(
        "1805 Geary Blvd, San Francisco, CA 94115", "https://www.thefillmore.com/",
        "The historic ballroom that Bill Graham's 1960s shows made famous.",
        "The Fillmore"),
    "Kilowatt": I(
        "3160 16th St, San Francisco, CA 94103", "https://kilowattbar.com/",
        "Mission dive bar with a small stage: punk and noise. 21+."),
    "Chapel": I(
        "777 Valencia St, San Francisco, CA 94110", "https://thechapelsf.com/",
        "A former mortuary chapel on Valencia, now a music hall with a bar and "
        "restaurant."),
    "Regency": I(
        "1300 Van Ness Ave, San Francisco, CA 94109", "https://www.theregencyballroom.com/",
        "Ornate ballroom at Van Ness and Sutter, run by Goldenvoice."),
    "August Hall": I(
        "420 Mason St, San Francisco, CA 94102", "https://www.augusthallsf.com/",
        "Club near Union Square, in the old Ruby Skye.", "August Hall"),
    "Warfield": I(
        "982 Market St, San Francisco, CA 94102", "https://www.thewarfieldtheatre.com/",
        "A vaudeville-era theatre on Market St, now a concert hall.", "Warfield Theatre"),
    "Masonic": I(
        "1111 California St, San Francisco, CA 94108", "https://www.sfmasonic.com/",
        "Auditorium atop Nob Hill: seated concerts and comedy.", "SF Masonic Auditorium"),
    "Bimbo's": I(
        "1025 Columbus Ave, San Francisco, CA 94133", "https://bimbos365club.com/",
        "Family-run North Beach supper club with a big dance floor.",
        "Bimbo's 365 Club"),
    "Brick & Mortar": I(
        "1710 Mission St, San Francisco, CA 94103", "https://www.brickandmortarmusic.com/",
        "Small, loud club on Mission St: punk, garage and hip-hop."),
    "DNA Lounge": I(
        "375 11th St, San Francisco, CA 94103", "https://www.dnalounge.com/",
        "All-ages SoMa nightclub with a pizza place next door: goth, industrial, "
        "drag and dance nights.", "DNA Lounge"),
    "Castro Theatre": I(
        "429 Castro St, San Francisco, CA 94114", "https://thecastro.com/",
        "A historic movie palace in the Castro, now also a concert venue.",
        "Castro Theatre"),
    "Great Northern": I(
        "119 Utah St, San Francisco, CA 94103", "https://www.thegreatnorthernsf.com/",
        "SoMa nightclub with art deco interiors: mostly DJs and electronic."),
    "Cafe du Nord": I(
        "2174 Market St, San Francisco, CA 94114", "https://cafedunord.com/",
        "Basement bar and music room under the Swedish American Hall.", "Cafe Du Nord"),
    "Swedish Am. Hall": I(
        "2174 Market St, San Francisco, CA 94114", "https://cafedunord.com/",
        "The hall upstairs from Cafe du Nord, same building and bookers."),
    "Knockout": I(
        "3223 Mission St, San Francisco, CA 94110", "https://theknockoutsf.com/",
        "Outer Mission dive bar with a stage: punk, garage, soul and DJ nights."),
    "Hotel Utah": I(
        "500 4th St, San Francisco, CA 94107", "https://hotelutah.com/",
        "Historic SoMa saloon with a small stage for songwriters and indie bands.",
        "Hotel Utah (San Francisco)"),
    "Neck of the Woods": I(
        "406 Clement St, San Francisco, CA 94118", "https://www.neckofthewoodssf.com/",
        "Two-room bar in the Inner Richmond: bands, comedy and dance nights."),
    "4 Star": I(
        "2200 Clement St, San Francisco, CA 94121", "https://www.4-star-movies.com/",
        "A Richmond District movie theatre that also hosts punk and rock shows."),
    "Black Cat": I(
        "400 Eddy St, San Francisco, CA 94109", "https://blackcatsf.com/",
        "Jazz supper club in the Tenderloin."),
    "Civic": I(
        "99 Grove St, San Francisco, CA 94102", "https://billgrahamcivic.com/",
        "Bill Graham Civic Auditorium: the big hall at Civic Center.",
        "Bill Graham Civic Auditorium"),
}

# Instagram handles, as each venue's own site links them (2026-09-26), and
# each checked to be a real profile with a picture. Three are left out on
# purpose: Freight and Buzzard have none that resolves, and @bottomofthehill
# is someone else. Used for the venue's picture when no logo is hand-picked,
# and opened by double-clicking the venue card.
INSTAGRAM = {
    "Stork Club": "theestorkcluboakland", "Ivy Room": "ivyroom",
    "Stay Gold Deli": "staygoldoakland", "Eli's Mile High": "elismilehighclub",
    "924 Gilman": "924gilmanstreet", "Great American": "greatamericanmusichall",
    "Yoshi's": "yoshis_oak", "Greek Theatre": "greekberkeley", "Fox Theater": "foxoakland",
    "Paramount": "oakparamount", "The Midway": "themidwaysf", "UC Theatre": "theuctheatre",
    "Cornerstone": "cornerstoneberkeley", "Starry Plough": "thestarryploughpub",
    "Crybaby": "crybabyoakland", "Independent": "theindependentsf",
    "Rickshaw Stop": "rickshawstop", "Fillmore": "thefillmore", "Kilowatt": "kilowatt_bar_sf",
    "Chapel": "thechapelsf", "Regency": "theregencyballroom", "August Hall": "augusthallsf",
    "Warfield": "thewarfield", "Masonic": "sfmasonic", "Bimbo's": "bimbos365club",
    "Brick & Mortar": "brickmortarsf", "DNA Lounge": "dnalounge", "Castro Theatre": "thecastro_sf",
    "Great Northern": "greatnorthernsf", "Cafe du Nord": "cafedunord",
    "Swedish Am. Hall": "swedishamericanhall", "Knockout": "theknockoutsf",
    "Hotel Utah": "hotelutah", "Neck of the Woods": "neckofthewoodssf", "4 Star": "4startheater",
    "Black Cat": "sfblackcat", "Civic": "billgrahamcivic", "Oakland Secret": "oakland.secret",
    # added 2026-09-26; Spats has no website, so its handle is from its listings
    "Ashkenaz": "ashkenazberkeley", "Sound Room": "thesoundroomoakland", "Spats": "spatsbar",
    "The DeLuxe": "thedeluxesf", "Gray Area": "grayareaorg", "Make-Out Room": "makeoutroomsf",
}
INFO = {name: replace(info, instagram=INSTAGRAM.get(name, "")) for name, info in INFO.items()}
del I


# ---- Wikipedia --------------------------------------------------------------

WIKI_TTL_S = 30 * 86400
WIKI_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"


def wiki_summary(title: str, timeout: float = 10.0) -> dict | None:
    """{"extract", "url"} for an article, cached 30 days; None if unavailable.

    Network failures return None rather than raising: this is garnish on a
    screen that is already useful without it.
    """
    if not title:
        return None
    store = cache._read("venue_wiki.json")
    hit = store.get(title)
    if hit and time.time() - hit.get("at", 0) < WIKI_TTL_S:
        return hit.get("page")
    try:
        resp = requests.get(WIKI_API + urllib.parse.quote(title.replace(" ", "_"), safe=""),
                            timeout=timeout,
                            headers={"User-Agent": "twiddle scene (venue info)"})
        page = None
        if resp.status_code == 200:
            d = resp.json()
            page = {"extract": d.get("extract", ""),
                    "url": d.get("content_urls", {}).get("desktop", {}).get("page", "")}
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return None
    store[title] = {"at": time.time(), "page": page}
    cache._write("venue_wiki.json", store)
    return page
