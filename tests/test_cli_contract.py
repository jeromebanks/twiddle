"""The CLI's contract with things that already invoke it.

The diagnostic commands moved under `twiddle diag`, but the installed
launchd agent invokes the *old* spelling by absolute argv, baked into a plist
on disk that this code does not rewrite. Breaking that would silently stop the
continuous monitor -- which is the best evidence source in the repo, and whose
absence would not be noticed until the next dropout went unrecorded.

So: the aliases are load-bearing, and these tests treat them that way.
"""
from pathlib import Path

import pytest

from twiddle import cli, control_cli, daemon


def parse(argv):
    """Parse with the real parser, but stop before dispatching."""
    return cli.build_parser().parse_args(argv)


def test_the_installed_launchd_argv_still_parses(tmp_path):
    """Build the real plist and feed its own argv back through the parser.

    This is the actual contract, not a paraphrase of it: if `build_plist`
    changes or a subcommand is renamed, this fails rather than the daemon
    dying quietly at the next login.
    """
    plist = daemon.build_plist(Path("/proj"), 30.0,
                               Path("/proj/logs/daemon.jsonl"),
                               50_000_000, "192.168.1.2")
    argv = plist["ProgramArguments"]
    # Strip the `uv run --project <dir> twiddle` prefix; the rest is ours.
    tail = argv[argv.index("twiddle") + 1:]
    args = parse(tail)
    assert args.func is cli.cmd_watch
    assert args.duration == 0
    assert args.anchor == "192.168.1.2"
    assert args.quiet is True


@pytest.mark.parametrize("argv,expected", [
    (["scan"], "cmd_scan"),
    (["watch", "--duration", "0"], "cmd_watch"),
    (["analyse"], "cmd_analyse"),
    (["ping"], "cmd_ping"),
    (["baseline"], "cmd_baseline"),
    (["baseline-diff"], "cmd_baseline_diff"),
    (["daemon", "status"], "cmd_daemon"),
    (["soak"], "cmd_soak"),
    (["radio", "http://example/s"], "cmd_radio"),
    (["serve", "media"], "cmd_serve"),
])
def test_old_top_level_diagnostic_names_still_work(argv, expected):
    assert parse(argv).func.__name__ == expected


@pytest.mark.parametrize("argv,expected", [
    (["diag", "scan"], "cmd_scan"),
    (["diag", "watch", "--duration", "0"], "cmd_watch"),
    (["diag", "analyse"], "cmd_analyse"),
    (["diag", "daemon", "status"], "cmd_daemon"),
    (["diag", "soak"], "cmd_soak"),
])
def test_diag_namespace_reaches_the_same_functions(argv, expected):
    assert parse(argv).func.__name__ == expected


@pytest.mark.parametrize("argv", [
    ["alarm", "list", "--json"],
    ["--json", "alarm", "list"],
    ["alarm", "--json", "list"],
])
def test_alarm_list_takes_json_anywhere(argv):
    args = parse(argv)
    assert args.json is True and args.func.__name__ == "cmd_list"
    assert args.func.__module__ == "twiddle.alarm_cli"


def test_control_commands_accept_json_after_the_subcommand():
    """Agents write `status --json`, not `--json status`. Both must work."""
    assert parse(["status", "--json"]).json is True
    assert parse(["--json", "status"]).json is True


@pytest.mark.parametrize("argv", [
    ["play", "--room", "roam"],
    ["pause", "--room", "roam"],
    ["stop", "--room", "roam"],
    ["volume", "--room", "roam"],
    ["volume", "40", "--room", "roam"],
    ["mute", "on", "--room", "roam"],
    ["bass", "--room", "roam"],
    ["treble", "--room", "roam"],
    ["balance", "--room", "roam"],
    ["loudness", "--room", "roam"],
    ["shuffle", "--room", "roam"],
    ["repeat", "--room", "roam"],
    ["stream", "http://example/s", "--room", "roam"],
    ["snapshot", "--room", "roam"],
    ["restore", "--room", "roam"],
    ["group", "--room", "roam", "--to", "living"],
    ["ungroup", "--room", "roam"],
    ["sleep", "--room", "roam"],
])
def test_every_control_command_takes_a_room(argv):
    assert parse(argv).room == "roam"


