"""WFMU's live show and playlist, against pages captured from wfmu.org -- no network."""
from pathlib import Path

import pytest

from twiddle import stations
from twiddle.stations.fetchers import wfmu

FIXTURES = Path(__file__).parent / "fixtures"
LIVE = (FIXTURES / "wfmu_currentliveshows.html").read_text()
PLAYLIST = (FIXTURES / "wfmu_playlist_169197.html").read_text()
PLAYLIST_URL = "https://wfmu.org/playlists/shows/169197"

# The page as the diagnosis on #47 captured it (2026-10-03, Fool's Paradise),
# rebuilt in the shape of today's capture: the `Show with Host` form.
FOOLS_PARADISE = """\
<div id="nowplaying">
<p>
<span class="nowplayingtext">
&quot;Bury Me Face Down (So I Can See Where I'm Going)&quot;
by
Cowboy Copas
</span>
<br>
<p class="nowplayingreload">
Fool's Paradise with Rex
</p>
<noscript>
<p class="nowplayingreload">
Reload this page to update the song info.
</p>
</noscript>
<p class="nowplayingbox">
<a href="/wfmu_mp3.pls">Listen Now</a>
| <a href="/playlists/shows/169127">See Playlist and Comments</a>
</p>
</div>
"""


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(wfmu, "_cache", {"key": None, "rows": []})


def serve(monkeypatch, pages):
    """Answer net.get from `pages` (url -> text, or an Exception to raise),
    recording every URL asked for."""
    asked = []

    def get(url, headers=None):
        asked.append(url)
        page = pages.get(url, "")
        if isinstance(page, Exception):
            raise page
        return page.encode()
    monkeypatch.setattr(stations.net, "get", get)
    return asked


# ---- currentliveshows.php ------------------------------------------------------------


def test_live_page_names_song_artist_show_and_playlist():
    assert wfmu.parse_current_live_shows(LIVE) == {
        "song": "Los Chucos Suaves", "artist": "Son Rompe Pera",
        "show": "Marty McSorley's show", "playlist_id": "169197"}


def test_live_page_with_a_host_names_show_and_host():
    assert wfmu.parse_current_live_shows(FOOLS_PARADISE) == {
        "song": "Bury Me Face Down (So I Can See Where I'm Going)", "artist": "Cowboy Copas",
        "show": "Fool's Paradise", "hosts": ["Rex"], "playlist_id": "169127"}


def test_with_several_channels_the_freeform_one_is_picked_not_the_first():
    other = (FOOLS_PARADISE.replace("/wfmu_mp3.pls", "/rocknsoul.pls")
             .replace("Cowboy Copas", "Someone Else").replace("169127", "999"))
    got = wfmu.parse_current_live_shows(other + FOOLS_PARADISE)
    assert (got["artist"], got["playlist_id"]) == ("Cowboy Copas", "169127")
    # Several channels and none of them the main stream: nothing is guessed.
    assert wfmu.parse_current_live_shows(other + other) == {}


def test_off_air_page_names_nothing():
    off = LIVE.replace(LIVE[LIVE.index('<span class="nowplayingtext">'):
                            LIVE.index("</span>") + 7], "")
    assert wfmu.parse_current_live_shows(off) == {}
    assert wfmu.parse_current_live_shows("") == {}


# ---- the playlist ----------------------------------------------------------------------


def test_playlist_rows_newest_first_with_every_field():
    rows = wfmu.parse_playlist(PLAYLIST)
    assert [r["song"] for r in rows] == [
        "Los Chucos Suaves", "Cumbia La Malinche", "Everybody's got soul", "Allah Wakbarr",
        "I Cry", "You Don’t Own Me"]
    assert rows[-1] == {"artist": "Klaus Nomi", "song": "You Don’t Own Me",
                        "album": "Klaus Nomi", "label": "RCA", "year": "1981", "format": "LP",
                        "playlist_url": PLAYLIST_URL, "song_id": "614369"}
    # Empty cells (every comment here) are left out, not kept as "".
    assert all("comment" not in r and "" not in r.values() for r in rows)
    assert wfmu.parse_playlist(PLAYLIST, "https://x/1")[0]["playlist_url"] == "https://x/1"


def test_playlist_comment_keeps_its_text_and_drops_its_markup():
    row = PLAYLIST[PLAYLIST.index('<tr id="drop_5145917"'):]
    row = row[:row.index("</tr>") + 5]
    row = row.replace('<td style="display:none" class="song col_comments">\n<font size="-1"></font>',
                      '<td style="display:none" class="song col_comments">\n<font size="-1">'
                      'Thanks <a href="/x">Bill</a>!<br>Req&#39;d by a listener</font>')
    row = row.replace("Millie Jackson", "Pretenders, The")
    got = wfmu.parse_playlist("<table>" + row + "</table>", PLAYLIST_URL)
    assert got[0]["comment"] == "Thanks Bill! Req'd by a listener"
    assert got[0]["artist"] == "The Pretenders"


