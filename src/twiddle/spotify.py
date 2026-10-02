"""Spotify Web API: the half of the system that *chooses* what plays.

`relay.py` carries audio. This carries intent. They meet at one point: the
relay runs librespot, which registers with Spotify as a playback device, and
this module tells Spotify to play to that device. Neither knows much about
the other, which is deliberate -- a different decoder or a different source
would leave this untouched.

The split matters for what comes next. An agent, a TUI and the CLI all want
the same seven verbs (search, play, pause, skip, queue, what's on, where is
it going), so those live here as plain functions over a `Session`, and every
caller composes them. Only `spotify play --room` has a side effect beyond
the API, and that is stated where it happens rather than hidden in here.

Authentication is PKCE with a refresh token cached on disk. A password is
never seen, asked for, or stored -- the one browser round-trip mints a
refresh token and every later call uses it unattended, which is exactly what
makes agent control possible.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

from . import netstats

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"

# Spotify control needs your own Spotify app (free, two minutes): its client
# id in TWIDDLE_SPOTIFY_CLIENT_ID or saved by `spotify auth --client-id`, and
# this redirect URI (or TWIDDLE_SPOTIFY_REDIRECT) registered as its callback.
# There is deliberately no built-in client id to fall back on.
DEFAULT_REDIRECT = "http://127.0.0.1:5588/login"

SCOPES = [
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "streaming",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-top-read",
    "user-read-recently-played",
]

TOKEN_PATH = Path(os.environ.get(
    "TWIDDLE_SPOTIFY_TOKENS",
    str(Path.home() / ".cache" / "twiddle" / "spotify" / "tokens.json")))

# Refresh a little early. A token that expires between the check and the call
# is a 401 for something the user asked for, and the retry is cheap to avoid.
EXPIRY_SKEW = 60.0


class AuthError(RuntimeError):
    """No usable credentials. The fix is always `spotify auth`."""


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, body: str = "",
                 retry_after: float = 0.0):
        detail = f"{status}: {message}"
        if retry_after:
            detail += f" (retry after {retry_after:.0f}s)"
        super().__init__(detail)
        self.status, self.message, self.body = status, message, body
        self.retry_after = retry_after


CONFIG_PATH = TOKEN_PATH.parent / "config.json"


def _config(path: Path | None = None) -> dict:
    # Resolved at call time, not bound as a default: a default argument is
    # evaluated once at import, which would freeze the path for the life of
    # the process and make the location impossible to redirect.
    path = path or CONFIG_PATH
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(client_id_: str = "", redirect: str = "",
                path: Path | None = None) -> dict:
    """Persist your own Spotify app's details, so no env var is needed."""
    path = path or CONFIG_PATH
    cfg = _config(path)
    if client_id_:
        cfg["client_id"] = client_id_
    if redirect:
        cfg["redirect_uri"] = redirect
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    return cfg


NO_CLIENT_ID = (
    "Spotify needs your own Spotify app's client id (free, two minutes):\n"
    "  1. create an app at https://developer.spotify.com/dashboard\n"
    f"  2. add the redirect URI {DEFAULT_REDIRECT}\n"
    "  3. run: uv run twiddle spotify auth --client-id <YOUR_CLIENT_ID>")


def client_id() -> str:
    """Env, then saved config. Raises AuthError, saying how to get one,
    when neither is set."""
    cid = os.environ.get("TWIDDLE_SPOTIFY_CLIENT_ID") or _config().get("client_id")
    if not cid:
        raise AuthError(NO_CLIENT_ID)
    return cid


def redirect_uri() -> str:
    return (os.environ.get("TWIDDLE_SPOTIFY_REDIRECT")
            or _config().get("redirect_uri") or DEFAULT_REDIRECT)


# ---- token storage ---------------------------------------------------------


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    # What Spotify actually granted, which is not always what was asked for:
    # authorizing against an existing consent can hand back that consent's
    # scope set instead. Recording it is what turns "every call fails" into
    # a one-line diagnosis.
    scope: str = ""

    @property
    def stale(self) -> bool:
        return time.time() >= self.expires_at - EXPIRY_SKEW

    def to_dict(self) -> dict:
        return {"access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "scope": self.scope}

    def missing_scopes(self, needed=("user-modify-playback-state",
                                     "user-read-playback-state")) -> list[str]:
        granted = set(self.scope.split())
        return [s for s in needed if s not in granted] if self.scope else []

    @classmethod
    def from_response(cls, data: dict, previous: str = "") -> "Tokens":
        # A refresh response may omit refresh_token, meaning "keep using the
        # one you have". Dropping it there would silently downgrade the
        # install to one that needs a browser again in an hour.
        return cls(access_token=data["access_token"],
                   refresh_token=data.get("refresh_token") or previous,
                   expires_at=time.time() + float(data.get("expires_in", 3600)),
                   scope=data.get("scope", ""))

    def save(self, path: Path = TOKEN_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        # A refresh token is a long-lived credential for a paid account.
        path.chmod(0o600)

    @classmethod
    def load(cls, path: Path = TOKEN_PATH) -> "Tokens":
        if not path.exists():
            raise AuthError(f"no Spotify tokens at {path}")
        try:
            d = json.loads(path.read_text())
            return cls(d["access_token"], d["refresh_token"], d["expires_at"],
                       d.get("scope", ""))
        except (json.JSONDecodeError, KeyError) as exc:
            raise AuthError(f"tokens at {path} are unreadable: {exc}") from exc


def authorized(path: Path = TOKEN_PATH) -> bool:
    return path.exists()


# ---- the PKCE dance --------------------------------------------------------


def _verifier_and_challenge() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(challenge: str, state: str) -> str:
    return ACCOUNTS + "/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "scope": " ".join(SCOPES),
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    })


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def log_message(self, *_args):
        pass

    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        type(self).result.update(params)
        ok = "code" in params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = ("<h2>Authorized.</h2><p>You can close this tab and go back to "
                "the terminal.</p>" if ok else
                f"<h2>Authorization failed.</h2><pre>{params}</pre>")
        self.wfile.write(f"<html><body style='font-family:system-ui;padding:3em'>"
                         f"{body}</body></html>".encode())


