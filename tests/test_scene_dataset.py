"""The published dataset: format, atomic publication, and the reader boundary."""
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date

import pytest

from twiddle import lookup
from twiddle.scenespec import dataset, genre, profiles
from twiddle.scenespec.band import BandProfile
from twiddle.scenespec.model import Show

STORK = "Thee Stork Club, Oakland"
SHOWS = [Show(date(2026, 10, 5), STORK, ["Girl Chow", "Wiseacre"], source="thelist",
              also=["storkclub"], flyer="f", tickets="t", source_url="http://l"),
         Show(date(2026, 10, 6), "Ivy Room, Albany", ["Coup Dville"], source="thelist")]


def doc(shows=SHOWS, **kw):
    kw.setdefault("bands", {})
    return dataset.document(shows=shows, venues=[], sources={}, enrichers={}, complete=True,
                            builder="test", **kw)


def test_round_trip_keeps_shows_provenance_and_stable_ids(tmp_path):
    path = tmp_path / "d.json"
    dataset.publish(doc(generated_at=1_800_000_000), path)
    snap = dataset.load(path)
    assert snap.shows == SHOWS                       # every field, incl. also/flyer/tickets
    assert snap.generated_at == 1_800_000_000 and snap.version == dataset.VERSION
    assert snap.show_ids == [dataset.show_id(SHOWS[0], None), dataset.show_id(SHOWS[1], None)]
    again = tmp_path / "e.json"
    dataset.publish(doc(generated_at=1_800_000_500), again)
    assert dataset.load(again).show_ids == snap.show_ids   # same night, same id next build


def test_show_ids_tell_untitled_nights_apart_and_are_unique():
    a = Show(date(2026, 10, 5), STORK, [], title="Karaoke")
    b = Show(date(2026, 10, 5), STORK, [], title="Freakyoke")
    dupe = Show(date(2026, 10, 5), STORK, [], title="Karaoke")
    ids = dataset.unique_ids([(a, None), (b, None), (dupe, None)])
    assert len(set(ids)) == 3 and ids[0] != ids[1]
    assert ids[2].startswith(ids[0])