@pytest.mark.parametrize("argv", [
    ["play", "--room", "roam"],
    ["volume", "40", "--room", "roam"],
    ["mute", "on", "--room", "roam"],
    ["bass", "4", "--room", "roam"],
    ["treble", "4", "--room", "roam"],
    ["balance", "4", "--room", "roam"],
    ["loudness", "on", "--room", "roam"],
    ["shuffle", "on", "--room", "roam"],
    ["repeat", "all", "--room", "roam"],
    ["stream", "http://example/s", "--room", "roam"],
    ["restore", "--room", "roam"],
    ["ungroup", "--room", "roam"],
    ["sleep", "30", "--room", "roam"],
])
def test_every_writing_command_can_be_previewed(argv):
    """--dry-run is the safety rail; a write without one is a trap."""
    assert parse(argv + ["--dry-run"]).dry_run is True


@pytest.mark.parametrize("token,expected", [
    ("44", ("abs", 44)),
    ("0", ("abs", 0)),
    ("+4", ("rel", 4)),
    ("++4", ("rel", 4)),
    ("-4", ("rel", -4)),
    ("--8", ("rel", -8)),
    ("+", ("rel", 5)),
    ("++", ("rel", 5)),
    ("-", ("rel", -5)),
    ("--", ("rel", -5)),
    ("mute", ("mute", None)),
    ("MUTE", ("mute", None)),
    ("unmute", ("unmute", None)),
])
def test_volume_spec_grammar(token, expected):
    assert control_cli.parse_volume_spec(token) == expected


def test_volume_spec_rejects_garbage():
    with pytest.raises(control_cli.BadSpec):
        control_cli.parse_volume_spec("loud")


def test_vol_is_an_alias_for_volume():
    args = parse(["vol", "+3", "--room", "roam"])
    assert args.func.__name__ == "cmd_volume"
    assert args.spec == "+3"


@pytest.mark.parametrize("argv,spec", [
    (["volume", "44", "--room", "roam"], "44"),
    (["volume", "+4", "--room", "roam"], "+4"),
    (["volume", "-4", "--room", "roam"], "-4"),
    (["volume", "-", "--room", "roam"], "-"),
    (["volume", "mute", "--room", "roam"], "mute"),
    (["volume", "--room", "roam"], None),
])
def test_volume_tokens_that_already_survive_argparse(argv, spec):
    """These never hit argparse's negative-number or unknown-option paths."""
    assert parse(argv).spec == spec


@pytest.mark.parametrize("argv,expected_spec", [
    (["volume", "--8", "--room", "roam"], "--8"),
    (["volume", "--", "--room", "roam"], "--"),
    (["volume", "--room", "roam", "--8"], "--8"),
    (["vol", "++3", "--room", "roam"], "++3"),
    (["vol", "--8", "--room", "roam"], "--8"),
])
def test_volume_normalize_argv_rescues_argparse_hostile_tokens(argv, expected_spec):
    """`--8` looks like an unknown option and bare `--` is swallowed by
    argparse as its end-of-options marker; both must survive by going
    through `cli.main`'s normalization, which `parse()` here does not do."""
    normalized = control_cli.normalize_argv(argv)
    args = parse(normalized)
    assert args.room == "roam"
    assert control_cli.parse_volume_spec(args.spec) == \
        control_cli.parse_volume_spec(expected_spec)


# ---- bass, treble, balance, loudness ----------------------------------------

@pytest.mark.parametrize("token,expected", [
    ("4", ("abs", 4)),
    ("-4", ("abs", -4)),
    ("0", ("abs", 0)),
    ("++2", ("rel", 2)),
    ("--2", ("rel", -2)),
    ("++", ("rel", control_cli.EQ_STEP)),
    ("--", ("rel", -control_cli.EQ_STEP)),
])
def test_eq_spec_grammar(token, expected):
    assert control_cli.parse_eq_spec(token) == expected


def test_eq_spec_rejects_a_single_dash_alone():
    """Unlike volume, a lone `-` isn't a legitimate absolute value here
    (there's no digit to be absolute *about*), so it must be rejected
    rather than silently guessed at."""
    with pytest.raises(control_cli.BadSpec):
        control_cli.parse_eq_spec("-")


