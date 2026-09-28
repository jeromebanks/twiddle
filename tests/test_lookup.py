"""Artist identification against canned MusicBrainz answers -- no network.

The point of `lookup` is picking the *right* artist, so these tests are
mostly about identity: the album wins over the bare name, a shared name
yields candidates rather than a guess, and Wikipedia is only reached through
the identified entity's own Wikidata link.
"""
import pytest

from twiddle import lookup


def _rels(qid=None, bandcamp=None):
    out = []
    if qid:
        out.append({"type": "wikidata", "url": {"resource": f"https://www.wikidata.org/wiki/{qid}"}})
    if bandcamp:
        out.append({"type": "bandcamp", "url": {"resource": bandcamp}})
    return out


class FakeWeb:
    """Routes URLs to canned JSON and records what was asked."""

    def __init__(self, routes):
        self.routes = routes  # substring -> response
        self.urls = []

    def __call__(self, url, headers=None):
        self.urls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                return resp
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(lookup.time, "sleep", lambda s: None)

    def install(routes):
        fake = FakeWeb(routes)
        monkeypatch.setattr(lookup, "_get_json", fake)
        return fake
    return install


ARTIST_SPOON = {"name": "Spoon", "type": "Group", "life-span": {"begin": "1994"},
                "area": {"name": "United States"}, "begin-area": {"name": "Austin"},
                "genres": [{"name": "rock", "count": 1}, {"name": "indie rock", "count": 5}],
                "relations": _rels("Q1", "https://spoon.bandcamp.com/")}
WIKI = {"wbgetentities": {"entities": {"Q1": {"sitelinks": {"enwiki": {"title": "Spoon (band)"}}}}},
        "page/summary": {"description": "American rock band", "extract": "Spoon is a band.",
                         "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Spoon_(band)"}}}}


def test_album_settles_identity_and_brings_album_details(web):
    fake = web({
        "release-group/?": {"release-groups": [
            {"score": 100, "id": "rg-jam", "title": "Kaleidoscope",
             "artist-credit": [{"name": "Jam & Spoon", "artist": {"id": "jam"}}]},
            {"score": 98, "id": "rg1", "title": "Kill the Moonlight",
             "artist-credit": [{"name": "Spoon", "artist": {"id": "spoon"}}]}]},
        "artist/spoon": ARTIST_SPOON,
        "release-group/rg1": {"title": "Kill the Moonlight", "first-release-date": "2002-08-20",
                              "primary-type": "Album", "secondary-types": [], "relations": []},
        **WIKI,
    })
    r = lookup.identify("Spoon", "Kill The Moonlight", use_cache=False)
    assert r.artist.mbid == "spoon"
    assert r.artist.album.year == "2002"
    assert r.artist.album.kind == "Album"
    assert r.artist.genres[0] == "indie rock", "genres should be ordered by vote count"
    assert r.artist.links["wikipedia"].endswith("Spoon_(band)")
    assert "artist + album" in r.artist.matched_by
    # the name-only search was never needed
    assert not any("/artist/?" in u for u in fake.urls)


def test_a_shared_name_returns_candidates_not_a_guess(web):
    web({"artist/?": {"artists": [
        {"score": 100, "id": "a1", "name": "Low", "disambiguation": "US slowcore band"},
        {"score": 100, "id": "a2", "name": "Low", "disambiguation": "UK electronic"},
        {"score": 60, "id": "a3", "name": "Lowell"}]}})
    r = lookup.identify("Low", use_cache=False)
    assert r.artist is None
    assert [c["mbid"] for c in r.candidates] == ["a1", "a2"]
    assert "--album" in lookup.render(r, "Low")


def test_station_supplied_ids_skip_every_search(web):
    fake = web({"artist/spoon": ARTIST_SPOON, **WIKI})
    r = lookup.identify("Spoon", mb_artist_id="spoon", use_cache=False)
    assert r.artist.name == "Spoon"
    assert not any("query=" in u for u in fake.urls)


