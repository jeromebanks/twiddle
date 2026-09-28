"""Where the audio goes when you press play.

`Player` is the extension point: anything that can list places to play,
play a URI there, and say what is playing. `SpotifyConnectPlayer` is the
one that exists -- Spotify Connect to any device, including the relay that
feeds the Roams and this Mac's own speakers (`local.py`, for anyone without
the Roams). A Bandcamp or radio player would be another class.

## The relay experiment makes "play on the Mac" a write, too

A Spotify account plays on one Connect device at a time. While you are
listening through the relay (librespot -> Roams), playing a preview to this
Mac *takes Spotify away from the relay*, the Roams go silent, and
`logs/relay.jsonl` records silence-while-playing -- exactly the "librespot
starved" signature the experiment is looking for. So:

- `play` refuses (`NeedsConfirmation`) to take playback off an active relay
  unless asked twice, and
- when it does, it journals `spotify_transfer_from_relay` against the relay
  room's coordinator, so `analyse` discounts the silence that follows.
- `back_to_relay` hands playback back, also journalled.

Playing *to* the relay goes through `spotify_ops.ensure_room_on_relay`,
which only re-points the room if it isn't already on the relay.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .. import play, relay, spotify, spotify_ops, supervisor
from ..relay_cli import DEFAULT_CACHE, DEFAULT_DEVICE_NAME
from . import cache
from .local import LOCAL_LABEL, LocalSpeaker

RELAY_LABEL = "Roams (relay)"
HANDBACK_MAX_AGE_S = 6 * 3600   # older than this, "what was playing" is stale


class NeedsConfirmation(RuntimeError):
    """The action would interrupt something; ask, then call again confirmed."""


@dataclass
class Device:
    id: str
    name: str
    relay: bool = False
    active: bool = False
    type: str = ""
    local: bool = False     # this Mac's speakers, via the app's own librespot

    @property
    def label(self) -> str:
        if self.relay:
            return RELAY_LABEL + ("" if self.id else " -- not running, will start")
        if self.local:
            return LOCAL_LABEL + ("" if self.id else " -- will start")
        return self.name


def relay_is_set_up() -> bool:
    """Has this Mac ever run the relay? If not, there are no Roams to offer.

    A friend's Mac has no supervisor record and no relay sign-in, and picking
    "Roams (relay)" there would only fail after a slow speaker search.
    """
    return supervisor.status().record is not None or \
        Path(relay.credentials_path(str(DEFAULT_CACHE))).exists()


class Player(Protocol):
    def devices(self) -> list[Device]: ...
    def play(self, uris: list[str], device: Device, *, offset: int = 0,
             confirmed: bool = False, label: str = "") -> str: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def next(self) -> None: ...
    def now(self) -> dict: ...
    def back_to_relay(self) -> str: ...
    def close(self) -> None: ...


class SpotifyConnectPlayer:
    """Spotify Connect, with the relay as one of the devices.

    Every method may raise `spotify_ops.PlaybackError`; a UI shows its
    message and hint. Call from a worker thread: they all touch the network.
    """

    def __init__(self, session_factory: Callable[[], object] = spotify_ops.session,
                 room: str = "roam", relay_name: str | None = None,
                 dry_run: bool = False, notify: Callable[[str], None] = lambda m: None,
                 household_factory: Callable[[], object] | None = None,
                 local: LocalSpeaker | None = None,
                 relay_available: Callable[[], bool] = relay_is_set_up):
        self._session_factory = session_factory
        self._sess = None
        self._lock = threading.Lock()
        self.room = room
        self._relay_name = relay_name
        self.dry_run = dry_run
        self.notify = notify
        self._household_factory = household_factory
        self._group = None
        self.local = local
        self._relay_available = relay_available

    # -- plumbing ----------------------------------------------------------

    def session(self):
        with self._lock:
            if self._sess is None:
                self._sess = self._session_factory()
            return self._sess

    def _call(self, fn):
        try:
            return fn()
        except (spotify.ApiError, spotify.AuthError) as exc:
            raise spotify_ops.as_playback_error(exc) from exc

    @property
    def relay_name(self) -> str:
        """The relay's name in Spotify: as the running relay was started.

        Read from its own argv, like the room and port, so a relay started
        with a custom `--device-name` is still recognised -- otherwise the
        "you're about to silence the Roams" check would never fire.
        """
        return (self._relay_name or spotify_ops.relay_argv_value("--device-name")
                or DEFAULT_DEVICE_NAME)

    def _relay_room(self) -> str:
        """The room the running relay feeds -- from its own argv, else ours."""
        return spotify_ops.relay_argv_value("--room") or self.room

    def _relay_group(self):
        """The relay room's group (coordinator), resolved once: SSDP is slow."""
        if self._group is None:
            if self._household_factory is not None:
                house = self._household_factory()
            else:
                from ..household import Household
                house = Household.load()
            self._group = house.resolve(self._relay_room()).group
        return self._group

    def _journal(self, action: str, **extra) -> None:
        try:
            ip = self._relay_group().ip
        except Exception:        # an unreachable household must not block the journal
            ip = ""
        play._journal(action, ip, room=self._relay_room(), source="scene", **extra)

    # -- Player ------------------------------------------------------------

    def devices(self) -> list[Device]:
        try:
            raw = self._call(lambda: self.session().devices())
        except spotify_ops.PlaybackError as exc:
            # Not signed in to Spotify: a Bandcamp track still plays on this
            # Mac (ffplay), so offer it rather than no device at all. A
            # Spotify track picked there fails with the sign-in hint.
            if not (self.local and spotify_ops.not_signed_in(exc)):
                raise
            raw = []
        mine = self.local.find(raw) if self.local else None
        out = [Device(id=d.get("id", ""), name=d.get("name", ""),
                      relay=d.get("name", "").lower() == self.relay_name.lower(),
                      active=bool(d.get("is_active")), type=d.get("type", ""),
                      local=d is mine)
               for d in raw]
        if not any(d.relay for d in out) and self._relay_available():
            out.append(Device(id="", name=self.relay_name, relay=True))
        if self.local and mine is None:
            out.append(Device(id="", name=self.local.name, local=True))
        return sorted(out, key=lambda d: (not d.relay, not d.local, d.name.lower()))

    def now(self) -> dict:
        return self._call(lambda: spotify.now_playing(self.session().current()))

    def relay_is_live(self, np: dict | None = None) -> bool:
        """Is the relay the device Spotify is actively playing on?"""
        np = np if np is not None else self.now()
        return bool(np.get("playing")) and \
            np.get("device", "").lower() == self.relay_name.lower()

    def play(self, uris: list[str], device: Device, *, offset: int = 0,
             confirmed: bool = False, label: str = "") -> str:
        """Play `uris` from `offset`, so `next` walks the rest of the list."""
        sess = self.session()
        uri = uris[offset] if uris else ""
        what = label or uri
        raw = self._call(sess.current)
        np = spotify.now_playing(raw)
        taking_from_relay = not device.relay and self.relay_is_live(np)
        # One account, one device: playing here stops whatever is playing
        # elsewhere -- the relay, or a podcast on your phone. Ask first.
        interrupting = bool(np.get("playing")) and np.get("device", "") \
            and np.get("device", "").lower() != device.name.lower() \
            and not (device.relay and self.relay_is_live(np))
        if interrupting and not confirmed:
            if taking_from_relay:
                raise NeedsConfirmation(
                    f"This takes Spotify away from the {RELAY_LABEL} -- they'll go "
                    f"silent. Press p again to play on {device.name} anyway.")
            what_else = np.get("track") or "what's playing"
            raise NeedsConfirmation(
                f"This stops {what_else} on {np.get('device')}. "
                f"Press p again to play on {device.label} anyway.")

        dev = {"id": device.id, "name": device.name}
        if device.relay:
            group = self._relay_group()
            dev = self._call(lambda: spotify_ops.relay_device(
                sess, device=None, device_name=self.relay_name, room=group.name,
                cache=str(DEFAULT_CACHE), notify=self.notify)) if not self.dry_run \
                else (dev if device.id else {"id": "", "name": self.relay_name})
            spotify_ops.ensure_room_on_relay(group, self.dry_run, notify=self.notify)
        elif device.local and not self.dry_run:
            if self.local is None:
                raise spotify_ops.PlaybackError("no local player configured")
            # Resolved by name every time: the id changes whenever librespot
            # restarts, so a remembered one is worthless.
            dev = self._call(lambda: self.local.start(sess, notify=self.notify))

        if self.dry_run:
            return f"[dry-run] would play {what} on {device.label}"

        if taking_from_relay:
            # Remember what the Roams were playing, so `R` can put it back
            # rather than carry on with the preview.
            raw = raw or {}
            cache.set_state("handback", {
                "context_uri": (raw.get("context") or {}).get("uri", ""),
                "item_uri": (raw.get("item") or {}).get("uri", ""),
                "progress_ms": raw.get("progress_ms") or 0,
                "track": np.get("track", ""), "at": time.time()})
            self._journal("spotify_transfer_from_relay", to_device=device.name, uri=uri,
                          was_playing=np.get("uri", ""))

        def go():
            sess.play(device_id=dev.get("id", ""), uris=uris, offset=offset)
        raced = self._call(lambda: spotify_ops.play_and_check(sess, dev, go))
        shown = (raced or {}).get("track") or what
        return f"▶ {shown}  on {device.label}"

    def back_to_relay(self) -> str:
        """Put the Roams back on what they were playing before a preview.

        If `play` took Spotify off the relay, it saved the context, track
        and position; replay exactly that on the relay. Otherwise (nothing
        saved, or saved too long ago to mean anything) just move whatever
        Spotify has now onto the relay.
        """
        sess = self.session()
        relay = next((d for d in self.devices() if d.relay and d.id), None)
        if relay is None:
            raise spotify_ops.PlaybackError(
                "the relay isn't registered with Spotify",
                "is it running? `twiddle relay status`")
        hb = cache.get_state("handback") or {}
        if time.time() - hb.get("at", 0) > HANDBACK_MAX_AGE_S:
            hb = {}
        # A Bandcamp track took the Roam itself off the relay's stream:
        # Spotify resumed on the relay would play to nobody. A no-op (one
        # read) when the room is already on it.
        spotify_ops.ensure_room_on_relay(self._relay_group(), self.dry_run,
                                         notify=self.notify)
        if self.dry_run:
            return (f"[dry-run] would put {hb.get('track') or 'playback'} back on "
                    f"{RELAY_LABEL}")
        self._journal("spotify_transfer_to_relay", restoring=hb.get("item_uri", ""))
        dev = {"id": relay.id, "name": relay.name}
        if hb.get("context_uri"):
            def go():
                sess.play(hb["context_uri"], device_id=relay.id,
                          position_ms=hb.get("progress_ms", 0),
                          offset_uri=hb.get("item_uri", ""))
        elif hb.get("item_uri"):
            def go():
                sess.play(device_id=relay.id, uris=[hb["item_uri"]],
                          position_ms=hb.get("progress_ms", 0))
        else:
            def go():
                sess.transfer(relay.id, play=True)
        self._call(lambda: spotify_ops.play_and_check(sess, dev, go))
        cache.set_state("handback", None)
        return f"▶ {hb.get('track') or 'playback'} back on {RELAY_LABEL}"

    def close(self) -> None:
        if self.local:
            self.local.stop()

    def pause(self) -> None:
        if not self.dry_run:
            self._call(lambda: self.session().pause())

    def _relay_room_listening(self) -> None:
        """Before Spotify sounds on the relay again: is the Roam still
        reading it? A Bandcamp track from `scene`, or a station from `dial`,
        takes the room itself elsewhere, and resuming Spotify then is
        silence with every component reporting PLAYING. One read when the
        room is where it should be."""
        if self.now().get("device", "").lower() == self.relay_name.lower():
            spotify_ops.ensure_room_on_relay(self._relay_group(), self.dry_run,
                                             notify=self.notify)

    def resume(self) -> None:
        self._relay_room_listening()
        if not self.dry_run:
            self._call(lambda: self.session().play())

    def next(self) -> None:
        self._relay_room_listening()
        if not self.dry_run:
            self._call(lambda: self.session().next_track())
