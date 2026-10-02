"""The dead-letter queue: what a build could not identify, kept with what was tried."""
import json
from datetime import date

from twiddle import cli
from twiddle.scenedata import builder, deadletters as dl
from twiddle.scenespec import dataset
from twiddle.scenespec.band import BandProfile
from twiddle.scenespec.model import Show

TODAY = date(2026, 10, 1)
STORK = "Thee Stork Club, Oakland"
HOPMONK = "Hopmonk, Novato"             # on no venue list
GHOST = Show(date(2026, 10, 5), STORK, ["Ghost Band", "Wiseacre"], source="thelist")
NIGHT = Show(date(2026, 10, 6), HOPMONK, ["Bachata Nightz"], source="thelist")
FOUND = Show(date(2026, 10, 7), STORK, ["Girl Chow"], source="thelist")


class Src:
    name = "thelist"

    def __init__(self, shows):
        self.shows = list(shows)

    def fetch(self):
        return list(self.shows)


class Enr:
    serial = False

    def __init__(self, name, fill=None, raises=None):
        self.name, self.fill, self.raises = name, fill, raises

    def enrich(self, p: BandProfile):
        if self.raises:
            raise self.raises
        if self.fill:
            self.fill(p)


def knows(*names):
    """Lookup that identifies only the named bands."""
    def fill(p):
        if p.band in names:
            from twiddle import lookup
            p.info = lookup.ArtistInfo(name=p.band, mbid="mb-" + p.band)
    return fill


def build(tmp_path, shows, lookup_fill=None, **kw):
    path = tmp_path / "d.json"
    r = builder.build(path=path, sources=[Src(shows)],
                      enrichers=[Enr("lookup", lookup_fill), Enr("bandcamp")],
                      genre_search=lambda n, offline=False: None, wiki=lambda t: None, today=TODAY,
                      spotify_gap=0, pace=lambda s: None, now=lambda: 1_790_000_000.0, **kw)
    return path, r


def letters(path):
    return dl.load(path)["letters"]


def test_a_band_nobody_knows_is_queued_with_what_was_tried_and_a_found_one_is_not(tmp_path):
    path, r = build(tmp_path, [GHOST, FOUND], knows("Girl Chow", "Wiseacre"))
    ls = letters(path)
    assert set(ls) == {"band:ghost band"}
    g = ls["band:ghost band"]
    assert g["kind"] == "band" and g["reason"] == "unfound" and g["status"] == "pending"
    assert g["evidence"]["spotify_checked"] is False and g["show_count"] == 1
    assert g["shows"] == [{"day": "2026-10-05", "venue": STORK, "billing": "Ghost Band, Wiseacre"}]
    assert r.dead_letters["new"] == 1


def test_clues_about_a_billing_that_is_not_a_band_travel_with_it(tmp_path):
    path, _ = build(tmp_path, [Show(date(2026, 10, 6), STORK, ["Bachata Nightz"]),
                               Show(date(2026, 10, 8), STORK, ["Astrozombies SF , Rusty Chains"]),
                               Show(date(2026, 10, 9), STORK, ["AFROBASHMENT(afrobeats"])])
    ls = letters(path)
    assert ls["band:bachata nightz"]["evidence"]["hints"]["looks_like_a_night"] is True
    assert ls["band:astrozombies sf rusty chains"]["evidence"]["hints"]["several_names_joined"] is True
    assert ls["band:afrobashment afrobeats"]["evidence"]["hints"]["unbalanced_parenthesis"] is True


def test_several_same_named_candidates_are_ambiguous_not_unfound(tmp_path):
    def two(p):
        p.spotify_candidates = [{"id": "a", "name": p.band}, {"id": "b", "name": p.band}]
    path, _ = build(tmp_path, [GHOST], two)
    g = letters(path)["band:ghost band"]
    assert g["reason"] == "ambiguous" and [c["id"] for c in g["evidence"]["spotify_candidates"]] == ["a", "b"]


def test_an_errored_or_rate_limited_lookup_is_not_a_dead_letter_it_is_retried(tmp_path):
    path, _ = build(tmp_path, [GHOST], lookup_fill=lambda p: 1 / 0)
    assert "band:ghost band" not in letters(path)


def test_a_room_on_no_venue_list_is_queued_with_its_shows(tmp_path):
    path, _ = build(tmp_path, [NIGHT, FOUND], knows("Girl Chow"))
    v = letters(path)["venue:hopmonk-novato"]
    assert v["kind"] == "venue" and v["reason"] == "unwatched_venue" and v["show_count"] == 1
    assert v["evidence"]["city"] == "Novato" and v["evidence"]["sources"] == ["thelist"]
    assert not any(k for k in letters(path) if k.startswith("venue:stork"))   # a watched room is not


