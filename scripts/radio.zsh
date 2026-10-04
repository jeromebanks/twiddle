# Short names for the radio/Spotify commands. Source this from your shell rc:
#   source /path/to/twiddle/scripts/radio.zsh
#
# It finds the repo from its own location; set TWIDDLE_HOME first to override.
#
#   kalx, kexp, wfmu ...   play that station on the Roam   (twiddle tune <key>)
#     kexp --volume 30       any `tune` flag works: --room beam, --dry-run
#   stations               list them
#   np                     what's playing -- the last station tuned, or Spotify
#   np kexp                  or ask about any station, any time
#   np -i                  ...plus who the artist is and what the album is
#                            (MusicBrainz + Wikipedia, cached for a month)
#   np -d                  ...then straight into `discover` with that artist
#   discover [artist]      search an artist on Spotify, sample tracks, play one
#   artist <name>          who is this? same lookup as `np -i`, for any name
#     artist low --album "Things We Lost in the Fire"   when a name is ambiguous
#   dial                   radio TUI: every station's now-playing, covers, artist
#                            bios; enter tunes the Roam, +/- volume, m mute, d output
#     dial list              the same, as text (read-only)
#   scene                  local shows TUI: venues -> lineup -> band -> play
#     scene --venue ivy      start on one venue; `scene list` prints them instead
#     scene build            compile the local events dataset it reads (network)
#     scene schedule         print a launchd agent that runs the build on a timer
#   shows                  the same as `scene` (the older name)
#   vol ++3                bump the Roam's volume (twiddle vol); also -, --,
#                            --8, 44, mute, unmute
#   bass -4, treble ++2    same shorthand for EQ (-10..10): N sets absolute,
#                            ++N/--N steps relative, bare ++/-- steps by 2
#   balance +3             -10 full left .. 0 centered .. +10 full right
#   loudness [on|off]      loudness compensation; omit to read
#   shuffle [on|off]       omit to read
#   rpt [off|all|one]      repeat mode (`repeat` is a zsh reserved word --
#                            it can't be a function name -- so this is `rpt`;
#                            `twiddle repeat ...` still works directly)
#   snooze                 tonight's comedy queue on the Roam, then a native
#                            Sonos sleep timer stops it -- `twiddle comedy
#                            sleep`; --n 3 for more albums, --dry-run to preview
#   comedy artists/refresh/new/rate   the rest of `comedy` -- refresh once in
#                            a while to pick up new comedians from your
#                            listening history; refresh and new are read-only
#
#   All of vol/bass/treble/balance/loudness/shuffle/rpt default to
#   --room roam; pass your own --room to target another speaker. `snooze`
#   defaults to --room roam the same way, via `comedy sleep`'s own parser.
#
# Stations live in src/twiddle/stations/catalog/, not here. Only the station
# names, `discover`, `snooze`, and vol/bass/treble/balance/loudness/shuffle/
# rpt write to a speaker (journalled, like any twiddle write); `np`, `np -i`,
# `artist`, `stations`, `scene list`, `comedy artists/refresh/new` are
# read-only. `scene` writes only when you press play in it; `dial` only when
# you tune, stop, or change volume/mute.

# Everything below also works in bash (a Chromebook's Linux shell); only
# finding the repo differs. zsh: ${(%):-%x} is this file as sourced, :A
# resolves symlinks, :h:h is the repo. bash: the same via BASH_SOURCE.
if [ -n "$BASH_VERSION" ]; then
    TWIDDLE_HOME="${TWIDDLE_HOME:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)}"
else
    TWIDDLE_HOME="${TWIDDLE_HOME:-${${(%):-%x}:A:h:h}}"
fi

_twiddle() { uv run --project "$TWIDDLE_HOME" twiddle "$@"; }

# One word per station in the catalog, so a new station file is a new word.
# A key that already names a command or function (`top`, `kill`...) is
# skipped rather than shadowing it -- use `twiddle tune <key>` for that one.
# (`ls`, not a glob: zsh aborts the whole file on a glob that matches nothing.
# Keys are [a-z0-9] only -- test_catalog.py -- so splitting on spaces is safe.)
for _station_file in $(command ls "$TWIDDLE_HOME/src/twiddle/stations/catalog" 2>/dev/null); do
    case "$_station_file" in *.toml) ;; *) continue ;; esac
    _station="${_station_file%.toml}"
    # (A function of that name is ours from an earlier `source`: redefine it.)
    if ! command -v "$_station" >/dev/null 2>&1 || typeset -f "$_station" >/dev/null 2>&1; then
        eval "${_station}() { _twiddle tune ${_station} \"\$@\"; }"
    fi
done
unset _station _station_file

np()        { _twiddle np "$@"; }
discover()  { _twiddle spotify discover "$@"; }
artist()    { _twiddle info "$@"; }
scene()     { _twiddle scene "$@"; }
shows()     { scene "$@"; }   # the older name
stations()  { _twiddle stations "$@"; }
radio-stations() { stations "$@"; }  # the old name
comedy()    { _twiddle comedy "$@"; }
snooze()    { _twiddle comedy sleep "$@"; }

_twiddle_roam_default() {
    local subcmd="$1"; shift
    if [[ "$*" == *--room* ]]; then
        _twiddle "$subcmd" "$@"
    else
        _twiddle "$subcmd" "$@" --room roam
    fi
}

vol()      { _twiddle_roam_default vol      "$@"; }
bass()     { _twiddle_roam_default bass     "$@"; }
treble()   { _twiddle_roam_default treble   "$@"; }
balance()  { _twiddle_roam_default balance  "$@"; }
loudness() { _twiddle_roam_default loudness "$@"; }
shuffle()  { _twiddle_roam_default shuffle  "$@"; }
rpt()      { _twiddle_roam_default repeat   "$@"; }  # `repeat` is a zsh reserved word
dial()      { _twiddle dial "$@"; }
