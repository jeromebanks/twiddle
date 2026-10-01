"""Band identity grading and the enrichment threading.

The failure this guards against is playing the wrong band: "Shape" at the
Stork Club is not whichever Shape Spotify returns first.
"""
import threading
import time

from twiddle import lookup, spotify, spotify_ops
from twiddle.scene import cache
from twiddle.scenedata import bands
from twiddle.scenespec.band import CORROBORATED, NAME_ONLY, NONE, UNCERTAIN, BandProfile
from twiddle.scene.book import BandBook
from twiddle.scenedata.bands import LookupEnricher, SpotifyEnricher, assess


def artist(name, aid):
    return {"name": name, "id": aid, "uri": f"spotify:artist:{aid}", "type": "artist"}


def done(p, **kw):
    p.status = {"lookup": "done", "spotify": "done"}
    for k, v in kw.items():
        setattr(p, k, v)
    assess(p)
    return p


def test_a_unique_name_alone_is_not_confirmation():
    p = done(BandProfile("Eraser"), spotify_artist=artist("Eraser", "e1"))
    assert p.confidence == NAME_ONLY
    assert "not confirmed" in p.why


def test_musicbrainz_linking_this_spotify_artist_confirms_it():
    info = lookup.ArtistInfo(name="Chuck Johnson", mbid="m",
                             links={"spotify": "https://open.spotify.com/artist/c1"})
    p = done(BandProfile("Chuck Johnson"), info=info, spotify_artist=artist("Chuck Johnson", "c1"))
    assert p.confidence == CORROBORATED


def test_musicbrainz_linking_a_different_spotify_artist_is_a_warning():
    info = lookup.ArtistInfo(name="Shape", mbid="m",
                             links={"spotify": "https://open.spotify.com/artist/local"})
    p = done(BandProfile("Shape"), info=info, spotify_artist=artist("Shape", "famous"))
    assert p.confidence == UNCERTAIN


def test_unique_on_both_databases_and_local_counts_as_confirmed():
    info = lookup.ArtistInfo(name="Girl Chow", origin="Oakland, California",
                             matched_by="unique name match on Bandcamp")
    p = done(BandProfile("Girl Chow"), info=info, spotify_artist=artist("Girl Chow", "g"))
    assert p.confidence == CORROBORATED and "Bandcamp" in p.why


def test_a_local_band_found_only_loosely_is_not_confirmed():
    info = lookup.ArtistInfo(name="X", origin="Oakland", matched_by="")
    p = done(BandProfile("X"), info=info, spotify_artist=artist("X", "x"))
    assert p.confidence == NAME_ONLY


def test_pins_override_everything():
    cache.pin("Shape", "chosen", "Shape")
    assert done(BandProfile("Shape")).confidence == CORROBORATED
    cache.pin("Shape", None)
    assert done(BandProfile("Shape"), spotify_artist=artist("Shape", "s")).confidence == NONE


class FakeSession:
    def __init__(self, artists):
        self.artists = artists
        self.calls = []

    def search(self, q, kind, limit=10):
        self.calls.append((q, kind, limit))
        return self.artists if kind == "artist" else []

    def request(self, method, path, **kw):
        return artist("Pinned", path.rsplit("/", 1)[-1])


def test_several_exact_name_matches_are_offered_not_chosen():
    sess = FakeSession([artist("Shape", "a"), artist("Shape", "b"), artist("Shapes", "c")])
    p = BandProfile("Shape", status={"spotify": "done"})
    SpotifyEnricher(lambda: sess).enrich(p)
    assess(p)
    assert p.spotify_artist is None
    assert [a["id"] for a in p.spotify_candidates] == ["a", "b"]
    assert p.confidence == UNCERTAIN


def test_punctuation_differences_are_the_same_name():
    """Measured: The List's "Holybasil909" is Spotify's "HOLYBASIL_909"."""
    sess = FakeSession([artist("HOLYBASIL_909", "h"), artist("HOLY808", "x")])
    p = BandProfile("Holybasil909", status={"spotify": "done"})
    SpotifyEnricher(lambda: sess).enrich(p)
    assess(p)
    assert p.spotify_artist["id"] == "h" and p.confidence == NAME_ONLY


