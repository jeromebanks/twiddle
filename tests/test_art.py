"""Album art / station logos: into the speaker's DIDL, and drawn in the terminal."""
import base64
import io
import xml.etree.ElementTree as ET

from PIL import Image

from twiddle import cli, play, spotify, stations, termimage

UPNP = "{urn:schemas-upnp-org:metadata-1-0/upnp/}"


def test_didl_carries_the_art_escaped_so_it_still_parses():
    art = "https://images.example/logo.png?auto=format,compress&w=2"
    root = ET.fromstring(play.radio_didl("KDVS", art))
    assert root.find(f".//{UPNP}albumArtURI").text == art


def test_didl_without_art_has_no_empty_art_element():
    assert "albumArtURI" not in play.radio_didl("Stream")


def test_tune_hands_the_station_logo_to_the_speaker(monkeypatch):
    got = {}

    def fake_play_radio(args):
        got.update(url=args.url, title=args.title, art=args.art)
        return 0
    monkeypatch.setattr("twiddle.radio_cli.cmd_play_radio", fake_play_radio)
    monkeypatch.setattr(stations, "remember", lambda key: None)
    args = cli.build_parser().parse_args(["tune", "kexp"])
    assert args.func(args) == 0
    assert got["art"] == stations.STATIONS["kexp"].logo


def test_spotify_now_playing_takes_the_largest_cover():
    state = {"item": {"name": "So What", "artists": [{"name": "Miles Davis"}],
                      "album": {"name": "Kind Of Blue", "images": [
                          {"url": "big", "width": 640}, {"url": "small", "width": 64}]}}}
    assert spotify.now_playing(state)["art"] == "big"
    assert spotify.now_playing({"item": {"name": "ep"}})["art"] == ""


def _png(w, h):
    out = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(out, format="JPEG")
    return out.getvalue()


def test_to_png_reencodes_and_shrinks():
    png = termimage.to_png(_png(1200, 600), max_px=100)
    im = Image.open(io.BytesIO(png))
    assert im.format == "PNG" and max(im.size) == 100


def test_escape_chunks_and_only_the_first_chunk_carries_the_controls():
    payload = bytes(range(256)) * 40            # > one 4096-char chunk once base64'd
    seq = termimage.escape(payload, cols=12)
    parts = seq.split("\x1b\\")[:-1]
    assert len(parts) > 1
    assert parts[0].startswith("\x1b_Ga=T,f=100,q=2,c=12,m=1;")
    assert all(p.startswith("\x1b_Gm=1;") for p in parts[1:-1])
    assert parts[-1].startswith("\x1b_Gm=0;")
    data = "".join(p.split(";", 1)[1] for p in parts)
    assert base64.standard_b64decode(data) == payload


def test_show_draws_nothing_when_not_a_tty(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    assert termimage.show("https://x/y.png", stream=buf) is False
    assert buf.getvalue() == ""
