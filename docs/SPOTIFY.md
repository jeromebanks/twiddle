# Playing Spotify without going through Sonos's cloud

**Status: built.** `twiddle relay` implements this. Two steps of the original
plan below were replaced during implementation and are marked where they
appear — see *What changed from the plan*.

**Goal:** listen to Spotify on a pair of Sonos Roams without Sonos-native
Spotify integration or Spotify Connect — both of which hand off through
Sonos's cloud. In the household this was built for, the Spotify Connect
handoff was the single worst measured trigger for dropouts: it concentrated
69% of one night's failures into 2% of the monitored time.

**Why this should actually help, not just avoid the cloud on principle:**
the one playback path already proven reliable in this repo is
`twiddle serve` / `twiddle stream` — a plain HTTP pull from this Mac.
The relay gets Spotify audio onto that same proven path instead of through
Sonos's Spotify integration or Connect.

## Use it

```bash
uv run twiddle spotify auth --client-id <YOUR_ID>   # once, in a browser
uv run twiddle spotify play kind of blue --room roam
```

That one `play` starts a relay if none is running, finds it in Spotify's
device list, and plays. Everything else composes from there:

```bash
uv run twiddle spotify search "miles davis" --type album
uv run twiddle spotify now
uv run twiddle spotify pause | next | prev
uv run twiddle spotify queue "so what"
uv run twiddle spotify discover          # interactive: search, sample, play
uv run twiddle relay up --room roam      # detached; survives your terminal
uv run twiddle relay down                # stops it and restores the room
```

Every command except `discover` takes `--json` and answers the repo's
`ok`/`error` envelope, which is what makes this drivable by an agent or a
TUI rather than only by a person — `discover_cli.py` is that TUI, built on
top of exactly this API layer, and is the one command that prompts on
purpose. See `discover_cli.py`'s own module docstring and the read-only vs
writing table in `CLAUDE.md`.

## Two halves, meeting at one point

`relay.py` carries **audio**. `spotify.py` carries **intent**. They meet only
where librespot registers with Spotify as a playback device and the Web API
is told to play to that device. Neither knows much about the other, so a
different decoder or a different source leaves the control layer untouched --
which is the point, since a personal TUI is just a third caller of the same
verbs. `discover_cli.py` is that TUI, now built: it calls `spotify_cli.py`'s
own session/search/play/relay helpers rather than duplicating any of them.

| Layer | What it is |
|---|---|
| `relay.py` | source -> paced PCM -> MP3 -> HTTP fan-out |
| `supervisor.py` | keeps a relay alive with no terminal attached |
| `spotify.py` | Web API: search, devices, transport |
| `spotify_cli.py` | the verbs an agent or TUI calls |
| `discover_cli.py` | `spotify discover` -- the interactive shell built on those verbs |

## You need your own Spotify app

**This is not optional, and it is the one thing that will bite you.** The
fallback client id is librespot's, which is shared by every librespot user in
the world, and Spotify meters the Web API **per client id, not per account**.
Using it earns `429 API rate limit exceeded` on essentially every call, with
a `Retry-After` that grows each time you retry. Waiting does not help; it is
not your quota being spent.

Two minutes to fix:

1. https://developer.spotify.com/dashboard -> **Create app**
2. Any name. Under *Which API/SDKs are you planning to use?* tick **Web API**.
3. Add redirect URI exactly: `http://127.0.0.1:5588/login`
4. Copy the client id, then:

```bash
uv run twiddle spotify auth --force --client-id <YOUR_ID>
```

It is saved to `~/.cache/twiddle/spotify/config.json`, so you pass it once.
`TWIDDLE_SPOTIFY_CLIENT_ID` overrides it if you'd rather use an env var.

No client secret is involved anywhere -- this is PKCE, so there is no secret
to leak. The refresh token is written `0600` and reused unattended forever;
the browser step happens exactly once.

## Original use (relay only)

```bash
uv run twiddle relay doctor          # is everything installed?
uv run twiddle relay login           # once: Spotify OAuth in a browser
uv run twiddle relay start --room roam --volume 30
```

Then open Spotify and cast to **Sonos Roam Relay**. Playback control stays in
the app. Ctrl-C restores the room to whatever it was playing before.

`--source tone` and `--source file --source-arg PATH` do the same thing
without Spotify, which is how the speaker half gets tested on its own.

## Architecture as built

