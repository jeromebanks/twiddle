"""The station catalog: every file is valid, and a bad one can't take the CLI down."""
import re

import pytest

from twiddle import stations
from twiddle.stations import model


def test_catalog_is_valid():
    # Strict: an unknown tag, fetcher or field, or a missing name/url/blurb,
    # raises here, naming the file. At runtime the same file is only skipped.
    got = stations.load_catalog(strict=True)
    assert got.keys() == stations.STATIONS.keys()
    assert len(got) >= 30


@pytest.mark.parametrize("key", list(stations.STATIONS))
def test_each_station_is_well_formed(key):
    s = stations.STATIONS[key]
    # A key is also a shell word (scripts/radio.zsh) and a CLI argument.
    assert re.fullmatch(r"[a-z][a-z0-9]*", key), key
    # The dial splits the blurb into city and description on " -- ".
    city, _, about = s.blurb.partition(" -- ")
    assert city and about, f"{key}: blurb should read 'City -- what it is'"
    assert s.tags, f"{key}: give it at least one tag"
    assert s.url.startswith(("http://", "https://"))


def test_tags_filter_in_list_order():
    electronic = stations.with_tag("electronic")
    assert electronic and all("electronic" in s.tags for s in electronic)
    order = list(stations.STATIONS)
    assert [s.key for s in electronic] == [k for k in order if k in {s.key for s in electronic}]
    assert stations.with_tag(None) == stations.with_tag("all") == list(stations.STATIONS.values())
    counts = stations.tag_counts()
    assert counts["electronic"] == len(electronic)
    assert set(counts) <= set(stations.TAGS)


def test_curated_order_is_kept():
    # 1..0 in the dial reach these, as before the catalog was split up.
    assert list(stations.STATIONS)[:10] == ["kalx", "kqed", "wfmu", "kexp", "kcrw",
                                             "wkcr", "kspc", "kxlu", "kdvs", "kxsf"]


def _write(tmp_path, key, body):
    (tmp_path / f"{key}.toml").write_text(body)


GOOD = 'name = "X"\nurl = "http://x"\nblurb = "Town -- x"\ntags = ["news"]\n'


def test_a_bad_file_is_skipped_with_a_warning_not_raised(tmp_path, capsys):
    _write(tmp_path, "good", GOOD)
    _write(tmp_path, "badtag", GOOD.replace('"news"', '"polka"'))
    _write(tmp_path, "badfetch", GOOD + 'fetch = "nope"\n')
    _write(tmp_path, "half", 'name = "Half\n')                  # a half-written file
    _write(tmp_path, "nourl", 'name = "X"\nblurb = "Town -- x"\n')
    got = model.load_catalog(tmp_path)
    assert list(got) == ["good"]
    err = capsys.readouterr().err
    for name in ("badtag.toml", "badfetch.toml", "half.toml", "nourl.toml"):
        assert name in err
    with pytest.raises(model.CatalogError, match="badfetch.toml"):
        model.load_catalog(tmp_path, strict=True)


def test_fetch_args_become_keyword_arguments(tmp_path, monkeypatch):
    _write(tmp_path, "fipx", GOOD + 'fetch = "radiofrance"\nfetch_args = { rf_id = 7 }\n')
    s = model.load_catalog(tmp_path, strict=True)["fipx"]
    urls = []
    monkeypatch.setattr(stations.net, "get_json", lambda url: urls.append(url) or {})
    s.now_playing()
    assert urls == ["https://api.radiofrance.fr/livemeta/pull/7"]
    hash(s)                                            # a frozen Station stays hashable
