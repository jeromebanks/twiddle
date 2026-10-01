"""The client's book: it shows what the dataset says and applies only your own
pin; it never searches for who a band is."""
import time

from twiddle import spotify, spotify_ops
from twiddle.scene import cache
from twiddle.scene.book import BandBook, PinEnricher, regrade
from twiddle.scenespec.band import CORROBORATED, NAME_ONLY, NONE, UNLOOKED, BandProfile


class Session:
    def __init__(self):
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        return {"id": path.rsplit("/", 1)[-1], "name": "Chosen"}

    def search(self, *a, **k):
        self.calls.append(("search", a))
        return []


def test_a_pin_overrides_the_datasets_grade_and_no_pin_leaves_it_alone():
    p = BandProfile("Shape", status={"spotify": "done"}, confidence=NAME_ONLY, why="from the dataset")
    regrade(p)
    assert (p.confidence, p.why) == (NAME_ONLY, "from the dataset")
    cache.pin("Shape", "chosen", "Shape")
    regrade(p)
    assert p.confidence == CORROBORATED and p.why == "chosen by you"
    cache.pin("Shape", None)
    regrade(p)
    assert p.confidence == NONE


def test_a_band_the_dataset_never_asked_spotify_about_shows_as_unlooked():
    p = BandProfile("Nobody", status={"spotify": "idle"})
    regrade(p)
    assert p.confidence == UNLOOKED


def test_a_pinned_artist_is_fetched_by_id_not_searched():
    cache.pin("Shape", "chosen")
    sess = Session()
    p = BandProfile("Shape")
    PinEnricher(lambda: sess).enrich(p)
    assert p.spotify_artist["id"] == "chosen"
    assert sess.calls == [("GET", "/artists/chosen")]


def test_a_not_on_spotify_pin_clears_the_artist_and_a_band_without_a_pin_is_untouched():
    cache.pin("Shape", None)
    sess = Session()
    p = BandProfile("Shape", spotify_artist={"id": "x"}, tracks=[{"uri": "u"}])
    PinEnricher(lambda: sess).enrich(p)
    assert p.spotify_artist is None and p.tracks == [] and not sess.calls
    q = BandProfile("Other", spotify_artist={"id": "y"})
    PinEnricher(lambda: sess).enrich(q)
    assert q.spotify_artist == {"id": "y"} and not sess.calls


def test_it_wakes_only_when_the_pin_disagrees_with_the_profile():
    pe = PinEnricher(Session)
    p = BandProfile("Shape", spotify_artist={"id": "a"})
    assert not pe.wakes(p)                      # no pin
    cache.pin("Shape", "a")
    assert not pe.wakes(p)                      # the pin agrees
    cache.pin("Shape", "b")
    assert pe.wakes(p)


def test_signed_out_says_so_when_applying_a_pin():
    def signed_out():
        raise spotify_ops.PlaybackError("no Spotify tokens at x") from spotify.AuthError("x")
    cache.pin("Shape", "chosen")
    try:
        PinEnricher(signed_out).enrich(BandProfile("Shape"))
    except RuntimeError as exc:
        assert "spotify auth" in str(exc)
    else:
        raise AssertionError("expected the not-signed-in message")


def test_a_band_not_in_the_dataset_is_never_searched_for():
    sess = Session()
    seen = []
    book = BandBook([PinEnricher(lambda: sess)], on_update=seen.append)
    p = book.get("Street Eaters")
    deadline = time.time() + 2
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    book.close()
    assert not sess.calls
    assert p.confidence == UNLOOKED and "dataset" in p.why