def test_a_later_build_that_finds_the_band_resolves_it_and_one_that_no_longer_bills_it_expires_it(tmp_path):
    path, _ = build(tmp_path, [GHOST, FOUND], knows("Girl Chow"))
    assert letters(path)["band:ghost band"]["status"] == "pending"
    build(tmp_path, [GHOST, FOUND], knows("Girl Chow", "Ghost Band", "Wiseacre"), )
    # the record is still fresh (3 days), so nothing re-looked it up: it is pending still
    assert letters(path)["band:ghost band"]["status"] == "pending"
    build(tmp_path, [FOUND], knows("Girl Chow"))
    assert letters(path)["band:ghost band"]["status"] == "expired"


def test_resolving_with_an_alias_sends_the_band_back_through_the_lookup_under_that_name(tmp_path):
    path, _ = build(tmp_path, [GHOST], knows("Girl Chow"))
    dl.resolve("band:ghost band", {"alias": "Girl Chow"}, by="ai", dataset_path=path)
    seen = []

    def fill(p):
        seen.append((p.band, p.search_name))
    build(tmp_path, [GHOST], fill)
    # the record was marked for a fresh lookup, and the alias is now the billing's alias
    from twiddle.scenedata import cache
    assert cache.alias("Ghost Band") == "Girl Chow"
    assert seen and seen[0][0] == "Ghost Band"
    l = letters(path)["band:ghost band"]
    assert l["applied"] is True and l["resolved_by"] == "ai" and l["attempts"] == 1


def test_a_resolved_or_abandoned_letter_is_never_reopened_by_a_build(tmp_path):
    path, _ = build(tmp_path, [GHOST, NIGHT], knows())
    dl.abandon("band:ghost band", "it is a DJ night", dataset_path=path)
    dl.resolve("venue:hopmonk-novato", {"address": "224 Vintage Way, Novato"}, dataset_path=path)
    build(tmp_path, [GHOST, NIGHT], knows())
    ls = letters(path)
    assert ls["band:ghost band"]["status"] == "abandoned" and "DJ night" in ls["band:ghost band"]["notes"]
    assert ls["venue:hopmonk-novato"]["status"] == "resolved"
    assert ls["venue:hopmonk-novato"]["resolution"]["address"].startswith("224 Vintage")


def test_a_dry_run_and_a_broken_queue_never_touch_or_fail_a_build(tmp_path):
    path, _ = build(tmp_path, [GHOST], knows(), dry_run=True)
    assert not dl.path(path).exists()
    dl.path(path).write_text("{not json")
    path, r = build(tmp_path, [GHOST], knows())          # an unreadable queue starts empty
    assert dataset.load(path).complete and "band:ghost band" in letters(path)


def test_scene_dlq_lists_exports_resolves_and_abandons(tmp_path, capsys):
    path, _ = build(tmp_path, [GHOST, NIGHT], knows())
    # the conftest-redirected default dataset path is tmp_path/'scene-dataset', so publish there too
    default = dataset.default_path()
    default.parent.mkdir(parents=True, exist_ok=True)
    default.write_text(path.read_text())
    dl.path(default).write_text(dl.path(path).read_text())
    assert cli.main(["scene", "dlq"]) == 0
    out = capsys.readouterr().out
    assert "band/unfound/pending" in out and "venue/unwatched_venue/pending" in out and "Ghost Band" in out
    assert cli.main(["scene", "dlq", "export", "--kind", "venue"]) == 0
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [r["id"] for r in rows] == ["venue:hopmonk-novato"] and rows[0]["evidence"]["city"] == "Novato"
    assert cli.main(["scene", "dlq", "resolve", "band:ghost band", "--alias", "Girl Chow", "--by", "ai"]) == 0
    assert cli.main(["scene", "dlq", "abandon", "venue:hopmonk-novato", "--note", "later"]) == 0
    capsys.readouterr()
    ls = dl.load(default)["letters"]
    assert ls["band:ghost band"]["resolution"] == {"alias": "Girl Chow"} and ls["band:ghost band"]["resolved_by"] == "ai"
    assert ls["venue:hopmonk-novato"]["status"] == "abandoned"
    assert cli.main(["scene", "dlq", "resolve", "band:nope", "--alias", "x"]) == 1
    assert cli.main(["scene", "dlq", "resolve", "band:ghost band"]) == 1       # nothing to record
    assert cli.main(["scene", "dlq", "reopen", "band:ghost band"]) == 0
    assert dl.load(default)["letters"]["band:ghost band"]["status"] == "pending"


def test_scene_dlq_scan_rebuilds_the_queue_from_a_dataset_without_a_build(tmp_path, capsys):
    path, _ = build(tmp_path, [GHOST, NIGHT], knows())
    default = dataset.default_path()
    default.parent.mkdir(parents=True, exist_ok=True)
    default.write_text(path.read_text())                 # a dataset, but no queue beside it
    assert not dl.path(default).exists()
    assert cli.main(["scene", "dlq", "scan"]) == 0
    assert "3 new" in capsys.readouterr().out
    assert set(dl.load(default)["letters"]) == {"band:ghost band", "band:wiseacre",
                                                "venue:hopmonk-novato"}