@pytest.mark.parametrize("cmd,fn", [
    ("bass", "cmd_bass"),
    ("treble", "cmd_treble"),
    ("balance", "cmd_balance"),
])
def test_bass_treble_balance_parse(cmd, fn):
    args = parse([cmd, "-4", "--room", "roam"])
    assert args.func.__name__ == fn
    assert args.spec == "-4"
    assert args.room == "roam"


@pytest.mark.parametrize("argv,expected_spec", [
    (["bass", "--4", "--room", "roam"], "--4"),
    (["bass", "--", "--room", "roam"], "--"),
    (["treble", "--4", "--room", "roam"], "--4"),
    (["balance", "--4", "--room", "roam"], "--4"),
])
def test_eq_normalize_argv_rescues_argparse_hostile_tokens(argv, expected_spec):
    normalized = control_cli.normalize_argv(argv)
    args = parse(normalized)
    assert args.room == "roam"
    assert control_cli.parse_eq_spec(args.spec) == \
        control_cli.parse_eq_spec(expected_spec)


def test_loudness_parses_as_an_optional_on_off():
    assert parse(["loudness", "--room", "roam"]).state is None
    assert parse(["loudness", "on", "--room", "roam"]).state == "on"


def test_shuffle_parses_as_an_optional_on_off():
    assert parse(["shuffle", "--room", "roam"]).state is None
    assert parse(["shuffle", "on", "--room", "roam"]).state == "on"


def test_repeat_parses_as_an_optional_off_all_one():
    assert parse(["repeat", "--room", "roam"]).state is None
    assert parse(["repeat", "all", "--room", "roam"]).state == "all"


def test_diagnostic_write_commands_accept_a_room_name():
    """These predate the household model but must not still demand an IP."""
    for argv in (["soak", "--room", "roam"],
                 ["radio", "http://example/s", "--room", "roam"],
                 ["serve", "media", "--room", "roam"]):
        assert parse(argv).room == "roam"


