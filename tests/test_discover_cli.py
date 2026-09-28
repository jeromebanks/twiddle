"""`discover_cli.cmd_discover`'s REPL, exercised without touching hardware.

Every seam that would otherwise reach something real is stubbed at
discover_cli's own module-level names, never at the modules underneath:

- `_session` would load your real `tokens.json` and, if stale, trigger a
  real refresh POST that rewrites the token file mid test run.
- `_target` does real SSDP/LAN discovery.
- `_relay_device` (patched in `spotify_cli`, where it lives) can start a
  real, detached relay process.
- `_relay_url` reads a real PID file (`supervisor.status()`) and opens a
  real socket (`play.local_ip_for()`).

So nothing here ever reaches `household.py`, `play.py`, `supervisor.py`, or
the real Spotify API -- no test in this file should ever write a line to
`logs/interventions.jsonl` or touch a real speaker.
"""
import builtins

import requests

from twiddle import discover_cli, spotify_cli


class FakeSession:
    """Records every call instead of hitting the Spotify Web API."""

    def __init__(self, artists=None, tracks=None):
        self.artists = artists or {}  # search term -> list[artist dict]
        self.tracks = tracks or {}    # search term -> list[track dict]
        self.calls = []
        self.playing = False

    def search(self, query, kind, limit=10):
        self.calls.append(("search", query, kind, limit))
        return (self.artists if kind == "artist" else self.tracks).get(query, [])

    def play(self, uri="", device_id="", position_ms=0, *, uris=None, offset=0):
        self.calls.append(("play", uri, uris, offset, device_id))
        self.playing = True

    def pause(self):
        self.calls.append(("pause",))
        self.playing = False

    def next_track(self):
        self.calls.append(("next",))

    def previous_track(self):
        self.calls.append(("prev",))

    def queue(self, uri, device_id=""):
        self.calls.append(("queue", uri))

    def current(self):
        return {"is_playing": self.playing, "item": {}, "progress_ms": 0,
                "device": {"name": "relay", "id": "dev1"}}

    def devices(self):
        return [{"id": "dev1", "name": "relay"}]


class FakeGroup:
    """Stands in for `household.Group`: records writes, never sends SOAP."""

    def __init__(self, ip="192.168.1.4", name="Sonos Roam", uri=""):
        self.ip = ip
        self.name = name
        self._uri = uri
        self.play_radio_calls = []
        self.art = []

    def now_playing(self):
        return {"uri": self._uri}

    def play_radio(self, url, title, art=None):
        self.play_radio_calls.append((url, title))
        self.art.append(art)
        self._uri = discover_cli.RADIO_SCHEME + url[len("http://"):]


class FakeResolution:
    def __init__(self, group):
        self.group = group


def _artist(name, aid):
    return {"type": "artist", "id": aid, "name": name}


def _track(name, artist_id, artist_name="Band", uri=None):
    slug = name.lower().replace(" ", "-")
    return {"type": "track", "id": slug, "name": name,
            "artists": [{"id": artist_id, "name": artist_name}],
            "album": {"name": "Album"},
            "uri": uri or f"spotify:track:{slug}"}


def _args(**kw):
    class Args:
        pass

    a = Args()
    a.room = "roam"
    a.anchor = None
    a.device = None
    a.device_name = "twiddle relay"
    a.cache = "/tmp/cache"
    a.volume = None
    a.no_restore = False
    a.dry_run = False
    a.artist = []
    a.json = False
    for k, v in kw.items():
        setattr(a, k, v)
    return a


RELAY_URL = "http://192.168.1.50:8899/stream.mp3"


def _wire(monkeypatch, sess, group, relay_url=RELAY_URL):
    res = FakeResolution(group)
    monkeypatch.setattr(discover_cli, "_session", lambda args: (sess, None))
    monkeypatch.setattr(discover_cli, "_target", lambda args: (res, None))
    monkeypatch.setattr(spotify_cli, "_relay_device",
                        lambda args, s: ({"id": "dev1", "name": "relay"}, None))
    monkeypatch.setattr(spotify_cli, "_relay_url", lambda group: relay_url)
    return res


def _run(monkeypatch, lines, sess, group, relay_url=RELAY_URL, **kw):
    it = iter(lines)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))
    _wire(monkeypatch, sess, group, relay_url=relay_url)
    return discover_cli.cmd_discover(_args(**kw))


# ---- search, list, play -----------------------------------------------------