def test_no_wikidata_link_means_no_wikipedia_call(web):
    fake = web({"artist/?": {"artists": [{"score": 100, "id": "tt", "name": "(T-T)b"}]},
                "artist/tt": {"name": "(T-T)b", "relations": _rels(bandcamp="https://t-tb.bandcamp.com/")}})
    r = lookup.identify("(T-T)b", use_cache=False)
    assert r.artist.summary is None
    assert r.artist.links["bandcamp"] == "https://t-tb.bandcamp.com/"
    assert not any("wiki" in u for u in fake.urls)


def test_second_ask_comes_from_the_cache(web):
    fake = web({"artist/spoon": ARTIST_SPOON, **WIKI})
    lookup.identify("Spoon", mb_artist_id="spoon")
    n = len(fake.urls)
    again = lookup.identify("Spoon", mb_artist_id="spoon")
    assert len(fake.urls) == n
    assert again.artist.name == "Spoon"


@pytest.mark.parametrize("a,b", [
    ("In The Aeroplane Over The Sea", "In the Aeroplane Over the Sea"),
    ("The Beatles", "Beatles"),
    ("Simon & Garfunkel", "Simon and Garfunkel"),
    ("Björk", "Bjork"),
])
def test_norm_treats_hand_typed_variants_as_equal(a, b):
    assert lookup.norm(a) == lookup.norm(b)


@pytest.mark.parametrize("title,bare", [
    ("Kind Of Blue (Legacy Edition)", "Kind Of Blue"),
    ("OK Computer (Remastered)", "OK Computer"),
    ("Songs for Drella", "Songs for Drella"),
    ("Songs (for Drella)", "Songs (for Drella)"),  # not an edition suffix
])
def test_bare_title_strips_only_edition_suffixes(title, bare):
    assert lookup.bare_title(title) == bare


def test_non_latin_names_keep_their_identity():
    """ASCII-folding once made every CJK name norm to "", so any two matched."""
    assert lookup.norm("坂本龍一") != lookup.norm("細野晴臣")
    assert not lookup._same("!!!", "")
    assert lookup._same("Sigur Rós", "sigur ros")


def test_a_stale_cache_entry_is_not_served(web, monkeypatch):
    fake = web({"artist/spoon": ARTIST_SPOON, **WIKI})
    lookup.identify("Spoon", mb_artist_id="spoon")
    n = len(fake.urls)
    real_time = lookup.time.time
    monkeypatch.setattr(lookup.time, "time", lambda: real_time() + lookup.CACHE_TTL_S + 1)
    lookup.identify("Spoon", mb_artist_id="spoon")
    assert len(fake.urls) > n



# ---- beyond MusicBrainz's exact names ------------------------------------------

SABOTAGE_MB = {"name": "Sabotage qu\u2019est\u2010ce que c\u2019est?", "type": "Group",
               "relations": [{"type": "discogs",
                              "url": {"resource": "https://www.discogs.com/artist/69880"}}]}


def test_abbreviated_name_is_accepted_only_when_the_song_confirms_it(web):
    """KALX logged "Sabotage Q.C.Q.C.?"; MusicBrainz spells it out in French."""
    fake = web({
        "artist/?query=artist": {"artists": []},             # exact phrase: nothing
        "AND+artist%3A%22Sabotage": {"recordings": []},      # exact artist + song: nothing
        "artist/?query=sabotage": {"artists": [
            {"score": 100, "id": "rapper", "name": "Sabotage"},
            {"score": 90, "id": "qcqc", "name": SABOTAGE_MB["name"]}]},
        "arid%3Arapper": {"count": 0},                       # the rapper never recorded it
        "arid%3Aqcqc": {"count": 1},
        "artist/qcqc": SABOTAGE_MB,
        "api.discogs.com/artists/69880": {"members": [{"name": "Marc Werner"},
                                                      {"name": "Tim Kroker", "active": False}],
                                          "profile": "German [a=Pop] band."},
    })
    r = lookup.identify("Sabotage Q.C.Q.C.?", song="Sex Dwarf", use_cache=False)
    assert r.artist.mbid == "qcqc"
    assert "confirmed by the song" in r.artist.matched_by
    assert r.artist.members == ["Marc Werner"]
    assert (r.artist.summary, r.artist.summary_source) == ("German Pop band.", "Discogs")
    assert any("arid%3Arapper" in u for u in fake.urls), "the rapper was checked, then rejected"


