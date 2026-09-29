"""The published dataset: format, atomic publication, and the reader boundary."""
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date

import pytest

from twiddle import lookup
from twiddle.scene import dataset, genre, profiles
from twiddle.scene.bands import BandProfile
from twiddle.scene.model import Show

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


def test_the_reader_needs_no_tui_collectors_or_network_modules():
    code = ("import sys, twiddle.scene.dataset as d\n"
            "bad = {'textual', 'requests', 'twiddle.scene.sources', 'twiddle.scene.bands',\n"
            "       'twiddle.scene.app', 'twiddle.scene.builder', 'twiddle.scene.bandcamp'}\n"
            "print(sorted(bad & set(sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "[]"


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
