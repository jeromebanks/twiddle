"""`tune`, `np` and `info`, with every station, database and speaker faked."""
import pytest

from twiddle import cli, discover_cli, radio_cli, stations


def parse(argv):
    return cli.build_parser().parse_args(argv)


class FakeStation:
    def __init__(self, np):
        self.np = np

    def now_playing(self):
        return self.np


def _station(monkeypatch, key="kalx", **fields):
    np = stations.NowPlaying(key.upper(), **fields)
    monkeypatch.setitem(stations.STATIONS, key, FakeStation(np))
    return np


def test_bare_np_asks_about_the_remembered_source(monkeypatch, capsys):
    _station(monkeypatch, "kexp", artist="Björk", song="Immature")
    stations.remember("kexp")
    assert parse(["np"]).func(parse(["np"])) == 0
    assert "Björk - Immature" in capsys.readouterr().out


def test_np_with_nothing_remembered_says_how_to_fix_it(capsys):
    args = parse(["np"])
    assert args.func(args) == 1
    assert "np kexp" in capsys.readouterr().err


def test_np_info_passes_everything_the_station_knew(monkeypatch):
    _station(monkeypatch, artist="Spoon", song="Inside Out", album="They Want My Soul",
             mb_artist_id="mb1")
    seen = {}

    def identify(artist, album=None, song=None, **kw):
        seen.update(artist=artist, album=album, song=song, **kw)
        return radio_cli.lookup.Result()
    monkeypatch.setattr(radio_cli.lookup, "identify", identify)

    args = parse(["np", "kalx", "-i"])
    assert args.func(args) == 0
    assert seen == {"artist": "Spoon", "album": "They Want My Soul", "song": "Inside Out",
                    "mb_artist_id": "mb1", "mb_release_group_id": None}


def test_np_discover_hands_the_artist_to_discover(monkeypatch):
    _station(monkeypatch, artist="Spoon", song="Inside Out")
    got = {}
    # patched before `parse`, which is when the parser binds discover's func
    monkeypatch.setattr(discover_cli, "cmd_discover", lambda a: got.update(vars(a)) or 0)
    args = parse(["np", "kalx", "-d", "--dry-run"])
    assert args.func(args) == 0
    assert got["artist"] == ["Spoon"]
    assert got["dry_run"] is True
    assert got["room"] == "roam"


def test_np_discover_hands_over_the_artist_without_a_with_credit(monkeypatch):
    _station(monkeypatch, artist='Butch Paulson with "The Motations"', song="Man From Mars")
    got = {}
    monkeypatch.setattr(discover_cli, "cmd_discover", lambda a: got.update(vars(a)) or 0)
    args = parse(["np", "kalx", "-d", "--dry-run"])
    assert args.func(args) == 0
    assert got["artist"] == ["Butch Paulson"]


def test_np_info_looks_up_without_a_with_credit(monkeypatch):
    _station(monkeypatch, artist='Butch Paulson with "The Motations"', song="Man From Mars")
    seen = []
    monkeypatch.setattr(radio_cli.lookup, "identify",
                        lambda artist, *a, **kw: seen.append(artist) or radio_cli.lookup.Result())
    args = parse(["np", "kalx", "-i"])
    assert args.func(args) == 0
    assert seen == ["Butch Paulson"]


def test_np_discover_without_an_artist_fails_before_discover(monkeypatch):
    _station(monkeypatch, raw_title="Your DJ speaks over something")
    args = parse(["np", "kalx", "-d"])
    assert args.func(args) == 1


def test_tune_dry_run_does_not_change_the_remembered_source(monkeypatch):
    monkeypatch.setattr(radio_cli, "cmd_play_radio", lambda args: 0)
    args = parse(["tune", "kexp", "--dry-run"])
    assert args.func(args) == 0
    assert stations.last_source() is None
    args = parse(["tune", "kexp"])
    assert args.func(args) == 0
    assert args.url == stations.STATIONS["kexp"].url
    assert stations.last_source() == "kexp"


def test_tune_rejects_an_unknown_station():
    with pytest.raises(SystemExit):
        parse(["tune", "kfoo"])
