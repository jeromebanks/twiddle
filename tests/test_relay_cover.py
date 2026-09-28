"""The relay's cover art: librespot hook -> state file -> /cover.jpg, plus its log."""
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from twiddle import relay, relay_cli, relay_event, spotify_ops

# Taken at import, before conftest's autouse fixture swaps in a fake.
REAL_PROBE = spotify_ops.relay_cover_url

TRACK_ENV = {"PLAYER_EVENT": "track_changed", "NAME": "So What",
             "ARTISTS": "Miles Davis\nJohn Coltrane", "ALBUM": "Kind Of Blue",
             "URI": "spotify:track:x",
             "COVERS": "https://i.scdn.co/image/small\nhttps://i.scdn.co/image/big"}


def test_track_changed_keeps_names_and_every_cover():
    t = relay_event.track_from_env(TRACK_ENV)
    assert t["name"] == "So What" and t["album"] == "Kind Of Blue"
    assert t["artists"] == ["Miles Davis", "John Coltrane"]
    assert t["covers"] == ["https://i.scdn.co/image/small", "https://i.scdn.co/image/big"]


def test_other_events_are_ignored():
    assert relay_event.track_from_env({"PLAYER_EVENT": "playing"}) is None


def test_the_installed_hook_really_runs_and_writes_state(tmp_path):
    # librespot runs the script with no arguments and the event in the
    # environment; do exactly that.
    script, state = relay.install_onevent_hook(str(tmp_path))
    subprocess.run([script], env=os.environ | TRACK_ENV, check=True, timeout=30)
    assert json.loads(state.read_text())["name"] == "So What"


def test_spotify_source_passes_the_hook_only_when_given():
    assert "--onevent" not in relay.spotify_source("n", "/c")
    argv = relay.spotify_source("n", "/c", onevent="/c/onevent.sh")
    assert argv[argv.index("--onevent") + 1] == "/c/onevent.sh"


def _state(tmp_path, covers):
    path = tmp_path / relay.COVER_STATE
    path.write_text(json.dumps({"name": "So What", "covers": covers}))
    return path


def test_covers_serves_the_largest_and_fetches_once_per_track(tmp_path):
    fetched = []
    images = {"a": b"x" * 10, "b": b"y" * 50}

    def fetch(url):
        fetched.append(url)
        return images[url]
    covers = relay.Covers(_state(tmp_path, ["a", "b"]), fetch=fetch)
    assert covers.current()[1] == images["b"]
    assert covers.current()[1] == images["b"]
    assert fetched == ["a", "b"]            # second request came from memory


def test_covers_follows_a_track_change(tmp_path):
    covers = relay.Covers(_state(tmp_path, ["a"]), fetch=lambda u: u.encode())
    assert covers.current()[1] == b"a"
    _state(tmp_path, ["c"])
    assert covers.current()[1] == b"c"


def test_no_state_or_dead_cover_is_no_image_not_an_error(tmp_path):
    assert relay.Covers(tmp_path / "missing.json").current() == ({}, None)

    def boom(url):
        raise OSError("down")
    assert relay.Covers(_state(tmp_path, ["a"]), fetch=boom).current()[1] is None


@pytest.fixture
def server(tmp_path):
    seen = []
    covers = relay.Covers(_state(tmp_path, ["a"]), fetch=lambda u: b"\xff\xd8jpeg")
    handler = type("_H", (relay._StreamHandler,), {
        "fanout": relay.Fanout(), "covers": covers,
        "on_cover": staticmethod(lambda **kw: seen.append(kw))})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen, tmp_path
    srv.shutdown()


def test_cover_endpoint_serves_the_image_and_logs_who_asked(server):
    base, seen, _ = server
    req = urllib.request.Request(base + relay.COVER_PATH, headers={"User-Agent": "Sonos/1"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.headers["Content-Type"] == "image/jpeg"
        assert "no-cache" in resp.headers["Cache-Control"]
        assert resp.read() == b"\xff\xd8jpeg"
    assert seen == [{"client": "127.0.0.1", "agent": "Sonos/1",
                     "track": "So What", "served": True}]


def test_cover_endpoint_404s_when_nothing_is_playing(server):
    base, seen, tmp_path = server
    (tmp_path / relay.COVER_STATE).unlink()
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(base + relay.COVER_PATH, timeout=5)
    assert err.value.code == 404
    assert seen[-1]["served"] is False


def test_record_log_follows_the_file_when_it_is_replaced(tmp_path):
    # What a `git checkout` of a tracked log does: a new file at the same path.
    path = tmp_path / "relay.jsonl"
    log = relay_cli.RecordLog(path)
    log.write({"n": 1})
    path.rename(tmp_path / "old.jsonl")
    path.write_text("")
    log.write({"n": 2})
    log.close()
    assert [json.loads(line)["n"] for line in path.read_text().splitlines()] == [2]


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}{relay.STREAM_PATH}"


def test_probe_offers_the_cover_when_the_relay_serves_one(server):
    base, _, tmp_path = server
    stream = base + relay.STREAM_PATH
    assert REAL_PROBE(stream) == base + relay.COVER_PATH
    (tmp_path / relay.COVER_STATE).unlink()          # new relay, no track yet: 404
    assert REAL_PROBE(stream) == base + relay.COVER_PATH


def test_probe_refuses_an_old_relay_and_never_becomes_its_listener():
    # A relay from before /cover.jpg: every path is the endless stream.
    fanout = relay.Fanout()
    old = type("_Old", (relay._StreamHandler,), {
        "fanout": fanout, "_is_cover": lambda self: False})
    srv, stream = _serve(old)
    try:
        assert REAL_PROBE(stream) is None
        assert fanout.total_clients == 0
    finally:
        srv.shutdown()


def test_probe_with_nothing_listening_is_no_cover():
    srv, stream = _serve(relay._StreamHandler)
    srv.shutdown()
    srv.server_close()
    assert REAL_PROBE(stream, timeout=0.5) is None