```
Spotify app (phone/laptop)
      |  Spotify Connect protocol
      v
librespot --backend pipe   (this Mac; appears as "Sonos Roam Relay")
      |  raw S16LE 44.1kHz stereo PCM on stdout -- 176400 B/s
      v
JitterBuffer + paced writer   (relay.py: real-time clock, silence-filled)
      |
      v
ffmpeg -> 192k CBR MP3
      |
      v
Fanout HTTP server on :8899  (one encoder, many listeners)
      |
      v
Roam pair, via x-rincon-mp3radio://  -- the mechanism already proven with KALX
```

Nothing in this path talks to Sonos's cloud.

## What changed from the plan, and why

Both changes were made deliberately. The original steps are struck through
below rather than deleted.

### ~~Step 2: route spotifyd's output through BlackHole~~ → librespot's pipe backend

**Superseded.** `librespot --backend pipe` writes decoded PCM straight to
stdout, so the virtual audio device is not needed at all. This removes a
kernel-extension install, a device-name lookup that varies per machine, and
the whole class of sample-rate and clock-drift bugs that come with routing
audio between two processes through a virtual sound card.

`--source device` still exists for sources that genuinely cannot be piped —
a browser tab, an app with no CLI — and BlackHole is how you would feed it.
The Spotify path does not need it.

### ~~Step 3: `ffmpeg ... -listen 1 http://0.0.0.0:8899/stream.mp3`~~ → a fan-out server

**Superseded.** `-listen 1` serves exactly one connection and then exits.
Sonos buffers ahead, closes the socket and reopens it, so that arrangement
gives one burst of audio followed by silence — and it demos perfectly in a
30-second test, which is what makes it dangerous.

`relay.Fanout` is a miniature Icecast instead: one encoder, many subscribers,
each with a bounded queue so a slow listener sheds frames rather than
stalling the encoder for everyone. Icecast itself is no longer needed as the
fallback, and this is also the piece that carries over to a music app later.

## The constraint that dictated the design

**librespot stops writing to the pipe when playback is paused.** Left alone,
ffmpeg then starves, the MP3 stream stalls, the speaker's buffer drains and
its transport goes to STOPPED — and pressing play in the Spotify app does
*not* bring it back, because Sonos already hung up and nothing re-issues the
URI.

So the writer into ffmpeg is paced against the wall clock at exactly
176400 B/s and substitutes digital silence for whatever the source failed to
provide. A pause becomes quiet on a station that never goes off the air,
which is the radio semantics Sonos wants. `relay.pace()` is that loop, and
`tests/test_relay.py` asserts the output rate holds with a source producing
nothing at all.

The same bounded buffer doubles as a rate limiter: `tone` and `file` sources
produce thousands of times faster than real time and block on a full buffer,
so every source reaches the encoder at 1x without per-source pacing.

## What was verified, and how

| Leg | Verified |
|---|---|
| ffmpeg → MP3 → HTTP → client | **yes** — `ffprobe` on a capture: 44100 Hz stereo, 192 kb/s, 10.06s of audio in 10.0s of wall clock |
| Real-time pacing | **yes** — measured; and unit-tested against a source that produces nothing |
| Reconnect without a click | **yes** — three sequential reconnects, no "junk at 0" from `ffprobe` (an earlier capture did show 250 bytes; `_frame_sync` fixed it) |
| Mac → Roam pair | **yes** — 90s at volume 20, `PLAYING` throughout, 1 listener, 0 dropped chunks, room restored to KALX @ 35 |
| librespot launches with the argv the relay builds | **yes** — it reports `Using StdoutSink (pipe) with format: S16`, which is the format the ffmpeg input flags assume |
| A silent librespot does not kill the stream | **yes** — this is the important one. An *idle* librespot (nothing casting) produced zero bytes for 40s; the relay kept the station on air throughout, delivering 11.95s of valid MP3 per 12s of wall clock at −91 dB, i.e. digital silence. A paused Spotify is indistinguishable from this at the pipe |
| Restore on SIGTERM to the CLI process | **yes** — on the real Roam pair: signalled mid-playback, `Stopping.` printed, and the room came back to KALX @ 35 |
| Restore when `uv run` is killed instead | **no, and it does not work** — see the caveat below |
| Authenticated Spotify audio through the pipe | **yes** — *Kind Of Blue* played on the Roam pair end to end. librespot delivered 12,503,232 bytes in 71s = **176,807 B/s**, a ratio of 1.002 against the expected 176,400, which validates sample format, rate and channel count together. 0 dropped chunks, silence flat at its startup value |
| librespot visible to the Web API | **yes** — it appears in `/me/player/devices` as "Sonos Roam Relay" once a relay is up, so `spotify play` works unattended with no manual cast |

