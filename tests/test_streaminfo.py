"""Stream format parsing and caching -- no network, no ffprobe."""
import json

from twiddle import streaminfo
from twiddle.streaminfo import StreamInfo


def _ffprobe(codec="mp3", br="128000", rate="44100", ch=2, profile="unknown", fmt_br=None):
    return json.dumps({"streams": [{"codec_type": "audio", "codec_name": codec, "profile": profile,
                                    "bit_rate": br, "sample_rate": rate, "channels": ch}],
                       "format": {"bit_rate": fmt_br} if fmt_br else {}})


def test_parses_the_audio_stream_and_labels_it():
    info = streaminfo.parse_ffprobe(_ffprobe())
    assert info == StreamInfo("mp3", 128, 44100, 2)
    assert info.label() == "MP3 128k · 44.1kHz stereo" and not info.low


def test_kqed_shape_is_low_quality():
    info = streaminfo.parse_ffprobe(_ffprobe(br="32000", rate="22050", ch=1))
    assert info.label() == "MP3 32k · 22.05kHz mono" and info.low


def test_aac_falls_back_to_the_container_bitrate_and_names_he_profiles():
    info = streaminfo.parse_ffprobe(_ffprobe(codec="aac", br=None, profile="HE-AACv2",
                                             fmt_br="63876"))
    assert info.label() == "AAC HE-AACv2 64k · 44.1kHz stereo"
    lc = streaminfo.parse_ffprobe(_ffprobe(codec="aac", br="160340", profile="LC"))
    assert lc.label() == "AAC 160k · 44.1kHz stereo"


def test_no_audio_stream_is_none():
    assert streaminfo.parse_ffprobe('{"streams": []}') is None
    assert streaminfo.parse_ffprobe("") is None


def test_probes_once_then_serves_the_cache_until_it_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(streaminfo, "probe",
                        lambda url: calls.append(url) or StreamInfo("mp3", 96, 44100, 2))
    assert streaminfo.info("http://x").kbps == 96
    assert streaminfo.info("http://x").kbps == 96
    assert calls == ["http://x"]
    assert streaminfo.cached("http://x", now=10**12) is None      # a week later: stale


def test_a_failed_probe_is_not_cached(monkeypatch):
    monkeypatch.setattr(streaminfo, "probe", lambda url: None)
    assert streaminfo.info("http://x") is None
    assert not streaminfo.CACHE_FILE.exists()