def run_pkce_flow(timeout: float = 300, open_browser: bool = True) -> Tokens:
    """One browser round-trip, exchanged for a refresh token that outlives it.

    Returns tokens; the caller saves them. Raises AuthError with whatever
    Spotify said if the user declines or the exchange is rejected.
    """
    verifier, challenge = _verifier_and_challenge()
    state = secrets.token_urlsafe(16)
    parsed = urllib.parse.urlparse(redirect_uri())
    _CallbackHandler.result = {}
    server = HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 80),
                        _CallbackHandler)
    server.timeout = 1.0
    url = authorize_url(challenge, state)
    # flush=True, and stderr: this URL is the whole point of the command and
    # it has to appear *before* the process blocks. Block buffering to a file
    # or a pipe would hold it until exit, which is to say until after the
    # thing it is asking for has timed out.
    print("Browse to this URL and approve access:\n", file=sys.stderr, flush=True)
    print(f"  {url}\n", file=sys.stderr, flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and "code" not in _CallbackHandler.result:
            if "error" in _CallbackHandler.result:
                raise AuthError("Spotify refused: "
                                + _CallbackHandler.result["error"])
            server.handle_request()
    finally:
        server.server_close()
    got = _CallbackHandler.result
    if "code" not in got:
        raise AuthError("timed out waiting for the browser step")
    if got.get("state") != state:
        # Not paranoia theatre: a mismatched state means the code came back
        # from a flow this process did not start, so it must not be redeemed.
        raise AuthError("state mismatch; start `spotify auth` again")
    resp = requests.post(ACCOUNTS + "/api/token", timeout=30, data={
        "grant_type": "authorization_code",
        "code": got["code"],
        "redirect_uri": redirect_uri(),
        "client_id": client_id(),
        "code_verifier": verifier,
    })
    if resp.status_code != 200:
        raise AuthError(f"token exchange failed ({resp.status_code}): {resp.text}")
    return Tokens.from_response(resp.json())


def refresh(tokens: Tokens) -> Tokens:
    resp = requests.post(ACCOUNTS + "/api/token", timeout=30, data={
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": client_id(),
    })
    if resp.status_code != 200:
        raise AuthError(f"refresh failed ({resp.status_code}): {resp.text}; "
                        "run `spotify auth` again")
    return Tokens.from_response(resp.json(), previous=tokens.refresh_token)


# ---- the session -----------------------------------------------------------


@dataclass
class Session:
    """An authenticated Web API client that keeps its own token fresh."""

    tokens: Tokens
    path: Path = TOKEN_PATH

    @classmethod
    def load(cls, path: Path = TOKEN_PATH) -> "Session":
        return cls(Tokens.load(path), path)

    def _ensure_fresh(self) -> None:
        if self.tokens.stale:
            self.tokens = refresh(self.tokens)
            self.tokens.save(self.path)

    def request(self, method: str, path: str, **kw) -> dict | None:
        """One call, with exactly one retry after a refresh on 401.

        Exactly one: a revoked refresh token would otherwise loop forever,
        and the honest outcome there is an AuthError telling the user to
        re-run `spotify auth`.
        """
        self._ensure_fresh()
        for attempt in (0, 1):
            url = path if path.startswith("http") else API + path
            resp = requests.request(
                method, url, timeout=30,
                headers={"Authorization": f"Bearer {self.tokens.access_token}"},
                **kw)
            netstats.record_response("spotify", resp)
            if resp.status_code == 401 and attempt == 0:
                self.tokens = refresh(self.tokens)
                self.tokens.save(self.path)
                continue
            return self._decode(resp)
        return None

    @staticmethod
    def _decode(resp) -> dict | None:
        # Spotify answers 429 with Retry-After in seconds. Surfacing it turns
        # "try again later" into a number the caller can actually wait for.
        retry_after = 0.0
        if resp.status_code == 429:
            try:
                retry_after = float(resp.headers.get("Retry-After", 0))
            except (TypeError, ValueError):
                retry_after = 0.0
        if resp.status_code == 204 or not resp.content:
            # The transport endpoints answer 204 with an empty body on
            # success, which is not an error and is not JSON either.
            return None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            msg = (body.get("error", {}).get("message")
                   if isinstance(body.get("error"), dict) else body.get("error"))
            raise ApiError(resp.status_code, msg or resp.reason,
                           resp.text[:400], retry_after)
        return body

    # -- reads --

    def devices(self) -> list[dict]:
        return (self.request("GET", "/me/player/devices") or {}).get("devices", [])

    def current(self) -> dict | None:
        return self.request("GET", "/me/player")

    def search(self, query: str, kind: str = "track", limit: int = 10) -> list[dict]:
        data = self.request("GET", "/search", params={
            "q": query, "type": kind, "limit": limit}) or {}
        return data.get(kind + "s", {}).get("items", []) or []

    # -- writes --

    def transfer(self, device_id: str, play: bool = False) -> None:
        self.request("PUT", "/me/player",
                     json={"device_ids": [device_id], "play": play})

    def play(self, uri: str = "", device_id: str = "", position_ms: int = 0,
             *, uris: list[str] | None = None, offset: int = 0,
             offset_uri: str = "") -> None:
        """Start playback. A context URI and a track URI go in different fields.

        Spotify rejects a playlist or album passed as `uris`, and rejects a
        track passed as `context_uri`, with the same unhelpful 400 either way.

        `uris`+`offset` play a whole list starting partway through it, so a
        caller sampling several tracks can `next`/`previous` through the rest
        instead of playback stopping after one track.
        """
        params = {"device_id": device_id} if device_id else {}
        body: dict = {}
        if uris:
            body["uris"] = uris
            if offset:
                body["offset"] = {"position": offset}
            if position_ms:
                body["position_ms"] = position_ms
        elif uri:
            if any(k in uri for k in (":album:", ":playlist:", ":artist:")):
                body["context_uri"] = uri
                if offset_uri:
                    # Resume a context at a given track: "back to where the
                    # album was", not "back to its first track".
                    body["offset"] = {"uri": offset_uri}
            else:
                body["uris"] = [uri]
            if position_ms:
                body["position_ms"] = position_ms
        self.request("PUT", "/me/player/play", params=params, json=body)

    def pause(self) -> None:
        self.request("PUT", "/me/player/pause")

    def next_track(self) -> None:
        self.request("POST", "/me/player/next")

    def previous_track(self) -> None:
        self.request("POST", "/me/player/previous")

    def queue(self, uri: str, device_id: str = "") -> None:
        params = {"uri": uri}
        if device_id:
            params["device_id"] = device_id
        self.request("POST", "/me/player/queue", params=params)

    def set_volume(self, percent: int) -> None:
        """librespot's own software volume, not the speaker's.

        Leave this at 100 and control loudness with `twiddle volume`, or the
        two attenuations multiply and quiet audio arrives at the speaker with
        the noise floor raised.
        """
        self.request("PUT", "/me/player/volume",
                     params={"volume_percent": max(0, min(100, int(percent)))})


# ---- helpers shared by the CLI and anything built on it --------------------


def describe(item: dict) -> str:
    """One line for a track, album, artist or playlist search hit."""
    if not item:
        return ""
    kind = item.get("type", "")
    name = item.get("name", "?")
    if kind == "track":
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        album = item.get("album", {}).get("name", "")
        return f"{name} - {artists}" + (f"  [{album}]" if album else "")
    if kind == "album":
        return f"{name} - " + ", ".join(a["name"] for a in item.get("artists", []))
    if kind == "playlist":
        owner = item.get("owner", {}).get("display_name", "")
        return f"{name}" + (f"  (by {owner})" if owner else "")
    return name


def now_playing(state: dict | None) -> dict:
    """Flatten /me/player into the handful of fields anything actually wants."""
    if not state:
        return {"playing": False}
    item = state.get("item") or {}
    dev = state.get("device") or {}
    return {
        "playing": bool(state.get("is_playing")),
        "track": item.get("name", ""),
        "artists": [a["name"] for a in item.get("artists", [])],
        "album": item.get("album", {}).get("name", ""),
        # Largest first, per the Web API; "" for an episode or local file.
        "art": ((item.get("album") or {}).get("images") or [{}])[0].get("url", ""),
        "uri": item.get("uri", ""),
        "progress_ms": state.get("progress_ms"),
        "duration_ms": item.get("duration_ms"),
        "device": dev.get("name", ""),
        "device_id": dev.get("id", ""),
        "volume_percent": dev.get("volume_percent"),
    }


def find_device(devices: list[dict], name: str) -> dict | None:
    """Match a device by name, case-insensitively and by prefix.

    Exact first: two devices whose names share a prefix would otherwise be
    resolved by list order, which is not stable across calls.
    """
    lowered = name.lower()
    for d in devices:
        if d.get("name", "").lower() == lowered:
            return d
    matches = [d for d in devices if lowered in d.get("name", "").lower()]
    return matches[0] if len(matches) == 1 else None