def test_loose_hits_for_a_band_spotify_lacks_are_not_candidates():
    """Measured: "Girl Chow" (not on Spotify) returns Girlschool, a Maori
    girls' choir... Offering those as "which one?" is noise; it is ✗."""
    sess = FakeSession([artist("Girlschool", "g"), artist("Chow Chow", "c")])
    p = BandProfile("Girl Chow", status={"spotify": "done"})
    SpotifyEnricher(lambda: sess).enrich(p)
    assess(p)
    assert p.spotify_candidates == [] and p.confidence == NONE


def test_a_musicbrainz_link_picks_among_same_named_artists():
    """Measured: two Spotify artists are called "Chuck Johnson"; MusicBrainz
    links 5lRVe4YjX70hurTNtjek0f, the guitarist on the Stork Club bill."""
    sess = FakeSession([artist("Chuck Johnson", "other"), artist("Chuck Johnson", "5lRV")])
    p = BandProfile("Chuck Johnson", status={"spotify": "done", "lookup": "done"})
    p.info = lookup.ArtistInfo(name="Chuck Johnson",
                               links={"spotify": "https://open.spotify.com/artist/5lRV"})
    SpotifyEnricher(lambda: sess).enrich(p)
    assess(p)
    assert p.spotify_artist["id"] == "5lRV" and p.confidence == CORROBORATED


def test_spotify_is_redone_when_the_lookup_lands_later_with_a_link():
    sess = FakeSession([artist("Chuck Johnson", "other"), artist("Chuck Johnson", "5lRV")])
    spot = SpotifyEnricher(lambda: sess)
    info = lookup.ArtistInfo(name="Chuck Johnson",
                             links={"spotify": "https://open.spotify.com/artist/5lRV"})
    gate = threading.Event()

    def identify(name):
        gate.wait(2)            # Spotify finishes first, and finds two
        return lookup.Result(artist=info)

    book = BandBook([LookupEnricher(identify), spot])
    p = book.get("Chuck Johnson")
    deadline = time.time() + 3
    while p.status.get("spotify") != "done" and time.time() < deadline:
        time.sleep(0.01)
    assert p.confidence == UNCERTAIN
    gate.set()
    while (p.busy() or p.confidence != CORROBORATED) and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert p.spotify_artist["id"] == "5lRV" and p.confidence == CORROBORATED


def test_search_asks_for_a_page():
    sess = FakeSession([])
    SpotifyEnricher(lambda: sess).enrich(BandProfile("x"))
    assert sess.calls[0][2] >= 5


def test_a_pinned_artist_is_fetched_by_id_not_searched():
    cache.pin("Shape", "chosen")
    sess = FakeSession([artist("Shape", "wrong")])
    p = BandProfile("Shape")
    SpotifyEnricher(lambda: sess).enrich(p)
    assert p.spotify_artist["id"] == "chosen"
    assert not [c for c in sess.calls if c[1] == "artist"]


def test_lookups_never_overlap():
    """lookup.py's MusicBrainz throttle and cache are not thread-safe, so a
    lineup prefetch must run its identify calls one at a time."""
    active, peak, lock = [0], [0], threading.Lock()

    def identify(name):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.03)
        with lock:
            active[0] -= 1
        return lookup.Result()

    updated = []
    book = BandBook([LookupEnricher(identify)], on_update=lambda p: updated.append(p.band))
    for b in ["A", "B", "C", "D"]:
        book.get(b, urgent=False)
    deadline = time.time() + 3
    while len(updated) < 4 and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert sorted(updated) == ["A", "B", "C", "D"]
    assert peak[0] == 1


def test_the_band_on_screen_jumps_the_lookup_queue():
    order = []
    gate = threading.Event()

    def identify(name):
        if name == "first":
            gate.wait(1)
        order.append(name)
        return lookup.Result()

    book = BandBook([LookupEnricher(identify)])
    book.get("first", urgent=False)          # occupies the lane
    time.sleep(0.05)
    for b in ["p1", "p2", "p3"]:
        book.get(b, urgent=False)
    book.get("onscreen", urgent=True)
    gate.set()
    deadline = time.time() + 3
    while len(order) < 5 and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert order[:2] == ["first", "onscreen"]