Every leg is now verified on the real hardware. `relay measure` remains the
one-command check if anything later drifts: the byte rate validates sample
format, rate and channel count together. Half of 176400 means mono; anything
else means librespot's output and the ffmpeg input flags disagree, which
would otherwise surface as audio playing at the wrong speed rather than as
an error.

### Caveat found by testing this: `uv run` does not forward SIGTERM

`kill -TERM` on the `uv run twiddle relay start ...` *wrapper* does not reach
the Python process. uv exits and takes the child down with it before the
`finally` can run, and the room is left pointed at a URL that no longer
answers. Measured, not assumed: that run left the Roams on the dead relay URL
and had to be put back by hand.

Signalling the Python process directly works correctly.

Two consequences:

* **Recovery is always available**, because the snapshot is written to disk
  before the relay makes its first write. The startup banner prints the exact
  command:
  ```bash
  uv run twiddle restore --room roam --path logs/snapshots/relay-before.json
  ```
* **This is the real obstacle to a launchd job**, over and above the
  unattended-process concern. `daemon.py` builds a plist that invokes
  `uv run --project ...`, and launchd stops a job with SIGTERM — which is
  precisely the case that does not clean up. A relay run under launchd would
  need to invoke the venv's `twiddle` directly rather than through `uv run`.

Everything *around* the untested leg is verified, including the behaviour that
a short demo would have missed — so if the authenticated path misbehaves, the relay is not
where to look first.

## Traps this actually hit

**Two volume controls in series make music inaudible.** The single most
confusing failure here, because everything reports success: transport says
`PLAYING`, the relay says one listener and zero dropped chunks, the Web API
says the track is progressing — and nothing comes out of the speaker.

librespot defaults to a software volume of 50 on a 60dB log curve, roughly
-30dB, and that multiplies with the Sonos's own setting. Measured on the
stream itself:

| | mean | max |
|---|---|---|
| librespot softvol at 49, Roam at 25 | **-50.8 dB** | -32.0 dB |
| librespot softvol at 100 | **-21.8 dB** | -2.8 dB |

The audio was always there; it was 29dB too quiet to hear. So
`spotify_source()` now pins `--initial-volume 100 --volume-ctrl fixed`:
librespot never attenuates, the Spotify app's volume slider is a deliberate
no-op, and loudness belongs to exactly one control — `twiddle volume
--room roam`. That also avoids attenuating in 16-bit and throwing away bits
to do it.

**The diagnostic that split it in one step** was capturing the relay's own
output and measuring it, rather than trusting any status field:

```bash
curl -s --max-time 8 http://127.0.0.1:8899/stream.mp3 -o /tmp/x.mp3
ffmpeg -i /tmp/x.mp3 -af volumedetect -f null - 2>&1 | grep volume
```

Real music sits near -20dB mean. Around -50dB means something is attenuating;
-91dB means digital silence and the problem is upstream of the encoder. This
is the same lesson `GUIDE.md` already records for the speakers themselves —
a component reporting `PLAYING` is not evidence that sound exists.

**`transfer` then `play` is a race.** Passing `device_id` to
`/me/player/play` already moves playback to that device. Doing it as two
calls lets the transfer auto-resume, and the `play` that follows is rejected
with `403 Restriction violated` — an error reported for an action that in
fact succeeded.

**Search ranking is not stable across `limit`.** Undocumented, and it makes
`play` play the wrong thing. Measured, searching albums for "kind of blue":

| `limit` | top result |
|---|---|
| 1 | Jazz Impressions Of A Boy Named Charlie Brown |
| 2 | Kind Of Blue (Legacy Edition) |
| 5 | Kind Of Blue |
| 10 | Kind Of Blue |

Asking for exactly the one result you want returns the wrong one. So
`_resolve_uri` asks for a page (`RESOLVE_LIMIT = 10`) and takes the top of
it, which is what a person sees when they search in the app.

**Authorizing against an existing consent can grant that consent's scopes,
not the ones you asked for.** The first token here was minted silently
against librespot's prior consent and came back with librespot's scope set
(`playlist-modify`, `ugc-image-upload`, ...) rather than the requested one.
Every call then failed, far from the cause. `Tokens.scope` now records what
was actually granted and `spotify auth` refuses a token missing playback
scopes, naming them.