def test_no_file_is_none_and_unusable_files_say_why(tmp_path):
    assert dataset.load(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(dataset.DatasetError, match="not valid JSON"):
        dataset.load(bad)
    bad.write_text(json.dumps({"schema": "something.else", "version": 1}))
    with pytest.raises(dataset.DatasetError, match="not a twiddle.scene.dataset"):
        dataset.load(bad)
    bad.write_text(json.dumps({"schema": dataset.SCHEMA, "version": dataset.VERSION + 1}))
    with pytest.raises(dataset.DatasetError, match="update twiddle"):
        dataset.load(bad)


def test_unknown_keys_are_ignored_and_bad_rows_skipped(tmp_path):
    d = doc()
    d["from_the_future"] = {"x": 1}
    d["shows"][0]["new_field"] = "ignored"
    d["shows"].append({"day": "not a date", "venue": "V", "bands": []})
    d["shows"].append("garbage")
    d["bands"] = {"ok": {"name": "Ok"}, "bad": "nope"}
    path = tmp_path / "d.json"
    dataset.publish(d, path)
    snap = dataset.load(path)
    assert snap.shows == SHOWS and list(snap.bands) == ["ok"]


def test_indexes_are_built_by_the_reader_from_the_file(tmp_path):
    rows = [dict(s.to_dict(), id=f"i{n}", venue_id="ivy-room" if "Ivy" in s.venue else None)
            for n, s in enumerate(SHOWS)]
    d = doc(show_rows=rows)
    d["venues"] = [{"id": "ivy-room", "name": "Ivy Room"}]
    path = tmp_path / "d.json"
    dataset.publish(d, path)
    snap = dataset.load(path)
    assert snap.shows_on(date(2026, 10, 5)) == [SHOWS[0]]
    assert snap.shows_at("Ivy Room") == [SHOWS[1]]
    assert snap.venue("ivy room")["id"] == "ivy-room"
    assert "index" not in path.read_text()               # nothing derived is stored


def test_stale_and_age(tmp_path):
    path = tmp_path / "d.json"
    dataset.publish(doc(generated_at=1000), path)
    snap = dataset.load(path)
    assert not snap.stale(now=1000 + 3600) and snap.stale(now=1000 + dataset.STALE_S + 1)
    assert snap.age_s(now=1060) == 60


def test_a_crash_while_publishing_leaves_the_old_dataset_whole(tmp_path, monkeypatch):
    path = tmp_path / "d.json"
    dataset.publish(doc(), path)
    before = path.read_text()

    def boom(*a):
        raise OSError("disk fell over")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        dataset.publish(doc(shows=[]), path)
    assert path.read_text() == before and dataset.load(path).shows == SHOWS
    assert [p.name for p in tmp_path.iterdir()] == ["d.json"]      # no temp file left


def test_a_reader_never_sees_a_partial_file_while_a_writer_runs(tmp_path):
    """The writer streams to a temp name; the real name only ever holds a
    complete document, so a reader mid-publish gets the previous one."""
    path = tmp_path / "d.json"
    dataset.publish(doc(), path)
    seen = []
    real_dump = json.dump

    def dump_and_read(obj, f, **kw):
        real_dump(obj, f, **kw)
        f.flush()
        seen.append(dataset.load(path))          # temp file is written, not yet renamed
    json_dump = json.dump
    try:
        json.dump = dump_and_read
        dataset.publish(doc(shows=SHOWS[:1]), path)
    finally:
        json.dump = json_dump
    assert seen[0].shows == SHOWS                # the old one, complete
    assert dataset.load(path).shows == SHOWS[:1]


def test_the_contract_package_needs_no_client_producer_tui_or_network_modules():
    """`scenespec` is what producers write and clients read: importing all of it
    must pull in neither side, Textual, nor an HTTP library."""
    code = ("import sys, twiddle.scenespec.dataset, twiddle.scenespec.profiles, "
            "twiddle.scenespec.genre, twiddle.scenespec.band, twiddle.scenespec.model\n"
            "bad = sorted(m for m in sys.modules if m in {'textual', 'requests'} or "
            "m.startswith(('twiddle.scene.', 'twiddle.scenedata')) or m == 'twiddle.scene')\n"
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "[]"


# ---- the header says which dataset this is ---------------------------------------


def test_the_header_names_the_dataset_and_a_file_without_one_still_loads(tmp_path):
    path = tmp_path / "d.json"
    dataset.publish(doc(identity={"id": "nyc-comedy", "name": "NYC comedy",
                                  "region": "New York, NY", "kind": "comedy", "junk": "x"}), path)
    snap = dataset.load(path)
    assert (snap.id, snap.name, snap.region, snap.kind) == \
        ("nyc-comedy", "NYC comedy", "New York, NY", "comedy")
    assert "junk" not in json.loads(path.read_text()) and snap.label == "NYC comedy"
    old = tmp_path / "old.json"
    dataset.publish(doc(), old)                         # a producer that predates the header
    bare = dataset.load(old)
    assert (bare.id, bare.name, bare.region, bare.kind) == ("", "", "", "")
    assert bare.label == "old"                          # falls back to the file's name
    assert "id" not in json.loads(old.read_text())      # and nothing is written for it


def test_load_all_reads_each_dataset_in_order_skips_missing_and_names_a_bad_one(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    dataset.publish(doc(identity={"id": "a"}), a)
    dataset.publish(doc(identity={"id": "b"}), b)
    assert [s.id for s in dataset.load_all([b, tmp_path / "missing.json", a])] == ["b", "a"]
    assert dataset.load_all([tmp_path / "missing.json"]) == []
    assert [s.path for s in dataset.load_all()] == []   # the conftest-redirected default: none yet
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(dataset.DatasetError, match="bad.json"):
        dataset.load_all([a, bad])


# ---- profiles <-> records ----------------------------------------------------------


def _profile():
    p = BandProfile(band="Girl Chow")
    p.status = {"lookup": "done", "spotify": "done", "bandcamp": "done"}
    p.info = lookup.ArtistInfo(
        name="Girl Chow", mbid="mb1", origin="Oakland, California", genres=["punk"],
        links={"bandcamp": "https://girlchow.bandcamp.com"}, matched_by="unique name match",
        album=lookup.AlbumInfo(title="Demo", mbid="al1", year="2026", genres=["punk"],
                               links={"x": "y"}))
    p.spotify_artist = {"id": "sp1", "name": "Girl Chow"}
    p.tracks = [{"uri": "spotify:track:1", "name": "One"}]
    p.bandcamp = {"name": "Girl Chow", "item_url_root": "https://girlchow.bandcamp.com"}
    p.bc_tracks = [{"title": "t"}]
    p.alias = "Girl"
    p.searched = {"spotify": "Girl"}
    return p


def test_a_profile_survives_the_json_round_trip_including_album_and_guess():
    guess = genre.Guess(scores=Counter({"punk": 3.0, "rock": 1.0}), tags=["punk"],
                        sources={"bandcamp", "musicbrainz"}, sure=False)
    rec = json.loads(json.dumps(profiles.to_record(_profile(), updated_at=1.0, guess=guess)))
    assert rec["identifiers"] == {"mbid": "mb1", "spotify_id": "sp1",
                                  "bandcamp": "https://girlchow.bandcamp.com"}
    back = profiles.from_record(rec)
    assert back.info == _profile().info and back.info.album.title == "Demo"
    assert back.spotify_artist == {"id": "sp1", "name": "Girl Chow"}
    assert back.tracks == _profile().tracks and back.bc_tracks == [{"title": "t"}]
    assert back.alias == "Girl" and back.searched == {"spotify": "Girl"}
    assert back.status == {"lookup": "done", "spotify": "done", "bandcamp": "done",
                           "tracks": "idle"}          # song lists are the app's, per band
    g = profiles.guess_from_dict(rec["genre"])
    assert g.scores == guess.scores and g.sources == guess.sources and g.sure is False
    assert isinstance(g.scores, Counter) and isinstance(g.sources, set)


def test_an_unenriched_band_comes_back_idle_for_every_enricher():
    p = profiles.from_record(profiles.minimal_record("Nobody", None))
    assert p.status == {"lookup": "idle", "spotify": "idle", "bandcamp": "idle",
                        "tracks": "idle"}
    assert not p.busy()


def test_an_errored_enricher_is_retried_not_trusted():
    rec = profiles.to_record(_profile(), updated_at=1.0, guess=None)
    rec["status"]["bandcamp"] = "error: rate limited"
    assert profiles.from_record(rec).status["bandcamp"] == "idle"
    assert not profiles.enriched(rec)


def test_a_pin_that_disagrees_with_the_dataset_sends_spotify_back_to_idle():
    rec = profiles.to_record(_profile(), updated_at=1.0, guess=None)

    def pins(spotify_id):
        return lambda band: {"spotify_id": spotify_id, "name": ""}
    assert profiles.from_record(rec, pinned=pins("sp1")).status["spotify"] == "done"
    assert profiles.from_record(rec, pinned=pins("other")).status["spotify"] == "idle"
    assert profiles.from_record(rec, pinned=pins(None)).status["spotify"] == "idle"
    assert profiles.from_record(rec, pinned=lambda b: None).status["spotify"] == "done"


def test_the_snapshot_carries_the_mtime_of_what_was_read(tmp_path):
    path = tmp_path / "d.json"
    dataset.publish(doc(), path)
    os.utime(path, (1, 1_700_000_000))
    assert dataset.load(path).mtime == 1_700_000_000


def test_cache_writes_use_a_private_temp_name_and_leave_nothing_behind(tmp_path, monkeypatch):
    from twiddle.scene import cache
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache._write("x.json", {"a": 1})
    cache._write("x.json", {"a": 2})
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"] and cache._read("x.json") == {"a": 2}


def test_a_mismatched_pin_forgets_the_datasets_artist_and_its_tracks():
    """Otherwise the song-list fetch, woken beside the pinned lookup, could put
    the wrong artist's tracks back -- or a "not on Spotify" band's."""
    from twiddle.scene.book import TrackEnricher
    from twiddle.scenedata.bands import SpotifyEnricher
    rec = profiles.to_record(_profile(), updated_at=1.0, guess=None)
    rec["tracks"] = [{"uri": "spotify:track:1"}]
    for pin in ("other", None):
        p = profiles.from_record(rec, pinned=lambda b, pin=pin: {"spotify_id": pin})
        assert p.spotify_artist is None and p.tracks == [] and p.spotify_candidates == []
        assert p.status["spotify"] == "idle"
        asked = []

        class Spy(SpotifyEnricher):
            def session(self):
                asked.append(1)
                return object()
        p.bandcamp = None                       # only the Spotify side is under test
        enr = TrackEnricher(Spy(lambda: object(), tracks=False))
        enr.enrich(p)
        assert asked == [] and p.tracks == [] and not enr.wants_rerun(p)
    kept = profiles.from_record(rec, pinned=lambda b: {"spotify_id": "sp1"})
    assert kept.spotify_artist == {"id": "sp1", "name": "Girl Chow"} and kept.tracks


def test_the_lookup_cache_is_saved_by_atomic_rename_so_a_reader_never_sees_half(tmp_path, monkeypatch):
    import threading
    from twiddle import lookup
    monkeypatch.setattr(lookup, "CACHE_FILE", tmp_path / "lookup.json")
    big = {f"k{i}": {"at": 9e12, "artist": None, "candidates": []} for i in range(2000)}
    lookup._cache_save(big)
    stop, torn = threading.Event(), []

    def reader():
        while not stop.is_set():
            got = lookup._cache_load()
            if len(got) < 2000:
                torn.append(len(got))
    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(60):
            lookup._cache_save(dict(big, extra={"at": 9e12 + i}))
    finally:
        stop.set()
        t.join()
    assert torn == [] and len(lookup._cache_load()) == 2001
    assert [p.name for p in tmp_path.iterdir()] == ["lookup.json"]


def test_shows_at_finds_an_unwatched_room_with_or_without_an_id(tmp_path):
    """Codex: venue_id=None made shows_at return nothing for rooms nobody watches."""
    away = Show(date(2026, 10, 8), "Somewhere Else, S.F.", ["Nobody"], source="thelist")
    path = tmp_path / "d.json"
    old = doc(shows=[away], show_rows=[dict(away.to_dict(), id="x", venue_id=None)])   # an older file
    dataset.publish(old, path)
    assert dataset.load(path).shows_at("Somewhere Else, S.F.") == [away]
    assert dataset.load(path).shows_at("somewhere else") == [away]
    new = doc(shows=[away], show_rows=[dict(away.to_dict(), id="x",
                                            venue_id=dataset.venue_id("Somewhere Else, S.F."))])
    dataset.publish(new, path)
    assert dataset.load(path).shows_at("Somewhere Else, S.F.") == [away]