def test_a_failing_enricher_is_reported_not_raised():
    def identify(name):
        raise lookup.LookupFailed("music databases unreachable")

    seen = []
    book = BandBook([LookupEnricher(identify)], on_update=seen.append)
    p = book.get("X")
    deadline = time.time() + 2
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert p.status["lookup"].startswith("error: music databases unreachable")
    assert not p.busy()


def test_region_matches_bandcamp_and_musicbrainz_spellings():
    assert bands.local(lookup.ArtistInfo(name="x", origin="Oakland, California"))
    assert bands.local(lookup.ArtistInfo(name="x", origin="Berkeley"))
    assert not bands.local(lookup.ArtistInfo(name="x", origin="Manchester, England"))


# ---- billings that aren't band names --------------------------------------------

import pytest  # noqa: E402

from twiddle.scenedata.bands import BandcampEnricher, name_candidates  # noqa: E402


@pytest.mark.parametrize("billed, first", [
    ("Mindi Abair Christmas Show", "Mindi Abair"),
    ("A Tribute To Grover Washington Jr.", "Grover Washington Jr."),
    ("Christian Sands Trio", "Christian Sands"),
    ("Cherronda G I'm Every Woman Show", "Cherronda G I'm Every Woman"),
])
def test_a_show_billing_offers_trimmed_names(billed, first):
    got = name_candidates(billed)
    assert got[0] == first and len(got) <= bands.MAX_CANDIDATES
    assert not any(w in bands.SHOW_WORDS for c in got for w in c.lower().split())


@pytest.mark.parametrize("billed", ["Spyro Gyra", "Direct From Sweden", "The Band", "Show"])
def test_a_plain_band_name_offers_nothing(billed):
    assert name_candidates(billed) == []


def _identify(known):
    calls = []

    def identify(name):
        calls.append(name)
        if name in known:
            return lookup.Result(artist=lookup.ArtistInfo(name=name, matched_by="unique name match"))
        if name == "Cherronda":
            return lookup.Result(candidates=[{"name": "Cherronda"}, {"name": "Cherronda"}])
        return lookup.Result()
    return identify, calls


def test_the_first_trim_the_catalog_confirms_becomes_the_alias():
    identify, calls = _identify({"Mindi Abair"})
    p = BandProfile("Mindi Abair Christmas Show")
    LookupEnricher(identify).enrich(p)
    assert p.alias == "Mindi Abair" and p.info.name == "Mindi Abair"
    assert calls == ["Mindi Abair Christmas Show", "Mindi Abair"]
    # ...and is remembered, so the next session doesn't search the trims again.
    identify2, calls2 = _identify({"Mindi Abair"})
    LookupEnricher(identify2).enrich(BandProfile("Mindi Abair Christmas Show"))
    assert calls2 == ["Mindi Abair Christmas Show", "Mindi Abair"]


def test_a_trim_that_is_merely_ambiguous_is_not_used():
    identify, calls = _identify(set())
    p = BandProfile("Cherronda Show Tonight")
    LookupEnricher(identify).enrich(p)
    assert p.alias is None and p.info is None
    identify2, calls2 = _identify(set())
    LookupEnricher(identify2).enrich(BandProfile("Cherronda Show Tonight"))
    assert calls2 == ["Cherronda Show Tonight"]         # "none worked" is cached too


def test_a_billing_the_catalog_knows_is_never_trimmed():
    identify, calls = _identify({"Spanish Harlem Orchestra Band"})
    p = BandProfile("Spanish Harlem Orchestra Band")
    LookupEnricher(identify).enrich(p)
    assert p.alias is None and calls == ["Spanish Harlem Orchestra Band"]


