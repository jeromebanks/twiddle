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
NIGHT = Show(date(2026, 10, 6), HOPMONK, ["Girl Chow Showcase"], source="thelist")
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
    path, _ = build(tmp_path, [Show(date(2026, 10, 6), STORK, ["Girl Chow Showcase"]),
                               Show(date(2026, 10, 8), STORK, ["Astrozombies SF , Rusty Chains"]),
                               Show(date(2026, 10, 9), STORK, ["AFROBASHMENT(afrobeats"])])
    ls = letters(path)
    assert ls["band:girl chow showcase"]["evidence"]["hints"]["looks_like_a_night"] is True
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
    assert cache.override("Ghost Band")["alias"] == "Girl Chow"     # an override, not the expiring guess
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


def test_an_event_is_not_a_dead_letter_and_an_old_letter_for_one_is_closed(tmp_path):
    from twiddle.scenedata import deadletters as dl
    from twiddle.scenedata.bands import non_band
    for name in ("Private Event", "Membership Meeting", "Karaoke Tuesday", "Bachata Nightz",
                 "Salsa Crazy Mondays", "Hamdi FC vs. San Francisco"):
        assert non_band(name), name
    for name in ("Mindi Abair", "Heavens To Betsey", "The Avengers", "Sunday Driver"):
        assert not non_band(name), name
    rec = {"name": "Private Event", "updated_at": "x", "status": {"lookup": "done", "bandcamp": "done"}}
    assert dl.classify_band(rec) is None
    path = tmp_path / "dataset.json"
    doc = dl.load(path)
    doc["letters"]["band:private-event"] = {"kind": "band", "name": "Private Event", "status": "pending",
                                            "reason": "unfound", "show_count": 6}
    dl.save(doc, path)
    dl.sync({}, path, billed={"band:private-event"})
    l = dl.load(path)["letters"]["band:private-event"]
    assert l["status"] == "resolved" and "not_a_band" in l["resolution"]


def test_the_spellings_of_one_room_are_one_letter(tmp_path):
    spellings = ["Hopmonk, Novato", "Hopmonk Tavern, Novato", "Felton Music Hall, 6275 Hwy 9, Felton",
                 "Felton Music Hall, Felton", "Guild Theater, Memlo Park", "Guild Theater, Meno Park",
                 "Siesta Valley Bowl, East Bay", "Siesta Valley Bowl, Orinda", "Hopmonk, Sebastopol"]
    shows = [Show(date(2026, 10, 6 + i), v, ["X"], source="thelist") for i, v in enumerate(spellings)]
    ls = dl.venue_letters(shows, ())
    assert len(ls) == 5                        # Hopmonk Novato, Hopmonk Sebastopol, Felton, Guild, Siesta
    felton = next(l for l in ls.values() if "Felton" in l["name"])
    assert felton["show_count"] == 2 and felton["evidence"]["address"] == "6275 Hwy 9"
    assert next(l for l in ls.values() if "Guild" in l["name"])["evidence"]["city"] == "Menlo Park"
    assert next(l for l in ls.values() if "Siesta" in l["name"])["evidence"]["city"] == "Orinda"
    hop = next(l for l in ls.values() if l["evidence"]["city"] == "Novato")
    assert sorted(hop["evidence"]["listed_as"]) == ["Hopmonk Tavern, Novato", "Hopmonk, Novato"]


def test_a_city_in_a_listing_is_read_through_typos_and_regions():
    from twiddle.scenedata.venue_names import parse
    p = parse("Alliance Francaise, 1345 Bush Street, S.F.")
    assert (p.name, p.address, p.city) == ("Alliance Francaise", "1345 Bush Street", "San Francisco")
    assert parse("Hopmonk, Sebastorpl").city == "Sebastopol"
    assert parse("Hertz Hall, East Bay").city == "" and parse("Castro,").name == "Castro"
    assert parse("Some Bar, Oakland, CA").state == "CA"
    assert parse("Frost Amphitheater, Stanford Campus").city == "Stanford"


def test_the_one_local_candidate_among_same_named_artists_is_chosen_by_rule(tmp_path):
    from twiddle.scenedata import cache
    path = tmp_path / "dataset.json"
    cand = lambda mbid, d: {"name": "Abracadabra", "mbid": mbid, "disambiguation": d}
    doc = dl.load(path)
    for lid, name, cands in (
            ("band:abracadabra", "Abracadabra", [cand("a", "Argentine band"), cand("b", "pop duo from Oakland, CA")]),
            ("band:castle", "Castle", [cand("c", "Bay Area band"), cand("d", "metal from San Jose, California")]),
            ("band:melt", "Melt", [cand("e", "Swedish band"), cand("f", "Japanese band")])):
        doc["letters"][lid] = {"kind": "band", "name": name, "reason": "ambiguous", "status": "pending",
                               "evidence": {"lookup_candidates": cands}, "show_count": 1}
    dl.save(doc, path)
    assert dl.settle_ambiguous(path) == 1
    ls = dl.load(path)["letters"]
    assert ls["band:abracadabra"]["resolution"]["mbid"] == "b" and ls["band:abracadabra"]["resolved_by"] == "rule"
    assert ls["band:castle"]["status"] == "pending" and ls["band:melt"]["status"] == "pending"   # two local / none
    assert dl.apply_resolutions(path) == ["abracadabra"] and cache.pick("Abracadabra") == "b"
    assert dl.apply_resolutions(path) == []                                  # once