def test_control_modules_import_nothing_that_writes_outside_play():
    """Every write must go through `play`, which journals it.

    `analyse` discounts events within 45s of a journalled write, so a module
    that talks SOAP itself makes its own writes invisible to that discount and
    manufactures phantom faults.

    Asserted on imports rather than by grepping for `soap(`: these modules do
    not import it today, so a source grep passes whether or not the property
    holds, and would keep passing if someone added `from .devices import soap`
    and called it as `devices.soap(...)`.
    """
    import ast

    from twiddle import control_cli, household

    read_only = {"Device", "discover", "load_device", "link_stats",
                 "sample_phy_rate", "primary_ip", "PORT"}
    for mod in (household, control_cli):
        tree = ast.parse(Path(mod.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "devices":
                names = {a.name for a in node.names}
                leaked = names - read_only
                assert not leaked, (
                    f"{mod.__name__} imports {leaked} from devices; writes "
                    "must go through play so they are journalled")
            if isinstance(node, ast.ImportFrom) and node.module is None:
                # `from . import x` -- devices itself must not be pulled in
                # wholesale, since that re-exposes soap().
                assert "devices" not in {a.name for a in node.names}, (
                    f"{mod.__name__} imports the devices module wholesale, "
                    "which re-exposes soap() outside the journal")


# ---- the relay -------------------------------------------------------------


@pytest.mark.parametrize("argv,expected", [
    (["relay", "doctor"], "cmd_doctor"),
    (["relay", "login"], "cmd_login"),
    (["relay", "measure", "--source", "tone"], "cmd_measure"),
    (["relay", "start", "--room", "roam"], "cmd_start"),
])
def test_relay_subcommands_parse(argv, expected):
    assert parse(argv).func.__name__ == expected


def test_relay_is_not_aliased_at_the_top_level():
    """Unlike the diagnostic verbs, `relay` has no legacy spelling to keep.

    The hidden top-level aliases exist because a plist on disk invokes them by
    absolute argv. Nothing invokes `relay`, so adding an alias would only
    widen the surface that has to keep working forever.
    """
    with pytest.raises(SystemExit):
        parse(["doctor"])


def test_relay_start_defaults_to_restoring_the_room():
    """Snapshot-then-restore is the repo's ritual; the relay must not opt out.

    A relay is left running for hours, so the room it borrows has to come back
    the way it was found without anyone remembering to ask.
    """
    args = parse(["relay", "start", "--room", "roam"])
    assert args.no_restore is False
    assert args.dry_run is False
    assert args.source == "spotify"


def test_relay_start_can_preview_without_writing():
    assert parse(["relay", "start", "--room", "roam", "--dry-run"]).dry_run


def test_every_source_the_parser_accepts_can_actually_be_built():
    """`--source` choices and the builder must not drift apart."""
    from twiddle import relay, relay_cli
    for kind in relay.SOURCE_HELP:
        args = parse(["relay", "start", "--source", kind, "--source-arg",
                      "/dev/null" if kind == "file" else "440"])
        argv, title = relay_cli._build_source(args)
        assert argv and title


# ---- spotify ---------------------------------------------------------------


@pytest.mark.parametrize("argv,expected", [
    (["spotify", "auth"], "cmd_auth"),
    (["spotify", "devices"], "cmd_devices"),
    (["spotify", "search", "miles davis"], "cmd_search"),
    (["spotify", "now"], "cmd_now"),
    (["spotify", "play"], "cmd_play"),
    (["spotify", "play", "kind", "of", "blue"], "cmd_play"),
    (["spotify", "pause"], "cmd_pause"),
    (["spotify", "next"], "cmd_next"),
    (["spotify", "prev"], "cmd_prev"),
    (["spotify", "queue", "so what"], "cmd_queue"),
    (["spotify", "discover"], "cmd_discover"),
    (["relay", "up", "--room", "roam"], "cmd_relay_up"),
    (["relay", "down"], "cmd_relay_down"),
    (["relay", "status"], "cmd_relay_status"),
])
def test_spotify_and_supervision_subcommands_parse(argv, expected):
    assert parse(argv).func.__name__ == expected


def test_play_accepts_a_bare_query_as_several_words():
    """`spotify play kind of blue` must not need quoting to work."""
    args = parse(["spotify", "play", "kind", "of", "blue"])
    assert args.what == ["kind", "of", "blue"]


def test_play_with_no_argument_means_resume():
    assert parse(["spotify", "play"]).what == []


def test_relay_up_requires_a_room():
    """A detached relay with no room would serve audio to nothing."""
    with pytest.raises(SystemExit):
        parse(["relay", "up"])


def test_every_spotify_command_can_answer_json():
    """The envelope is what makes this usable by an agent rather than a person."""
    for argv in (["spotify", "devices"], ["spotify", "now"],
                 ["spotify", "play"], ["spotify", "discover"], ["relay", "status"]):
        assert parse([*argv, "--json"]).json is True


def test_discover_defaults_to_roam_without_prompting():
    """The household has two groups, so --room would otherwise be required.

    `discover` is the one command that defaults it, to the room the owner
    actually listens on, so the interactive shell never has to ask first.
    """
    assert parse(["spotify", "discover"]).room == "roam"


def test_discover_takes_an_artist_and_dry_run():
    args = parse(["spotify", "discover", "Radiohead", "--dry-run"])
    assert args.artist == ["Radiohead"]
    assert args.dry_run is True


def test_search_resolution_asks_for_a_page_not_a_single_result():
    """Spotify's ranking is unstable for small `limit`; measured, for albums
    matching "kind of blue": limit=1 returned a Vince Guaraldi record, while
    limit>=5 returned Kind Of Blue. Asking for exactly one result is how
    `spotify play` ends up playing the wrong thing.
    """
    from twiddle import spotify_cli
    assert spotify_cli.RESOLVE_LIMIT >= 5


def test_play_resolves_through_the_page_limit(monkeypatch):
    from twiddle import spotify_cli

    seen = {}

    class FakeSession:
        def search(self, query, kind, limit):
            seen["limit"] = limit
            return [{"uri": "spotify:album:right", "type": "album",
                     "name": "Kind Of Blue", "artists": [{"name": "Miles"}]}]

    args = parse(["spotify", "play", "kind", "of", "blue", "--type", "album"])
    uri, label, _ = spotify_cli._resolve_uri(args, FakeSession(), "kind of blue")
    assert seen["limit"] == spotify_cli.RESOLVE_LIMIT
    assert uri == "spotify:album:right"


@pytest.mark.parametrize("given,expected", [
    ("spotify:track:abc", "spotify:track:abc"),
    ("https://open.spotify.com/album/xyz", "spotify:album:xyz"),
    ("https://open.spotify.com/track/abc?si=123", "spotify:track:abc"),
])
def test_a_uri_or_share_link_is_used_verbatim_without_searching(given, expected):
    """Pasting a share link should play that exact thing, not search for it."""
    from twiddle import spotify_cli

    class Boom:
        def search(self, *a, **kw):
            raise AssertionError("searched for something already identified")

    args = parse(["spotify", "play", given])
    uri, _label, _ = spotify_cli._resolve_uri(args, Boom(), given)
    assert uri == expected


def test_play_moves_and_starts_in_one_call_not_two():
    """transfer-then-play races and 403s on something that worked.

    Passing device_id to /me/player/play already moves playback there, so
    the separate transfer only creates a window where Spotify auto-resumes
    and then rejects the follow-up with `Restriction violated`.
    """
    import inspect

    from twiddle import spotify_cli
    src = inspect.getsource(spotify_cli.cmd_play)
    assert "sess.transfer(" not in src, "cmd_play still transfers separately"
    assert "sess.play(uri, device_id=device_id)" in src


@pytest.mark.parametrize("token,seconds", [
    ("30", 30 * 60), ("45m", 45 * 60), ("1h", 3600), ("1h30", 5400),
    ("1h30m", 5400), ("1:30", 5400), ("2H", 7200),
    ("off", 0), ("cancel", 0), ("0", 0),
])
def test_sleep_spec_grammar(token, seconds):
    assert control_cli.parse_sleep_spec(token) == seconds


@pytest.mark.parametrize("token", ["soon", "25h", "1:xx", "-5"])
def test_sleep_spec_rejects_garbage_and_absurd(token):
    with pytest.raises(control_cli.BadSpec):
        control_cli.parse_sleep_spec(token)


def _fake_target(monkeypatch):
    from types import SimpleNamespace
    res = SimpleNamespace(group=SimpleNamespace(ip="10.0.0.4", name="Roam"),
                          redirected=False, reason="",
                          to_dict=lambda: {"acted_on": "Roam"})
    monkeypatch.setattr(control_cli, "_target", lambda args: (res, None))


def test_sleep_reads_sets_and_cancels(monkeypatch, capsys):
    from twiddle import play
    _fake_target(monkeypatch)
    timer = {"left": None}
    calls = []

    def fake_set(ip, seconds, **journal):
        calls.append((ip, seconds, journal))
        timer["left"] = seconds or None
        return play.datetime.now(play.timezone.utc) if seconds else None
    monkeypatch.setattr(play, "set_sleep_timer", fake_set)
    monkeypatch.setattr(play, "get_sleep_timer", lambda ip: timer["left"])

    args = parse(["sleep", "--room", "roam"])
    assert args.func(args) == 0
    assert "sleep timer: off" in capsys.readouterr().out
    args = parse(["sleep", "45", "--room", "roam"])
    assert args.func(args) == 0
    assert "sleeps in 45:00" in capsys.readouterr().out
    assert calls[-1] == ("10.0.0.4", 2700, {"source": "cli"})
    args = parse(["sleep", "off", "--room", "roam"])
    assert args.func(args) == 0 and calls[-1][1] == 0
    args = parse(["sleep", "30", "--room", "roam", "--dry-run"])
    assert args.func(args) == 0 and len(calls) == 2            # a preview writes nothing


def test_sleep_says_so_when_the_speaker_ignores_the_timer(monkeypatch, capsys):
    from twiddle import play
    _fake_target(monkeypatch)
    monkeypatch.setattr(play, "set_sleep_timer",
                        lambda ip, s, **j: play.datetime.now(play.timezone.utc))
    monkeypatch.setattr(play, "get_sleep_timer", lambda ip: None)
    args = parse(["sleep", "30", "--room", "roam"])
    assert args.func(args) == 1
    assert "reports none running" in capsys.readouterr().err