def test_spotify_and_bandcamp_search_again_under_the_alias():
    sess = FakeSession([artist("Mindi Abair", "m")])
    spot = SpotifyEnricher(lambda: sess)
    seen = []
    bc = BandcampEnricher(search=lambda n: seen.append(n) or [], tracks=lambda u: [])
    gate = threading.Event()
    identify, _ = _identify({"Mindi Abair"})

    def slow(name):
        gate.wait(2)            # Spotify and Bandcamp finish first, finding nothing
        return identify(name)

    book = BandBook([LookupEnricher(slow), spot, bc])
    p = book.get("Mindi Abair Christmas Show")
    deadline = time.time() + 3
    while (p.status.get("spotify") != "done" or p.status.get("bandcamp") != "done") \
            and time.time() < deadline:
        time.sleep(0.01)
    assert p.spotify_artist is None
    gate.set()
    while (p.busy() or not p.spotify_artist) and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert p.spotify_artist["id"] == "m"
    artist_searches = [c[0] for c in sess.calls if c[1] == "artist"]
    assert artist_searches[:2] == ["Mindi Abair Christmas Show", "Mindi Abair"]
    assert seen[0] == "Mindi Abair Christmas Show" and seen[1] == "Mindi Abair"
    assert "searched as “Mindi Abair”" in p.why


def test_not_signed_in_to_spotify_says_so_not_no_such_artist():
    def signed_out():
        raise spotify_ops.PlaybackError("no Spotify tokens at x") from spotify.AuthError("x")

    seen = []
    book = BandBook([SpotifyEnricher(signed_out)], on_update=seen.append)
    p = book.get("Street Eaters")
    deadline = time.time() + 2
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert p.confidence == NONE
    assert "spotify auth" in p.why and "Bandcamp" in p.why
    assert "no Spotify artist" not in p.why


def test_a_failed_spotify_search_is_not_reported_as_no_such_artist():
    p = done(BandProfile("Shape"))
    p.status["spotify"] = "error: 429: API rate limit exceeded"
    assess(p)
    assert p.confidence == NONE and p.why == "Spotify: 429: API rate limit exceeded"


# ---- song lists for the band on screen --------------------------------------------------


def test_track_enricher_fetches_only_the_lists_that_are_missing(monkeypatch):
    from twiddle import discover_cli
    from twiddle.scene.book import TrackEnricher
    asked = []
    monkeypatch.setattr(discover_cli, "_artist_tracks",
                        lambda sess, a: asked.append(("spotify", a["id"])) or [{"uri": "u"}])
    spot = SpotifyEnricher(lambda: object(), tracks=False)
    enr = TrackEnricher(spot, bc_tracks=lambda url: asked.append(("bandcamp", url)) or [{"title": "t"}])
    p = BandProfile("X")
    p.spotify_artist = {"id": "s1"}
    p.bandcamp = {"item_url_root": "https://x.bandcamp.com"}
    assert enr.wants_rerun(p)
    enr.enrich(p)
    assert p.tracks == [{"uri": "u"}] and p.bc_tracks == [{"title": "t"}]
    assert not enr.wants_rerun(p)
    enr.enrich(p)                                        # nothing missing: nothing asked
    assert len(asked) == 2


def test_track_enricher_does_not_retry_an_artist_with_no_tracks_forever(monkeypatch):
    from twiddle import discover_cli
    from twiddle.scene.book import TrackEnricher
    monkeypatch.setattr(discover_cli, "_artist_tracks", lambda sess, a: [])
    enr = TrackEnricher(SpotifyEnricher(lambda: object(), tracks=False))
    p = BandProfile("X")
    p.spotify_artist = {"id": "s1"}
    enr.enrich(p)
    assert not enr.wants_rerun(p)                        # tried this identity already
    p.spotify_artist = {"id": "s2"}
    assert enr.wants_rerun(p)                            # a different one is worth a look


def test_track_enricher_waits_out_an_identity_that_lands_later(monkeypatch):
    """Woken together with Spotify identity, it may run first and find nothing;
    it runs again once the artist is known."""
    from twiddle import discover_cli
    from twiddle.scene.book import TrackEnricher
    monkeypatch.setattr(discover_cli, "_artist_tracks", lambda sess, a: [{"uri": "u"}])
    gate = threading.Event()

    class SlowIdentity:
        name, serial = "spotify", False

        def session(self):
            return object()

        def enrich(self, p):
            gate.wait(2)
            p.spotify_artist = {"id": "s1"}
    ident = SlowIdentity()
    book = BandBook([ident, TrackEnricher(ident)])
    p = BandProfile("X")
    p.status = {"spotify": "idle", "tracks": "idle"}
    book.seed([p])
    book.get("X", urgent=True)
    time.sleep(0.2)
    assert p.status["tracks"] == "done" and not p.tracks    # ran first, found no identity
    gate.set()
    deadline = time.time() + 3
    while (not p.tracks or p.busy()) and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert p.tracks == [{"uri": "u"}]