Related: **refresh tokens rotate.** Each refresh returns a new one, and the
old one stops working immediately. `Tokens.from_response` adopts a rotated
token and keeps the previous one when the response omits it -- getting that
backwards means a browser sign-in every hour.

Also note a Spotify app left in **Development mode** expires its refresh
tokens after **180 days**, at which point `spotify auth --force` again.

**A Development-mode app cannot read ranked popularity data, from any
endpoint.** Found building `spotify discover`, and worth recording before
anyone reaches for these again: `GET /artists/{id}/top-tracks` returns a
flat `403 Forbidden` for a personal app — confirmed live, not assumed, with
`Session.load().request("GET", "/artists/{id}/top-tracks", ...)`, no speaker
touched. Everything this app *can* read comes back with `genres`,
`followers` and `popularity` stripped to `null`, on both artist and track
objects, from `/search` as much as from `/artists/{id}` directly. This
matches Spotify's November 2024 policy change restricting several
endpoints and fields to apps with "extended quota mode" approval, which a
personal Development-mode app does not have and is unlikely to get.

Consequence: there is no way to ask this app "what are this artist's most
popular tracks." `discover_cli._artist_tracks` works around it with two
merged search tiers instead — `sess.search('artist:"Name"', "track",
limit=10)` (reliably attributed, but capped around 5 results regardless of
`limit`, observed not documented) padded by a plain-name search (Spotify's
own relevance ordering, which favors well-known singles, but returns
other same-named artists' tracks too). **Both tiers must be filtered by
each track's `artists[].id` against the resolved artist's own id, never by
name** — measured on real, not hypothetical, collisions: a plain
`sess.search("Spoon", "track", limit=10)` returned only 2 of 10 tracks
actually by the band Spoon (the rest: Dave Matthews Band, Cibo Matto, ...),
and `sess.search("Momma", "track", limit=10)` returned **zero** tracks by
the band Momma at all — a real but not-huge act loses entirely to
better-known songs that happen to share the word.

## Conventions followed

- **Every speaker write is journalled** through `play.py`. `relay start`
  brackets the session with `journal_span("relay_start"/"relay_end")`, so
  `analyse` treats the whole window as confounded rather than only the
  instant a command was sent. This needed no change to `report.py` — the
  `_start`/`_end` span convention already handles it.
- **Snapshot before, restore after**, automatically. The room comes back to
  what it was playing without anyone remembering to ask. `--no-restore`
  opts out.
- **The restore runs from a `finally` reachable from SIGTERM**, not only
  Ctrl-C — *provided the signal reaches the CLI process*. It does not survive
  `uv run` being killed instead; see the caveat below. That is why the
  snapshot is written to disk before the first write, and why the startup
  banner prints the command that puts the room back by hand.
- **Don't test this while chasing a real dropout.** It adds a new piece to
  the audio path; keep it separate from the WiFi-link investigation until
  the relay itself is proven boring.

## Known rough edges

- **ToS**: librespot is an unofficial, reverse-engineered Spotify Connect
  client. It authenticates with your own paid account for personal playback,
  not for redistribution, but it is explicitly outside Spotify's terms for
  third-party clients.
- **Premium required.** Connect targets do not work on Free.
- **No tight transport sync.** The Spotify app talks to librespot, not to
  Sonos, so the position the app shows leads what is audible by the relay's
  buffer plus the speaker's — a few seconds. Pause and skip work; scrubbing
  feels laggy.
- **Latency is the price of never starving.** `--buffer` (default 2s) plus
  Sonos's own buffering. Lower it if you want it tighter and can accept more
  underruns.
- **This still lands on the same Roam↔AP link** for the final hop. If that
  link is bad enough this will still glitch — it removes the Connect handoff
  and the Sonos cloud, not the radio.

## Not done: running it unattended

Deliberately left out. The repo already has an incident where a
`nohup ... &` process fired after three hours and notified nobody, so the
relay is a foreground command you can see. If it earns a launchd job later,
`daemon.py` has the pattern — and note the relay would need the same Local
Network privacy grant the monitor needs.

## Where this goes next

`relay.py` is deliberately source-agnostic: a source is any process that
writes S16LE 44.1k stereo PCM to stdout, and adding one means adding an argv
and nothing else. The encoder, the pacing and the fan-out server are the
reusable part — for a music app of your own, that app becomes a fifth source
and everything downstream is unchanged.