def test_single_match_lists_tracks_and_plays_by_number_with_repoint(monkeypatch):
    artist = _artist("Radiohead", "aid1")
    tracks = [_track("Creep", "aid1"), _track("Karma Police", "aid1")]
    sess = FakeSession(artists={"Radiohead": [artist]},
                       tracks={'artist:"Radiohead"': tracks, "Radiohead": []})
    group = FakeGroup(uri="x-rincon-mp3radio://kspc.radioca.st/stream")

    res = _run(monkeypatch, ["Radiohead", "1", "quit"], sess, group)

    assert res == 0
    assert group.play_radio_calls == [(RELAY_URL, "Spotify (relay)")]
    # the relay's fixed cover URL rides along with the one re-point
    assert group.art == [RELAY_URL.replace("/stream.mp3", "/cover.jpg")]
    play_calls = [c for c in sess.calls if c[0] == "play"]
    assert len(play_calls) == 1
    _, _uri, uris, offset, device_id = play_calls[0]
    assert uris == [t["uri"] for t in tracks]
    assert offset == 0
    assert device_id == "dev1"


def test_second_play_does_not_repoint_again(monkeypatch):
    artist = _artist("Radiohead", "aid1")
    tracks = [_track("Creep", "aid1"), _track("Karma Police", "aid1")]
    sess = FakeSession(artists={"Radiohead": [artist]},
                       tracks={'artist:"Radiohead"': tracks, "Radiohead": []})
    group = FakeGroup(uri="x-rincon-mp3radio://kspc.radioca.st/stream")

    res = _run(monkeypatch, ["Radiohead", "1", "2", "quit"], sess, group)

    assert res == 0
    assert len(group.play_radio_calls) == 1


def test_room_already_on_relay_skips_the_repoint_write(monkeypatch):
    artist = _artist("Radiohead", "aid1")
    tracks = [_track("Creep", "aid1")]
    sess = FakeSession(artists={"Radiohead": [artist]},
                       tracks={'artist:"Radiohead"': tracks, "Radiohead": []})
    already_on_relay = discover_cli.RADIO_SCHEME + RELAY_URL[len("http://"):]
    group = FakeGroup(uri=already_on_relay)

    res = _run(monkeypatch, ["Radiohead", "1", "quit"], sess, group)

    assert res == 0
    assert group.play_radio_calls == []
    assert [c for c in sess.calls if c[0] == "play"]


def test_ambiguous_match_shows_a_picker(monkeypatch):
    a1 = _artist("Spoon", "aid1")
    a2 = _artist("Spoons", "aid2")
    tracks = [_track("Inside Out", "aid1")]
    sess = FakeSession(artists={"spoo": [a1, a2]},
                       tracks={'artist:"Spoon"': tracks, "Spoon": []})
    group = FakeGroup()

    # "spoo" -> ambiguous -> picker consumes the next input ("1") -> Spoon
    # chosen -> next prompt reads the second "1" to play track 1.
    res = _run(monkeypatch, ["spoo", "1", "1", "quit"], sess, group)

    assert res == 0
    assert [c for c in sess.calls if c[0] == "play"]


def test_unknown_artist_says_so_and_continues(monkeypatch, capsys):
    sess = FakeSession(artists={"Xyzzy": []})
    group = FakeGroup()

    res = _run(monkeypatch, ["Xyzzy", "quit"], sess, group)

    assert res == 0
    assert "no artist matching" in capsys.readouterr().out


def test_out_of_range_number_falls_through_to_search(monkeypatch, capsys):
    """A band literally named "311" must not be swallowed as a track pick."""
    sess = FakeSession(artists={"311": []})
    group = FakeGroup()

    res = _run(monkeypatch, ["311", "quit"], sess, group)

    assert res == 0
    assert ("search", "311", "artist", 5) in sess.calls
    assert "no artist matching '311'" in capsys.readouterr().out


# ---- the id-filtered, two-tier track merge ----------------------------------


def test_tracks_are_filtered_by_artist_id_and_padded_from_the_name_search(
        monkeypatch, capsys):
    artist = _artist("Momma", "aid1")
    field_hits = [_track("Medicine", "aid1"), _track("Speeding 72", "aid1")]
    name_hits = [_track("Momma Song", "other-aid", artist_name="Benson Boone"),
                 _track("Double Dare", "aid1")]
    sess = FakeSession(artists={"Momma": [artist]},
                       tracks={'artist:"Momma"': field_hits, "Momma": name_hits})
    group = FakeGroup()

    res = _run(monkeypatch, ["Momma", "quit"], sess, group)

    assert res == 0
    out = capsys.readouterr().out
    assert "Medicine" in out
    assert "Speeding 72" in out
    assert "Double Dare" in out
    assert "Momma Song" not in out, "wrong-artist track leaked into the list"


# ---- transport ---------------------------------------------------------------


def test_pause_toggles_between_pause_and_resume(monkeypatch):
    sess = FakeSession()
    sess.playing = True
    group = FakeGroup()

    res = _run(monkeypatch, ["p", "p", "quit"], sess, group)

    assert res == 0
    kinds = [c[0] for c in sess.calls]
    assert "pause" in kinds
    assert "play" in kinds