def test_track_enricher_checks_again_when_identity_lands_while_it_runs(monkeypatch):
    """Codex: Spotify's tracks begin before Bandcamp identifies the band; the
    Bandcamp completion sees `tracks` running and schedules nothing."""
    from twiddle import discover_cli
    from twiddle.scene.book import TrackEnricher
    monkeypatch.setattr(discover_cli, "_artist_tracks", lambda sess, a: [{"uri": "u"}])
    started, release = threading.Event(), threading.Event()

    def bc_tracks(url):
        return [{"title": "t"}]

    class SlowSpotifySide:
        name, serial = "spotify", False

        def session(self):
            started.set()
            release.wait(2)          # the Spotify track request is in flight ...
            return object()

        def enrich(self, p):
            p.spotify_artist = {"id": "s1"}

    class Bandcamp:
        name, serial = "bandcamp", False

        def enrich(self, p):
            started.wait(2)          # ... when Bandcamp identifies the band
            p.bandcamp = {"item_url_root": "https://x.bandcamp.com"}
            release.set()
    spot = SlowSpotifySide()
    book = BandBook([spot, Bandcamp(), TrackEnricher(spot, bc_tracks=bc_tracks)])
    p = BandProfile("X")
    p.status = {"spotify": "done", "bandcamp": "idle", "tracks": "idle"}
    p.spotify_artist = {"id": "s1"}
    book.seed([p])
    book.get("X", urgent=True)
    deadline = time.time() + 3
    while (not (p.tracks and p.bc_tracks) or p.busy()) and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert p.tracks and p.bc_tracks == [{"title": "t"}]


def test_a_record_that_arrives_mid_lookup_is_applied_when_the_lookup_ends():
    """Codex: the last publish must not be lost because a worker was running."""
    gate = threading.Event()

    class Slow:
        name, serial = "spotify", False

        def enrich(self, p):
            gate.wait(2)
            p.spotify_artist = {"id": "local"}
    updates = []
    book = BandBook([Slow()], on_update=updates.append)
    p = BandProfile("X")
    p.status = {"spotify": "idle"}
    book.seed([p])
    book.get("X", urgent=True)                             # spotify now running
    published = BandProfile("X")
    published.status = {"spotify": "done"}
    published.spotify_artist = {"id": "from-dataset"}
    book.seed([published])                                 # arrives mid-lookup: deferred
    assert book.profiles["x"] is p
    gate.set()
    deadline = time.time() + 3
    while book.profiles["x"] is p and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert book.profiles["x"].spotify_artist == {"id": "from-dataset"}
    assert updates and updates[-1] is book.profiles["x"]


def test_changing_a_pin_discards_a_record_that_was_waiting_for_the_old_lookup():
    """Codex: a checkpoint deferred before the pin changed must not come back after it."""
    gate = threading.Event()

    class Slow:
        name, serial = "spotify", False

        def enrich(self, p):
            gate.wait(2)
            p.spotify_artist = {"id": "pinned"}
    book = BandBook([Slow()])
    p = BandProfile("X")
    p.status = {"spotify": "idle"}
    book.seed([p])
    book.get("X", urgent=True)                             # running
    stale = BandProfile("X")
    stale.status = {"spotify": "done"}
    stale.spotify_artist = {"id": "from-dataset"}
    book.seed([stale])                                     # deferred
    assert "x" in book._deferred
    book.refresh("X")
    assert "x" not in book._deferred
    gate.set()
    time.sleep(0.3)
    book.close()
    assert (book.profiles["x"].spotify_artist or {}).get("id") != "from-dataset"


