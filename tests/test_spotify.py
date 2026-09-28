"""The Web API client's contract, especially the parts that rot silently.

Token refresh is the one that matters most: it works perfectly for an hour
and then every call 401s, long after any test has passed. So the refresh
path, the retry bound, and the "keep the old refresh token" rule are all
pinned here rather than discovered in production at 2am.
"""
import json
import time

import pytest

from twiddle import spotify


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.reason = "reason"
        self.text = text or (json.dumps(body) if body is not None else "")
        self.content = self.text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


# ---- tokens ----------------------------------------------------------------


def test_refresh_response_without_a_refresh_token_keeps_the_old_one():
    """Spotify omits refresh_token on refresh, meaning "keep using yours".

    Dropping it would silently downgrade the install to one that needs a
    browser again at the next expiry -- which is exactly the unattended
    operation this whole layer exists to provide.
    """
    t = spotify.Tokens.from_response(
        {"access_token": "new", "expires_in": 3600}, previous="keep-me")
    assert t.refresh_token == "keep-me"
    assert t.access_token == "new"


def test_refresh_response_with_a_new_refresh_token_adopts_it():
    t = spotify.Tokens.from_response(
        {"access_token": "a", "refresh_token": "rotated", "expires_in": 10},
        previous="old")
    assert t.refresh_token == "rotated"


def test_a_token_is_stale_before_it_actually_expires():
    """Refreshing early avoids a 401 on something the user just asked for."""
    t = spotify.Tokens("a", "r", time.time() + spotify.EXPIRY_SKEW / 2)
    assert t.stale
    assert not spotify.Tokens("a", "r", time.time() + 3600).stale


def test_tokens_are_written_private(tmp_path):
    """A refresh token is a long-lived credential for a paid account."""
    path = tmp_path / "t.json"
    spotify.Tokens("a", "r", time.time() + 60).save(path)
    assert path.stat().st_mode & 0o077 == 0, "tokens are group/world readable"


def test_missing_tokens_say_what_to_run(tmp_path):
    with pytest.raises(spotify.AuthError):
        spotify.Tokens.load(tmp_path / "nope.json")


def test_corrupt_tokens_are_an_auth_error_not_a_crash(tmp_path):
    path = tmp_path / "t.json"
    path.write_text("{not json")
    with pytest.raises(spotify.AuthError):
        spotify.Tokens.load(path)


# ---- the session -----------------------------------------------------------


def _session(tmp_path, expires_in=3600):
    path = tmp_path / "t.json"
    toks = spotify.Tokens("access-1", "refresh-1", time.time() + expires_in)
    toks.save(path)
    return spotify.Session(toks, path)


def test_a_401_refreshes_once_and_retries_once(tmp_path, monkeypatch):
    """Exactly once. An unbounded retry on a revoked token loops forever."""
    sess = _session(tmp_path)
    calls = []

    def fake_request(method, url, **kw):
        calls.append(kw["headers"]["Authorization"])
        return FakeResponse(401) if len(calls) == 1 else FakeResponse(200, {"ok": 1})

    refreshes = []

    def fake_refresh(tokens):
        refreshes.append(tokens.refresh_token)
        return spotify.Tokens("access-2", "refresh-1", time.time() + 3600)

    monkeypatch.setattr(spotify.requests, "request", fake_request)
    monkeypatch.setattr(spotify, "refresh", fake_refresh)
    assert sess.request("GET", "/me") == {"ok": 1}
    assert len(calls) == 2, "should have retried exactly once"
    assert calls[0] == "Bearer access-1"
    assert calls[1] == "Bearer access-2", "retry used the stale token"
    assert refreshes == ["refresh-1"]