# ---- resilience --------------------------------------------------------------


def test_a_network_error_mid_loop_is_caught_and_the_shell_survives(
        monkeypatch, capsys):
    class FlakySession(FakeSession):
        def search(self, query, kind, limit=10):
            if kind == "artist" and query == "boom":
                raise requests.exceptions.RequestException("wifi blip")
            return super().search(query, kind, limit)

    sess = FlakySession(artists={"Radiohead": [_artist("Radiohead", "aid1")]},
                        tracks={'artist:"Radiohead"': [], "Radiohead": []})
    group = FakeGroup()

    res = _run(monkeypatch, ["boom", "Radiohead", "quit"], sess, group)

    assert res == 0
    assert "wifi blip" in capsys.readouterr().err


def test_eof_exits_cleanly(monkeypatch):
    sess = FakeSession()
    group = FakeGroup()
    monkeypatch.setattr(builtins, "input",
                        lambda prompt="": (_ for _ in ()).throw(EOFError))
    _wire(monkeypatch, sess, group)

    assert discover_cli.cmd_discover(_args()) == 0


def test_quit_exits_cleanly(monkeypatch):
    sess = FakeSession()
    group = FakeGroup()

    assert _run(monkeypatch, ["quit"], sess, group) == 0


# ---- --dry-run ----------------------------------------------------------------


def test_dry_run_never_starts_a_relay_or_writes(monkeypatch):
    artist = _artist("Radiohead", "aid1")
    tracks = [_track("Creep", "aid1")]
    sess = FakeSession(artists={"Radiohead": [artist]},
                       tracks={'artist:"Radiohead"': tracks, "Radiohead": []})
    group = FakeGroup(uri="x-rincon-mp3radio://kspc.radioca.st/stream")
    res_obj = FakeResolution(group)

    def boom(args, s):
        raise AssertionError("_relay_device must not run under --dry-run")

    monkeypatch.setattr(discover_cli, "_session", lambda args: (sess, None))
    monkeypatch.setattr(discover_cli, "_target", lambda args: (res_obj, None))
    monkeypatch.setattr(spotify_cli, "_relay_device", boom)
    monkeypatch.setattr(spotify_cli, "_relay_url", lambda group: RELAY_URL)

    it = iter(["Radiohead", "1", "quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))

    res = discover_cli.cmd_discover(_args(dry_run=True))

    assert res == 0
    assert group.play_radio_calls == []
    assert not [c for c in sess.calls if c[0] == "play"]


def test_dry_run_transport_and_queue_make_no_spotify_writes(monkeypatch, capsys):
    """n, b, p and q<N> used to reach Spotify for real under --dry-run."""
    artist = _artist("Radiohead", "aid1")
    tracks = [_track("Creep", "aid1")]
    sess = FakeSession(artists={"Radiohead": [artist]},
                       tracks={'artist:"Radiohead"': tracks, "Radiohead": []})
    sess.playing = True
    group = FakeGroup()
    monkeypatch.setattr(discover_cli, "_session", lambda args: (sess, None))
    monkeypatch.setattr(discover_cli, "_target", lambda args: (FakeResolution(group), None))
    it = iter(["Radiohead", "n", "b", "p", "q1", "quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))

    assert discover_cli.cmd_discover(_args(dry_run=True)) == 0
    writes = [c for c in sess.calls if c[0] in ("play", "pause", "next", "prev", "queue")]
    assert writes == []
    out = capsys.readouterr().out
    for what in ("skip forward", "skip back", "pause", "queue Creep"):
        assert f"[dry-run] would {what}" in out


# ---- info ---------------------------------------------------------------------


def test_info_looks_up_the_listed_artist_using_an_album_to_pin_identity(monkeypatch):
    artist = _artist("Spoon", "aid1")
    tracks = [_track("Inside Out", "aid1")]
    sess = FakeSession(artists={"Spoon": [artist]},
                       tracks={'artist:"Spoon"': tracks, "Spoon": []})
    asked = []
    monkeypatch.setattr(discover_cli.lookup, "identify",
                        lambda name, album=None, song=None, **kw:
                        asked.append((name, album)) or discover_cli.lookup.Result())

    assert _run(monkeypatch, ["Spoon", "i", "i1", "quit"], sess, FakeGroup()) == 0
    assert asked == [("Spoon", "Album"), ("Spoon", "Album")]


def test_a_successful_pick_makes_spotify_the_np_source(monkeypatch):
    from twiddle import stations
    artist = _artist("Radiohead", "aid1")
    sess = FakeSession(artists={"Radiohead": [artist]},
                       tracks={'artist:"Radiohead"': [_track("Creep", "aid1")],
                               "Radiohead": []})
    _run(monkeypatch, ["Radiohead", "1", "quit"], sess, FakeGroup())
    assert stations.last_source() == stations.SPOTIFY