def test_a_deferred_record_that_corrects_the_artist_wakes_its_song_lists(monkeypatch):
    """Codex: the corrected profile has no tracks; something must fetch them."""
    from twiddle import discover_cli
    from twiddle.scene.book import TrackEnricher
    gate, asked = threading.Event(), []

    def artist_tracks(sess, artist):
        asked.append(artist["id"])
        if artist["id"] == "old":
            gate.wait(2)                                    # this request is in flight ...
        return [{"uri": artist["id"]}]
    monkeypatch.setattr(discover_cli, "_artist_tracks", artist_tracks)

    class Ident:
        name, serial = "spotify", False

        def session(self):
            return object()

        def enrich(self, p):
            pass
    ident = Ident()
    book = BandBook([ident, TrackEnricher(ident)])
    p = BandProfile("X")
    p.status = {"spotify": "done", "tracks": "idle"}
    p.spotify_artist = {"id": "old"}
    book.seed([p])
    book.get("X", urgent=True)
    deadline = time.time() + 2
    while "old" not in asked and time.time() < deadline:
        time.sleep(0.01)
    corrected = BandProfile("X")
    corrected.status = {"spotify": "done", "tracks": "idle"}
    corrected.spotify_artist = {"id": "new"}
    book.seed([corrected])                                  # ... when the correction arrives
    gate.set()
    deadline = time.time() + 3
    while book.profiles["x"].tracks != [{"uri": "new"}] and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert book.profiles["x"].tracks == [{"uri": "new"}] and "new" in asked


def test_song_lists_fetched_for_an_artist_that_changed_meanwhile_are_dropped(monkeypatch):
    """Codex: the old artist's slow reply must not overwrite the corrected artist's songs."""
    from twiddle import discover_cli
    from twiddle.scene.book import TrackEnricher
    gate = threading.Event()

    def artist_tracks(sess, artist):
        if artist["id"] == "old":
            gate.wait(2)
        return [{"uri": artist["id"]}]
    monkeypatch.setattr(discover_cli, "_artist_tracks", artist_tracks)

    class Ident:
        name, serial = "spotify", False

        def session(self):
            return object()
    enr = TrackEnricher(Ident())
    p = BandProfile("X")
    p.spotify_artist = {"id": "old"}
    t = threading.Thread(target=enr.enrich, args=(p,))
    t.start()
    time.sleep(0.1)
    p.spotify_artist, p.tracks = {"id": "new"}, [{"uri": "new"}]      # identity corrected meanwhile
    gate.set()
    t.join()
    assert p.tracks == [{"uri": "new"}]


def test_a_failed_song_list_fetch_is_retried_when_a_new_identity_arrives(monkeypatch):
    """Codex: Spotify's auth failure must not suppress Bandcamp tracks whose identity
    lands later -- and an unchanged identity is not retried forever."""
    from twiddle import discover_cli
    from twiddle.scene.book import TrackEnricher
    spotify_calls = []

    def artist_tracks(sess, artist):
        spotify_calls.append(artist["id"])
        raise ConnectionError("auth expired")
    monkeypatch.setattr(discover_cli, "_artist_tracks", artist_tracks)
    gate = threading.Event()

    class Ident:
        name, serial = "spotify", False

        def session(self):
            return object()

    class SlowBandcamp:
        name, serial = "bandcamp", False

        def enrich(self, p):
            gate.wait(2)                                  # tracks runs (and fails) first
            p.bandcamp = {"item_url_root": "https://x.bandcamp.com"}
    ident = Ident()
    book = BandBook([SlowBandcamp(), TrackEnricher(ident, bc_tracks=lambda u: [{"title": "t"}])])
    p = BandProfile("X")
    p.status = {"bandcamp": "idle", "tracks": "idle"}
    p.spotify_artist = {"id": "s1"}
    book.seed([p])
    book.get("X", urgent=True)
    deadline = time.time() + 2
    while not p.status.get("tracks", "").startswith("error") and time.time() < deadline:
        time.sleep(0.01)
    assert p.status["tracks"].startswith("error") and p.bc_tracks == []
    gate.set()
    deadline = time.time() + 3
    while not p.bc_tracks and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    book.close()
    assert p.bc_tracks == [{"title": "t"}]                # Bandcamp songs arrived despite the failure
    assert len(spotify_calls) <= 2                         # one rerun for the new identity, no loop
