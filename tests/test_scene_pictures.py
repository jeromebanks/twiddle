"""Which photo a band gets, and from where."""
from twiddle import lookup
from twiddle.dial import art
from twiddle.scene import pictures
from twiddle.scenespec.band import CORROBORATED, NAME_ONLY, UNCERTAIN, BandProfile

SPOTIFY = {"id": "x", "images": [{"url": "https://i.scdn.co/big", "height": 640},
                                 {"url": "https://i.scdn.co/small", "height": 64}]}
BC = {"item_url_root": "https://sleepbomb.bandcamp.com",
      "img": "https://f4.bcbits.com/img/0042827271_23.jpg"}


def test_bandcamp_photo_comes_first_at_full_size(monkeypatch):
    monkeypatch.setattr(art, "wikipedia_image", lambda url: "https://wiki/x.jpg")
    p = BandProfile("Sleepbomb", bandcamp=BC, spotify_artist=SPOTIFY, confidence=CORROBORATED)
    assert pictures.band_photo(p) == ("https://f4.bcbits.com/img/0042827271_16.jpg", "Bandcamp")


def test_spotify_image_for_confirmed_and_for_name_only_captioned_apart(monkeypatch):
    monkeypatch.setattr(art, "wikipedia_image", lambda url: None)
    p = BandProfile("Shape", spotify_artist=SPOTIFY, confidence=CORROBORATED)
    assert pictures.band_photo(p) == ("https://i.scdn.co/big", "Spotify")
    p.confidence = NAME_ONLY
    assert pictures.band_photo(p) == ("https://i.scdn.co/big", "Spotify (name match)")


def test_several_same_named_spotify_artists_show_no_spotify_face(monkeypatch):
    monkeypatch.setattr(art, "wikipedia_image", lambda url: None)
    p = BandProfile("Eraser", spotify_artist=SPOTIFY, confidence=UNCERTAIN)
    assert pictures.band_photo(p) == (None, "")


def test_wikipedia_when_nothing_else(monkeypatch):
    monkeypatch.setattr(art, "wikipedia_image", lambda url: "https://upload.wiki/x.jpg" if url
                        else None)
    info = lookup.ArtistInfo(name="X", links={"wikipedia": "https://en.wikipedia.org/wiki/X"})
    p = BandProfile("X", info=info, confidence=UNCERTAIN)
    assert pictures.band_photo(p) == ("https://upload.wiki/x.jpg", "Wikipedia")


def test_signature_changes_when_the_identity_or_bandcamp_page_does():
    p = BandProfile("Shape", confidence=NAME_ONLY, spotify_artist={"id": "a"})
    before = pictures.signature(p)
    p.confidence = CORROBORATED
    assert pictures.signature(p) != before
    before = pictures.signature(p)
    p.bandcamp = BC
    assert pictures.signature(p) != before