# ---- the fetcher -----------------------------------------------------------------------


def test_now_playing_takes_album_from_the_playlist_and_leaves_the_song_out_of_recent(monkeypatch):
    asked = serve(monkeypatch, {wfmu.CURRENT: LIVE, PLAYLIST_URL: PLAYLIST})
    np = stations.STATIONS["wfmu"].now_playing()
    assert (np.artist, np.song, np.album, np.show) == (
        "Son Rompe Pera", "Los Chucos Suaves", "Batuco", "Marty McSorley's show")
    assert [r["song"] for r in np.recent] == [
        "Cumbia La Malinche", "Everybody's got soul", "Allah Wakbarr", "I Cry",
        "You Don’t Own Me"]
    assert asked == [wfmu.CURRENT, PLAYLIST_URL]


def test_rows_entered_ahead_of_the_song_on_air_are_not_just_played(monkeypatch):
    # The DJ logged two more songs before playing them; on air is still I Cry.
    live = LIVE.replace("Los Chucos Suaves", "I Cry").replace("Son Rompe Pera", "Millie Jackson")
    serve(monkeypatch, {wfmu.CURRENT: live, PLAYLIST_URL: PLAYLIST})
    np = stations.STATIONS["wfmu"].now_playing()
    assert np.album == "It Hurts So Good"
    assert [r["song"] for r in np.recent] == ["You Don’t Own Me"]


def test_a_song_not_in_the_playlist_yet_keeps_every_row(monkeypatch):
    live = LIVE.replace("Los Chucos Suaves", "Brand New")
    serve(monkeypatch, {wfmu.CURRENT: live, PLAYLIST_URL: PLAYLIST})
    np = stations.STATIONS["wfmu"].now_playing()
    assert np.album is None
    assert len(np.recent) == 6


def test_playlist_is_fetched_again_only_when_its_id_or_the_song_changes(monkeypatch):
    pages = {wfmu.CURRENT: LIVE, PLAYLIST_URL: PLAYLIST}
    asked = serve(monkeypatch, pages)
    s = stations.STATIONS["wfmu"]
    s.now_playing()
    s.now_playing()
    assert asked.count(PLAYLIST_URL) == 1                  # same song: from the cache
    pages[wfmu.CURRENT] = LIVE.replace("Los Chucos Suaves", "Next One")
    s.now_playing()
    assert asked.count(PLAYLIST_URL) == 2                  # the song changed
    pages[wfmu.CURRENT] = pages[wfmu.CURRENT].replace("169197", "170000")
    pages["https://wfmu.org/playlists/shows/170000"] = PLAYLIST
    s.now_playing()
    assert asked[-1] == "https://wfmu.org/playlists/shows/170000"


def test_a_failed_playlist_keeps_the_live_song_and_is_tried_again(monkeypatch):
    pages = {wfmu.CURRENT: LIVE, PLAYLIST_URL: OSError("timed out")}
    asked = serve(monkeypatch, pages)
    np = stations.STATIONS["wfmu"].now_playing()
    assert (np.artist, np.show, np.album, np.recent) == (
        "Son Rompe Pera", "Marty McSorley's show", None, [])
    pages[PLAYLIST_URL] = PLAYLIST
    assert stations.STATIONS["wfmu"].now_playing().album == "Batuco"
    assert asked.count(PLAYLIST_URL) == 2


@pytest.mark.parametrize("live", [
    "",                                                     # off air: no song named
    LIVE.replace("/playlists/shows/169197", "/schedule"),   # no playlist linked
    OSError("wfmu.org unreachable"),
])
def test_off_air_no_playlist_or_down_falls_back_to_the_stream_title(monkeypatch, live):
    monkeypatch.setattr(stations.icy, "icy_title",
                        lambda url: '"Man From Mars" by Butch Paulson on Fool\'s Paradise on WFMU')
    asked = serve(monkeypatch, {wfmu.CURRENT: live})
    np = stations.STATIONS["wfmu"].now_playing()
    assert (np.artist, np.song, np.show) == ("Butch Paulson", "Man From Mars", "Fool's Paradise")
    assert np.recent == [] and asked == [wfmu.CURRENT]


def test_nothing_reads_the_playlist_rss_any_more(monkeypatch):
    # Every path above records what it fetched; this is the source itself.
    source = Path(wfmu.__file__).read_text()
    assert "playlistfeed" not in source and "latest published" not in source
    monkeypatch.setattr(stations.icy, "icy_title", lambda url: None)
    for live in (LIVE, "", OSError("down")):
        wfmu._cache.update(key=None, rows=[])
        asked = serve(monkeypatch, {wfmu.CURRENT: live, PLAYLIST_URL: PLAYLIST})
        np = stations.STATIONS["wfmu"].now_playing()
        assert not any("playlistfeed" in u for u in asked)
        assert "latest published" not in (np.show or "") + np.render()
