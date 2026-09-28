"""HTTP for the station fetchers: one place to set the agent and timeout,
and the one place tests patch (`net.get`, `net.get_json`)."""
from __future__ import annotations

import json
import urllib.request

USER_AGENT = "twiddle/0.1 (+https://github.com/jeromebanks/twiddle)"
TIMEOUT = 8


def get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT} | (headers or {}))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def get_json(url: str):
    return json.loads(get(url, {"Accept": "application/json"}))