def test_a_second_401_is_not_retried_again(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    calls = []

    def always_401(method, url, **kw):
        calls.append(1)
        return FakeResponse(401, {"error": {"message": "revoked"}})

    monkeypatch.setattr(spotify.requests, "request", always_401)
    monkeypatch.setattr(spotify, "refresh",
                        lambda t: spotify.Tokens("a2", "r", time.time() + 3600))
    with pytest.raises(spotify.ApiError):
        sess.request("GET", "/me")
    assert len(calls) == 2, "retried more than once"


def test_a_stale_token_is_refreshed_before_the_call(tmp_path, monkeypatch):
    sess = _session(tmp_path, expires_in=1)   # already inside the skew
    monkeypatch.setattr(spotify, "refresh",
                        lambda t: spotify.Tokens("fresh", "r", time.time() + 3600))
    seen = {}

    def fake_request(method, url, **kw):
        seen["auth"] = kw["headers"]["Authorization"]
        return FakeResponse(200, {})

    monkeypatch.setattr(spotify.requests, "request", fake_request)
    sess.request("GET", "/me")
    assert seen["auth"] == "Bearer fresh"
    # and it persisted, so the next process does not refresh again
    assert json.loads((tmp_path / "t.json").read_text())["access_token"] == "fresh"


def test_204_is_success_not_a_parse_error(tmp_path, monkeypatch):
    """The transport endpoints answer 204 with an empty body."""
    sess = _session(tmp_path)
    monkeypatch.setattr(spotify.requests, "request",
                        lambda *a, **kw: FakeResponse(204))
    assert sess.request("PUT", "/me/player/pause") is None


def test_429_carries_the_retry_after_seconds(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    monkeypatch.setattr(
        spotify.requests, "request",
        lambda *a, **kw: FakeResponse(429, {"error": {"message": "slow down"}},
                                      headers={"Retry-After": "35"}))
    with pytest.raises(spotify.ApiError) as exc:
        sess.request("GET", "/me")
    assert exc.value.retry_after == 35
    assert "35" in str(exc.value)


# ---- playback request shapes ------------------------------------------------


@pytest.mark.parametrize("uri,field", [
    ("spotify:track:abc", "uris"),
    ("spotify:album:abc", "context_uri"),
    ("spotify:playlist:abc", "context_uri"),
    ("spotify:artist:abc", "context_uri"),
])
def test_context_and_track_uris_go_in_different_fields(uri, field, tmp_path,
                                                       monkeypatch):
    """Spotify 400s either way round, with the same unhelpful message."""
    sess = _session(tmp_path)
    sent = {}

    def fake_request(method, url, **kw):
        sent.update(kw.get("json") or {})
        return FakeResponse(204)

    monkeypatch.setattr(spotify.requests, "request", fake_request)
    sess.play(uri, device_id="dev")
    assert field in sent
    if field == "uris":
        assert sent["uris"] == [uri]
    else:
        assert sent["context_uri"] == uri


def test_play_with_no_uri_sends_an_empty_body_to_resume(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    sent = {}
    monkeypatch.setattr(spotify.requests, "request",
                        lambda m, u, **kw: sent.update(body=kw.get("json"))
                        or FakeResponse(204))
    sess.play(device_id="dev")
    assert sent["body"] == {}


def test_play_with_a_uri_list_sends_uris_and_offset(tmp_path, monkeypatch):
    """Sampling several tracks needs a real context, or `next` has nothing to
    skip to -- a single-`uri` play stops dead when that one track ends."""
    sess = _session(tmp_path)
    sent = {}
    monkeypatch.setattr(spotify.requests, "request",
                        lambda m, u, **kw: sent.update(body=kw.get("json"))
                        or FakeResponse(204))
    uris = ["spotify:track:a", "spotify:track:b", "spotify:track:c"]
    sess.play(uris=uris, offset=2, device_id="dev")
    assert sent["body"]["uris"] == uris
    assert sent["body"]["offset"] == {"position": 2}


def test_play_with_a_uri_list_and_no_offset_omits_it(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    sent = {}
    monkeypatch.setattr(spotify.requests, "request",
                        lambda m, u, **kw: sent.update(body=kw.get("json"))
                        or FakeResponse(204))
    sess.play(uris=["spotify:track:a"], device_id="dev")
    assert "offset" not in sent["body"]


# ---- helpers ---------------------------------------------------------------


def test_find_device_prefers_an_exact_name_over_a_prefix():
    """Otherwise the winner depends on list order, which is not stable."""
    devices = [{"name": "Sonos Roam Relay 2"}, {"name": "Sonos Roam Relay"}]
    assert spotify.find_device(devices, "Sonos Roam Relay")["name"] == \
        "Sonos Roam Relay"


def test_find_device_refuses_an_ambiguous_substring():
    devices = [{"name": "Relay One"}, {"name": "Relay Two"}]
    assert spotify.find_device(devices, "relay") is None


def test_find_device_is_case_insensitive():
    assert spotify.find_device([{"name": "Kitchen"}], "kitchen")


def test_now_playing_survives_an_empty_player_state():
    """Spotify answers /me/player with 204 when nothing is active."""
    assert spotify.now_playing(None) == {"playing": False}


def test_now_playing_flattens_the_fields_a_ui_wants():
    np = spotify.now_playing({
        "is_playing": True, "progress_ms": 1000,
        "device": {"name": "Sonos Roam Relay", "id": "d1", "volume_percent": 80},
        "item": {"name": "So What", "duration_ms": 545000,
                 "uri": "spotify:track:x",
                 "artists": [{"name": "Miles Davis"}],
                 "album": {"name": "Kind of Blue"}}})
    assert np["track"] == "So What"
    assert np["artists"] == ["Miles Davis"]
    assert np["device"] == "Sonos Roam Relay"
    assert np["playing"] is True


def test_describe_renders_a_track_with_its_artists():
    line = spotify.describe({"type": "track", "name": "So What",
                             "artists": [{"name": "Miles Davis"}],
                             "album": {"name": "Kind of Blue"}})
    assert "So What" in line and "Miles Davis" in line and "Kind of Blue" in line


def test_the_pkce_challenge_verifies_against_its_verifier():
    import base64
    import hashlib

    verifier, challenge = spotify._verifier_and_challenge()
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expect
    assert "=" not in challenge, "padding must be stripped for PKCE"


def test_the_authorize_url_asks_for_playback_control(monkeypatch):
    monkeypatch.setenv("TWIDDLE_SPOTIFY_CLIENT_ID", "my-own-app")
    url = spotify.authorize_url("chal", "state")
    assert "user-modify-playback-state" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=my-own-app" in url


def test_client_id_and_redirect_are_overridable(monkeypatch):
    """So you can point this at your own Spotify app instead of librespot's."""
    monkeypatch.setenv("TWIDDLE_SPOTIFY_CLIENT_ID", "mine")
    monkeypatch.setenv("TWIDDLE_SPOTIFY_REDIRECT", "http://127.0.0.1:9/cb")
    assert spotify.client_id() == "mine"
    assert spotify.redirect_uri() == "http://127.0.0.1:9/cb"


# ---- bring your own Spotify app --------------------------------------------


def test_without_your_own_app_it_says_how_to_make_one(tmp_path, monkeypatch):
    """There is no borrowed client id to fall back on: your own Spotify app,
    saved once, with no env var needed after."""
    monkeypatch.delenv("TWIDDLE_SPOTIFY_CLIENT_ID", raising=False)
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(spotify, "CONFIG_PATH", cfg)
    with pytest.raises(spotify.AuthError, match="developer.spotify.com"):
        spotify.client_id()
    spotify.save_config("my-own-app", path=cfg)
    assert spotify.client_id() == "my-own-app"


def test_the_environment_still_wins_over_saved_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(spotify, "CONFIG_PATH", cfg)
    spotify.save_config("from-file", path=cfg)
    monkeypatch.setenv("TWIDDLE_SPOTIFY_CLIENT_ID", "from-env")
    assert spotify.client_id() == "from-env"


def test_saving_a_redirect_does_not_clobber_the_client_id(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(spotify, "CONFIG_PATH", cfg)
    spotify.save_config("keep-me", path=cfg)
    spotify.save_config(redirect="http://127.0.0.1:5588/login", path=cfg)
    assert json.loads(cfg.read_text())["client_id"] == "keep-me"


def test_a_corrupt_config_falls_back_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.delenv("TWIDDLE_SPOTIFY_CLIENT_ID", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text("{ broken")
    monkeypatch.setattr(spotify, "CONFIG_PATH", cfg)
    with pytest.raises(spotify.AuthError):
        spotify.client_id()


def test_the_granted_scope_is_recorded_not_discarded():
    """Spotify can grant a different scope set than the one requested.

    Authorizing against a consent that already exists for the same client id
    can hand back *that* consent's scopes. Without recording what arrived,
    the symptom is every playback call failing much later, with nothing on
    disk to explain why.
    """
    t = spotify.Tokens.from_response({
        "access_token": "a", "refresh_token": "r", "expires_in": 3600,
        "scope": "user-read-private playlist-modify"})
    assert "playlist-modify" in t.scope
    assert t.missing_scopes() == ["user-modify-playback-state",
                                  "user-read-playback-state"]


def test_a_token_with_playback_scopes_reports_nothing_missing():
    t = spotify.Tokens.from_response({
        "access_token": "a", "refresh_token": "r", "expires_in": 3600,
        "scope": "user-modify-playback-state user-read-playback-state"})
    assert t.missing_scopes() == []


def test_an_unknown_scope_set_is_not_treated_as_missing():
    """Older token files have no scope field; that is not a failure."""
    assert spotify.Tokens("a", "r", 0).missing_scopes() == []


def test_scope_round_trips_through_disk(tmp_path):
    path = tmp_path / "t.json"
    spotify.Tokens("a", "r", time.time() + 60, scope="streaming").save(path)
    assert spotify.Tokens.load(path).scope == "streaming"
