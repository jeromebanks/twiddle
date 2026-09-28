"""KQED: the stream's title is always blank, but kqed.org/radio/schedule
embeds the day's schedule as JSON (show, episode, start/end epochs), so the
slot covering "now" names the show."""
from __future__ import annotations

import json
import time

from .. import net
from ..model import NowPlaying, Station
from .icy import talk

SCHEDULE = "https://www.kqed.org/radio/schedule"
SCHEDULE_TTL = 15 * 60
_cache: dict = {"at": 0.0, "slots": []}


def parse_kqed_schedule(page: str) -> list[dict]:
    """The day's slots out of the schedule page's embedded state: dicts with
    programTitle, episodeTitle, episodeHost, startTime, endTime (epochs)."""
    i = page.find('"type": "radio-schedules"')
    j = page.find('"schedule":', i) if i >= 0 else -1
    if j < 0:
        return []
    slots, _ = json.JSONDecoder().raw_decode(page, page.index("[", j))
    return [x for x in slots if isinstance(x, dict) and x.get("startTime")]


def kqed_slot(slots: list[dict], now: float) -> dict | None:
    return next((x for x in slots if x["startTime"] <= now < (x.get("endTime") or 0)), None)


def kqed_row(slot: dict, on_now: bool) -> dict:
    row = {"time": time.strftime("%H:%M", time.localtime(slot["startTime"])),
           "show": slot.get("programTitle"),
           "episode": (slot.get("episodeTitle") or "").strip() or None,
           "host": (slot.get("episodeHost") or "").strip() or None}
    return {k: v for k, v in row.items() if v} | ({"on_now": True} if on_now else {})


def kqed(station: Station) -> NowPlaying:
    now = time.time()
    slot = kqed_slot(_cache["slots"], now)
    # One page covers the whole day, so re-fetch only when it is stale or
    # "now" has run past it (midnight).
    if slot is None or now - _cache["at"] > SCHEDULE_TTL:
        try:
            page = net.get(SCHEDULE, {"User-Agent": "Mozilla/5.0"}).decode("utf-8", "replace")
            _cache.update(at=now, slots=parse_kqed_schedule(page))
        except (OSError, ValueError):
            pass
        slot = kqed_slot(_cache["slots"], now)
    if slot is None:
        return talk(station)
    program = slot.get("programTitle") or None
    episode = (slot.get("episodeTitle") or "").strip() or None
    host = (slot.get("episodeHost") or "").strip()
    return NowPlaying(station.name, raw_title=episode or program,
                      show=program if episode else None, hosts=[host] if host else [],
                      note="(talk/news: from KQED's schedule)",
                      art_url=slot.get("imageSrc") or None,
                      schedule=[kqed_row(x, x is slot) for x in _cache["slots"]])
