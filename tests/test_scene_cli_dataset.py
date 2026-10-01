"""`scene list` / `scene venue` read the dataset; `scene build` / `schedule` write / print."""
import json
import plistlib
from datetime import date, timedelta

from twiddle import cli
from twiddle.scenespec import dataset
from twiddle.scenedata import builder
from twiddle.scene import cli as scene_cli
from twiddle.scenespec.model import Show

STORK = "Thee Stork Club, Oakland"


def _stock_venues():
    """The venue table a build with the stock Bay Area list publishes."""
    from twiddle.scenedata.venues import DEFAULT_VENUES
    return builder._venue_records(DEFAULT_VENUES, None, None)


def _publish(shows, venues=None, sources=None):
    dataset.publish(dataset.document(shows=shows,
                                     venues=_stock_venues() if venues is None else list(venues),
                                     bands={},
                                     sources=sources or {}, enrichers={}, complete=True,
                                     builder="t"))


def _tomorrow():
    return date.today() + timedelta(days=1)


def _dead_network(monkeypatch):
    import twiddle.scenedata.sources as srcs
    from twiddle.scenedata import venue_info
    boom = lambda *a, **k: 1 / 0            # noqa: E731
    monkeypatch.setattr(srcs, "fetch_all", boom)
    monkeypatch.setattr(builder, "build", boom)
    monkeypatch.setattr(venue_info, "wiki_summary", boom)


def test_scene_list_reads_the_dataset_and_touches_no_network(monkeypatch, capsys):
    _dead_network(monkeypatch)
    _publish([Show(_tomorrow(), STORK, ["Girl Chow"], price="$10"),
              Show(_tomorrow(), "Somewhere Else, S.F.", ["Nobody"])])
    assert cli.main(["scene", "list"]) == 0
    out = capsys.readouterr().out
    assert "Girl Chow" in out and "[$10]" in out and "Nobody" not in out     # watched only
    assert cli.main(["scene", "list", "--all-venues", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert {s["bands"][0] for s in doc["shows"]} == {"Girl Chow", "Nobody"}
    assert doc["generated_at"]


def test_scene_list_without_a_dataset_says_how_to_make_one(monkeypatch, capsys):
    _dead_network(monkeypatch)
    assert cli.main(["scene", "list"]) == 1
    err = capsys.readouterr().err
    assert "no dataset yet" in err and "scene build" in err


def test_scene_list_reports_the_builds_failed_sources(monkeypatch, capsys):
    _publish([Show(_tomorrow(), STORK, ["Girl Chow"])],
             sources={"storkclub": {"ok": False, "error": "site is down"}})
    assert cli.main(["scene", "list"]) == 0
    assert "warning: storkclub: site is down" in capsys.readouterr().out


def test_scene_list_refresh_runs_the_builder_first(monkeypatch, capsys):
    calls = []

    def fake_build(**kw):
        calls.append(kw)
        _publish([Show(_tomorrow(), STORK, ["Freshly Built"])])
    monkeypatch.setattr(builder, "build", fake_build)
    assert cli.main(["scene", "list", "--refresh"]) == 0
    assert calls and "Freshly Built" in capsys.readouterr().out


def test_scene_venue_shows_the_stored_wikipedia_summary_offline(monkeypatch, capsys):
    _dead_network(monkeypatch)
    _publish([], venues=[{"id": "fox-theater", "name": "Fox Theater",
                          "wikipedia_summary": {"extract": "A restored hall.", "url": "http://w"}}])
    assert cli.main(["scene", "venue", "fox"]) == 0
    assert "A restored hall." in capsys.readouterr().out


def test_scene_venue_reads_its_details_from_the_datasets_venue_table(monkeypatch, capsys):
    _dead_network(monkeypatch)
    _publish([])
    assert cli.main(["scene", "venue", "gilman"]) == 0
    assert "924 Gilman" in capsys.readouterr().out


def test_scene_venue_without_a_dataset_says_how_to_make_one(monkeypatch, capsys):
    _dead_network(monkeypatch)
    assert cli.main(["scene", "venue", "gilman"]) == 1
    assert "no dataset yet" in capsys.readouterr().err


def test_scene_build_reports_and_a_busy_lock_exits_75(monkeypatch, capsys):
    def busy(**kw):
        raise builder.BuildBusy("another `scene build` is running")
    monkeypatch.setattr(builder, "build", busy)
    assert cli.main(["scene", "build"]) == scene_cli.BUILD_BUSY == 75
    assert "another `scene build` is running" in capsys.readouterr().err

    def failing(**kw):
        raise builder.BuildError("no listings from any source")
    monkeypatch.setattr(builder, "build", failing)
    assert cli.main(["scene", "build"]) == 1

    def ok(**kw):
        return builder.Result(path=dataset.default_path(), shows=3, bands=5, enriched=2, reused=3)
    monkeypatch.setattr(builder, "build", ok)
    assert cli.main(["scene", "build"]) == 0
    assert "3 shows, 5 bands (2 looked up, 3 reused)" in capsys.readouterr().out


def test_scene_build_passes_its_flags_through(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return builder.Result(path=dataset.default_path())
    monkeypatch.setattr(builder, "build", fake)
    cli.main(["scene", "build", "--days", "10", "--all-venues", "--no-spotify", "--dry-run"])
    assert (seen["days"], seen["all_venues"], seen["use_spotify"], seen["dry_run"]) == \
        (10, True, False, True)


def test_scene_schedule_prints_a_launchd_agent_and_installs_nothing(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["scene", "schedule", "--hours", "4"]) == 0
    out = capsys.readouterr().out
    plist = plistlib.loads(out[:out.index("</plist>") + len("</plist>")].encode())
    assert plist["Label"] == scene_cli.LAUNCHD_LABEL and plist["StartInterval"] == 4 * 3600
    assert plist["ProgramArguments"][-3:] == ["twiddle", "scene", "build"]
    assert "launchctl bootstrap" in out
    assert not (tmp_path / "Library" / "LaunchAgents").exists()


def test_scene_venue_with_a_dataset_that_lists_no_venues_says_so_instead_of_crashing(capsys):
    _publish([], venues=[])
    assert cli.main(["scene", "venue"]) == 1
    assert "no venues" in capsys.readouterr().err