def test_a_loose_match_without_a_song_or_album_is_never_accepted(web, monkeypatch):
    fake = web({"artist/?query=artist": {"artists": []},
                "database/search": {"results": []}})
    monkeypatch.setattr(lookup, "_by_bandcamp", lambda artist: None)
    r = lookup.identify("Sabotage Q.C.Q.C.?", use_cache=False)
    assert r.artist is None
    assert not any("query=sabotage" in u for u in fake.urls), "no loose search without evidence"


def test_discogs_answers_when_musicbrainz_cannot(web, monkeypatch):
    web({"artist/?query=artist": {"artists": []},
         "database/search": {"results": [{"id": 69880, "title": "Sabotage Q.C.Q.C.?"},
                                         {"id": 1, "title": "Sabotage (2)"}]},
         "api.discogs.com/artists/69880": {"name": "Sabotage Q.C.Q.C.?",
                                           "uri": "https://www.discogs.com/artist/69880",
                                           "members": [{"name": "Tim Kroker (2)"}],
                                           "urls": ["https://sabotage.bandcamp.com"]}})
    monkeypatch.setattr(lookup, "_by_bandcamp",
                        lambda a: (_ for _ in ()).throw(AssertionError("Discogs sufficed")))
    r = lookup.identify("Sabotage Q.C.Q.C.?", use_cache=False)
    assert r.artist.name == "Sabotage Q.C.Q.C.?"
    assert r.artist.members == ["Tim Kroker"]
    assert r.artist.links["bandcamp"] == "https://sabotage.bandcamp.com"
    assert "Discogs" in r.artist.matched_by


def test_bandcamp_is_the_last_resort(web, monkeypatch):
    web({"artist/?query=artist": {"artists": []}, "database/search": {"results": []}})
    monkeypatch.setattr(lookup, "_by_bandcamp", lambda a: lookup.ArtistInfo(
        name=a, origin="Boston", links={"bandcamp": "https://x.bandcamp.com"}))
    r = lookup.identify("Tiny Band", use_cache=False)
    assert r.artist.links == {"bandcamp": "https://x.bandcamp.com"}
    assert "Bandcamp" in r.artist.matched_by


def test_a_source_being_down_falls_through_to_the_next(web, monkeypatch):
    web({"artist/?query=artist": {"artists": []}})
    monkeypatch.setattr(lookup, "_discogs", lambda *a, **k: (_ for _ in ()).throw(OSError("503")))
    monkeypatch.setattr(lookup, "_by_bandcamp", lambda a: lookup.ArtistInfo(name=a))
    assert lookup.identify("Tiny Band", use_cache=False).artist.name == "Tiny Band"


def test_discogs_markup_is_flattened():
    assert lookup.discogs_markup("By [a=Jeff Mangum] on [l=Merge] [a123]"
                                 "[url=http://x]site[/url] [b]now[/b]") == \
        "By Jeff Mangum on Merge site now"


def test_discogs_token_is_saved_private_and_env_overrides_it(monkeypatch):
    path = lookup.save_discogs_token(" abc123 \n")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert lookup.discogs_token() == "abc123"
    monkeypatch.setenv("DISCOGS_TOKEN", "from-env")
    assert lookup.discogs_token() == "from-env"
