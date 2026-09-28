# Quick start: setting up on your own Mac

For the fun parts: `shows` (upcoming Bay Area shows, with who each band is
and their music) and `dial` (every radio station's now-playing, one key to
listen). **No Sonos needed.** The Sonos tools are there if you have one, and
you can ignore them if you don't.

About 10 minutes, once.

## What you need

- A Mac with [Homebrew](https://brew.sh).
- **Spotify Premium**, to play from Spotify. Spotify only lets Premium
  accounts play through third-party players. Everything else works without
  Spotify at all: radio, browsing shows and band bios, and playing a band's
  Bandcamp tracks on your Mac. Without Spotify, skip steps 3 and 4.

## 1. Install

```bash
brew install uv librespot ffmpeg
git clone https://github.com/jeromebanks/twiddle.git
cd twiddle
uv sync                 # fetches Python 3.12+ and the dependencies
```

`librespot` is the Spotify player; `ffmpeg` provides `ffplay`, which plays
radio and Bandcamp tracks on your Mac. Clone it wherever you like.

## 2. The short names

```bash
echo "source $PWD/scripts/radio.zsh" >> ~/.zshrc
source ~/.zshrc
```

Run this from inside the clone. It gives you `shows`, `dial`, `np`,
`artist`, `stations` and `discover`. The script finds the repo from its own
location, so the path above is all it needs. Every one is also
`uv run twiddle ...` from inside the repo.

Leave out the station names (`kexp`, `kalx`, ...) and `vol`/`bass`/`snooze`:
they play on a Sonos room named `roam`, so without a Sonos they only
print an error.

## 3. Your own Spotify developer app

It's free, and the app needs it to search Spotify and control playback.

1. Go to <https://developer.spotify.com/dashboard>, log in with your Spotify
   account, and click **Create app**.
2. Give it any name and description.
3. Set the **Redirect URI** to exactly `http://127.0.0.1:5588/login`, and
   click **Add**.
4. Under *Which API/SDKs are you planning to use?*, tick **Web API**, then
   click **Save**.
5. Open the app's **Settings** and copy its **Client ID**.

You need your own because the fallback is a public client id shared by every
librespot user in the world, and Spotify rate-limits it until it's useless.

## 4. Sign in (two browser approvals)

```bash
uv run twiddle spotify auth --client-id <your Client ID>   # lets the app search and control Spotify
uv run twiddle scene login                                 # signs your Mac's player in
```

Each opens a browser (or prints a URL). Approve it, and the command finishes
by itself. Tokens go to `~/.cache/twiddle/`, outside the repo.

## 5. Use it

```bash
shows                     # the local-shows app
dial --output mac         # the radio app, playing through this Mac
```

**`shows`:**

- `j`/`k` or the arrow keys move, and `enter` goes in a level: show →
  lineup → a band's tracks.
- `p` plays. The first time, it asks where to play. Choose **This Mac
  (speakers)**, which plays through whatever macOS is using: speakers,
  headphones or AirPods.
- `space` pauses, `n` skips, `?` lists every key, and `q` quits. Quitting
  stops the music.
- It shows up in Spotify as "*your Mac's name* (scene)", so your phone's
  Spotify app can control it while the app is open.
- If Spotify is already playing on your phone, the first `p` warns you that
  it will stop it. Press `p` again to go ahead.
- A wider terminal looks best: 110 columns or more shows all three panes.

The full guide, including choosing your own venues: [SCENE.md](SCENE.md).

**`dial`:** `enter` tunes the highlighted station, `+`/`-` changes the Mac's
volume, `m` mutes, `?` lists every key. Without `--output mac` it looks for
a Sonos first. Details: [GUIDE.md → Dial](GUIDE.md#dial-every-station-at-once-and-one-key-to-tune).

**Without a TUI:**

```bash
shows list                # this week's shows, as text
shows list --venue stork  # one venue (partial names work)
dial list                 # every station's now-playing
np kexp -i                # what's on KEXP, and who the artist is
artist "street eaters"    # who is this band?
```

## If something goes wrong

| Symptom | Fix |
|---|---|
| `shows: command not found` | step 2, or open a new terminal |
| `librespot not found` | `brew install librespot` |
| `ffplay isn't installed` | `brew install ffmpeg` |
| "this Mac isn't signed in to Spotify as a speaker yet" | `uv run twiddle scene login` |
| "the local Spotify player exited as it started" | read `~/.cache/twiddle/librespot-local/librespot.log`. It's usually a free (non-Premium) account, or a sign-in that needs redoing: `scene login --force` |
| A band's badge says "Spotify: not signed in" | expected without steps 3–4; its Bandcamp tracks (listed first) still play |
| `429` / rate limited | Spotify's per-app quota: wait the time it says (it can be hours after heavy use) |
| `403` on play, or "missing scopes" | revoke the app at <https://www.spotify.com/account/apps>, then `uv run twiddle spotify auth --force` |
| Stopped working after ~6 months | Development-mode Spotify apps expire their sign-in after 180 days: `uv run twiddle spotify auth --force` |
| No shows | `shows list --refresh` prints a warning line for each source (The List and each venue's own page) it couldn't reach or read |

To update later: `git pull && uv sync`.

## On a Chromebook (Linux container)

Turn on Linux (*Settings → About ChromeOS → Developers*), then in its terminal:

```bash
sudo apt install ffmpeg pkg-config libasound2-dev libpulse-dev libssl-dev
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal   # Debian's Rust is too old
~/.cargo/bin/cargo install librespot --locked                     # ~10 min; Spotify only
git clone https://github.com/jeromebanks/twiddle.git && cd twiddle && uv sync
cat >> ~/.bashrc <<EOF
export PATH="\$HOME/.local/bin:\$HOME/.cargo/bin:\$PATH"
export TWIDDLE_ANCHOR=192.168.1.2      # any speaker's IP; only if you have a Sonos
source $PWD/scripts/radio.zsh           # works from bash too
EOF
```

Then steps 3–5 as above. The differences from a Mac:

- `dial` plays on the Chromebook's speakers; `+`/`-`/`m` set the ChromeOS
  volume through `pactl`.
- The container is NATed off the LAN, so Sonos discovery never answers.
  Speakers are reachable directly, which is what `TWIDDLE_ANCHOR` is for.
- Bluetooth output in `dial` is macOS-only. Pair headphones in ChromeOS
  instead; they become the Chromebook's speakers.
- Don't run `relay` here: the speakers can't connect back into the container.