def test_a_chosen_artist_is_what_the_lookup_asks_for(tmp_path, monkeypatch):
    from twiddle import lookup
    from twiddle.scenedata import cache
    from twiddle.scenedata.bands import LookupEnricher
    from twiddle.scenespec.band import BandProfile
    cache.save_pick("Abracadabra", "mb-b")
    seen = []
    monkeypatch.setattr(lookup, "identify", lambda name, **kw: seen.append((name, kw.get("mb_artist_id")))
                        or lookup.Result(None, []))
    LookupEnricher().enrich(BandProfile(band="Abracadabra"))
    assert seen[0] == ("Abracadabra", "mb-b")


def test_the_build_publishes_unwatched_rooms_as_known_venues_and_a_resolution_describes_them(tmp_path):
    path, _ = build(tmp_path, [NIGHT, FOUND], knows("Girl Chow"))
    snap = dataset.load(path)
    (row,) = snap.known_venues
    assert row["id"] == "hopmonk-novato" and row["city"] == "Novato" and row["match"] == ["hopmonk, novato"]
    assert not any(v["name"] == "Hopmonk" for v in snap.venues)            # known, not watched
    dl.resolve("venue:hopmonk-novato", {"address": "224 Vintage Way, Novato", "url": "https://hopmonk.com"},
               by="ai", dataset_path=path)
    path, _ = build(tmp_path, [NIGHT, FOUND], knows("Girl Chow"))
    (row,) = dataset.load(path).known_venues
    assert row["address"] == "224 Vintage Way, Novato" and row["url"] == "https://hopmonk.com"


def _pending(path, lid="band:ghost band", name="Ghost Band"):
    doc = dl.load(path)
    doc["letters"][lid] = {"kind": "band", "name": name, "reason": "unfound", "status": "pending",
                           "evidence": {}, "show_count": 1}
    dl.save(doc, path)


def test_a_lookup_that_failed_leaves_the_letter_pending_not_resolved(tmp_path):
    # Codex: an errored or rate-limited record is not "found by a later build"
    path = tmp_path / "dataset.json"
    _pending(path)
    bands = {"ghost band": {"name": "Ghost Band", "updated_at": "x",
                            "status": {"lookup": "error: unreachable", "bandcamp": "done"}}}
    assert dl.unanswered_ids(bands) == {"band:ghost band"}
    dl.sync({}, path, billed={"band:ghost band"}, unanswered=dl.unanswered_ids(bands))
    assert dl.load(path)["letters"]["band:ghost band"]["status"] == "pending"
    answered = {"ghost band": {"name": "Ghost Band", "updated_at": "x", "info": {"name": "Ghost Band"},
                               "status": {"lookup": "done", "bandcamp": "done"}}}
    dl.sync({}, path, billed={"band:ghost band"}, unanswered=dl.unanswered_ids(answered))
    assert dl.load(path)["letters"]["band:ghost band"]["status"] == "resolved"      # a real answer


def test_reopening_an_applied_resolution_removes_what_it_left_in_the_cache(tmp_path):
    from twiddle.scenedata import cache
    path = tmp_path / "dataset.json"
    _pending(path)
    dl.resolve("band:ghost band", {"mbid": "wrong-id", "alias": "Ghosts"}, "ai", path)
    assert dl.apply_resolutions(path) == ["ghost band"]
    assert cache.pick("Ghost Band") == "wrong-id" and cache.override("Ghost Band")["alias"] == "Ghosts"
    dl.reopen("band:ghost band", path)
    assert cache.override("Ghost Band") == {}
    assert dl.apply_resolutions(path) == ["ghost band"]          # looked up afresh, once
    assert dl.apply_resolutions(path) == []
    cache.save_alias("Ghost Band", "A Build's Own")              # a build's alias is not ours to drop
    cache.drop_override("Ghost Band", alias="Ghosts")
    assert cache.alias("Ghost Band") == "A Build's Own"


def test_two_writers_of_the_queue_cannot_undo_each_other(tmp_path):
    import threading
    path = tmp_path / "dataset.json"
    _pending(path)
    done = threading.Event()
    with dl._txn(path):                       # a build is in the middle of its read-modify-write
        t = threading.Thread(target=lambda: (dl.resolve("band:ghost band", {"alias": "G"}, "human", path),
                                             done.set()))
        t.start()
        assert not done.wait(0.3)             # the person's `resolve` waits its turn
    t.join(2)
    assert done.is_set() and dl.load(path)["letters"]["band:ghost band"]["status"] == "resolved"


