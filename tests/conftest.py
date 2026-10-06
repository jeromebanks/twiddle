"""Keep every test away from the real per-user state files.

`stations.remember()` is called from code paths the discover tests exercise
for real, and `lookup` caches to disk and reads a saved Discogs token. Without this, a test run would quietly
change what your next `np` asks about.

The intervention journal, `scene`'s cache and its published dataset are redirected too: a test that
plays through a fake must never leave a line in the real
`logs/interventions.jsonl`, where `analyse` would discount a real fault.
So are the launchd agents: an alarm points at the installed agent's port, and
this Mac's real agent must not decide what a test builds.
"""
import pytest

from twiddle import comedy, daemon, lookup, netstats, play, ratelimit, spotify_ops, stations, streaminfo
from twiddle.dial import state as dial_state
from twiddle.scene import cache as scene_cache
from twiddle.scenedata import cache as scenedata_cache
from twiddle.scenedata import geocode
from twiddle.scenespec import dataset as scene_dataset


@pytest.fixture(autouse=True)
def _isolated_state_files(tmp_path, monkeypatch):
    netstats.reset()                # request counters are process-global
    monkeypatch.setattr(ratelimit, "LEDGER_PATH", tmp_path / "ratelimits.json")
    monkeypatch.setattr(ratelimit, "CONFIG_PATH", tmp_path / "ratelimits.toml")
    ratelimit.reset(flush=False)    # governors too: and never write a test's requests to the real ledger
    monkeypatch.setattr(stations, "LAST_SOURCE_FILE", tmp_path / "last-station")
    monkeypatch.setattr(lookup, "CACHE_FILE", tmp_path / "lookup.json")
    monkeypatch.setattr(streaminfo, "CACHE_FILE", tmp_path / "streams.json")
    monkeypatch.setattr(lookup, "DISCOGS_TOKEN_FILE", tmp_path / "discogs.json")
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.setattr(play, "INTERVENTION_LOG", tmp_path / "interventions.jsonl")
    monkeypatch.setattr(daemon, "AGENT_DIR", tmp_path / "LaunchAgents")
    monkeypatch.setattr(scene_cache, "CACHE_DIR", tmp_path / "scene")
    monkeypatch.setattr(scenedata_cache, "CACHE_DIR", tmp_path / "scene")
    monkeypatch.setattr(scene_dataset, "DATASET_PATH", tmp_path / "scene-dataset" / "dataset.json")
    monkeypatch.setattr(geocode, "MAX_PER_BUILD", 0)     # a build under test asks no map
    monkeypatch.setattr(geocode, "_fetch", lambda url: (_ for _ in ()).throw(AssertionError("test reached Nominatim")))
    monkeypatch.setattr(dial_state, "CACHE_DIR", tmp_path / "dial")
    monkeypatch.setattr(comedy, "CACHE_FILE", tmp_path / "comedy_artists.json")
    monkeypatch.setattr(comedy, "ALBUMS_CACHE_FILE", tmp_path / "comedy_albums.json")
    monkeypatch.setattr(comedy, "PLAYED_LOG", tmp_path / "comedy_played.jsonl")
    monkeypatch.setattr(comedy, "PACE_S", 0)  # real pacing would make the suite slow
    # Re-pointing at the relay probes it over HTTP for a cover; fakes have no
    # relay, so answer as a current one would. test_relay_cover tests the probe.
    monkeypatch.setattr(spotify_ops, "relay_cover_url",
                        lambda url, timeout=1.0: url.replace("/stream.mp3", "/cover.jpg"))
    yield
    ratelimit.reset(flush=False)    # before the monkeypatches are undone and the atexit flush could reach the real file