def test_an_explicit_alias_is_searched_even_when_the_billing_has_candidates(monkeypatch):
    # Codex: the alias cache was only read after a miss, so a resolution for an ambiguous
    # band (which has candidates) was ignored while its letter said "applied"
    from twiddle import lookup
    from twiddle.scenedata import cache
    from twiddle.scenedata.bands import LookupEnricher
    from twiddle.scenespec.band import BandProfile
    cache.save_override("Abracadabra", alias="Abracadabra (Oakland duo)")
    seen = []

    def identify(name, **kw):
        seen.append(name)
        art = lookup.ArtistInfo(name="Abracadabra", mbid="m") if "Oakland" in name else None
        return lookup.Result(art, [] if art else [{"name": "Abracadabra", "mbid": "a"}, {"name": "Abracadabra", "mbid": "b"}])
    monkeypatch.setattr(lookup, "identify", identify)
    p = BandProfile(band="Abracadabra")
    LookupEnricher().enrich(p)
    assert seen[0] == "Abracadabra (Oakland duo)" and p.info and p.alias == "Abracadabra (Oakland duo)"
    assert p.search_name == "Abracadabra (Oakland duo)"          # Spotify and Bandcamp use it too


def test_a_canadian_candidate_is_not_local_and_only_bay_area_places_settle_an_identity(tmp_path):
    # Codex: ", ca" matched ", Canada"
    from twiddle.scenedata.bands import bay_area_only, near
    assert not near("rock band from Toronto, Canada") and near("metal from San Jose, CA")
    assert not bay_area_only("rock band from Toronto, Canada")
    assert not bay_area_only("band from Albany, New York") and not bay_area_only("Richmond, Virginia")
    assert bay_area_only("pop duo from Oakland, CA") and bay_area_only("Bay Area post-hardcore band")
    path = tmp_path / "dataset.json"
    doc = dl.load(path)
    doc["letters"]["band:x"] = {"kind": "band", "name": "X", "reason": "ambiguous", "status": "pending", "show_count": 1,
                                "evidence": {"lookup_candidates": [
                                    {"name": "X", "mbid": "ca", "disambiguation": "rock band from Toronto, Canada"},
                                    {"name": "X", "mbid": "uk", "disambiguation": "English band"}]}}
    dl.save(doc, path)
    assert dl.settle_ambiguous(path) == 0


def test_an_alias_is_the_search_name_even_when_the_lookup_databases_do_not_know_it(monkeypatch):
    # Codex: Spotify and Bandcamp kept searching the billing unless MusicBrainz found the alias
    from twiddle import lookup
    from twiddle.scenedata import cache
    from twiddle.scenedata.bands import LookupEnricher
    from twiddle.scenespec.band import BandProfile
    cache.save_override("Ghost Band", alias="Ghosts")
    monkeypatch.setattr(lookup, "identify", lambda name, **kw: lookup.Result(None, []))
    p = BandProfile(band="Ghost Band")
    LookupEnricher().enrich(p)
    assert p.search_name == "Ghosts"


def test_replacing_an_applied_resolution_removes_what_the_old_one_left(tmp_path):
    # Codex: a wrong MBID survived a later alias resolution and kept winning in the lookup
    from twiddle.scenedata import cache
    path = tmp_path / "dataset.json"
    _pending(path)
    dl.resolve("band:ghost band", {"mbid": "wrong-id"}, "ai", path)
    assert dl.apply_resolutions(path) == ["ghost band"] and cache.pick("Ghost Band") == "wrong-id"
    dl.resolve("band:ghost band", {"alias": "Ghosts"}, "human", path)       # corrects it
    assert cache.pick("Ghost Band") == ""                                    # the wrong artist is gone
    assert dl.apply_resolutions(path) == ["ghost band"]                      # looked up afresh, once
    assert cache.override("Ghost Band") == {"billed": "Ghost Band", "alias": "Ghosts", "at": cache.override("Ghost Band")["at"]}


def test_a_rooms_key_does_not_move_when_its_spellings_swap_show_counts():
    # Codex: the key came from the most-played spelling, so a rebuild with different counts
    # re-keyed the room and orphaned its resolutions and its cached map answer
    from twiddle.scenedata.venue_names import cluster
    a = cluster({"Hopmonk, Novato": 20, "Hopmonk Tavern, Novato": 3})
    b = cluster({"Hopmonk, Novato": 3, "Hopmonk Tavern, Novato": 20})
    assert [r.key for r in a] == [r.key for r in b] == ["hopmonk-novato"]
    assert a[0].name == "Hopmonk" and b[0].name == "Hopmonk Tavern"     # only the display name follows the counts
