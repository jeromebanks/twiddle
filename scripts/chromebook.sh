#!/usr/bin/env bash
# Run twiddle from the Chromebook's Linux container, where Sonos multicast
# discovery doesn't work: point it at one speaker with TWIDDLE_ANCHOR.
#
#   scripts/chromebook.sh            # check the speaker, then run `twiddle dial`
#   scripts/chromebook.sh rooms      # check, then any other twiddle command
#   TWIDDLE_ANCHOR=192.168.1.9 scripts/chromebook.sh   # a different speaker
#
# It also sets the container's audio sink to TWIDDLE_SINK_VOLUME (default 100%;
# "keep" leaves it alone). ChromeOS's container starts it low (it was 40%), and
# that multiplies with dial's own volume. Each launch appends what it found and
# did to logs/chromebook.log, to learn whether the level resets across reboots.
#
# To keep the anchor in your own shell instead:  source scripts/chromebook.sh --env
# The Roam sleeps; if this stops answering, wake it or use a mains-powered speaker's IP.
set -u

export TWIDDLE_ANCHOR="${TWIDDLE_ANCHOR:-192.168.1.4}"

if [ "${1:-}" = "--env" ]; then
    return 0 2>/dev/null || exit 0
fi

if ! curl -fsS -m 3 -o /dev/null "http://$TWIDDLE_ANCHOR:1400/xml/device_description.xml"; then
    echo "chromebook.sh: can't reach a Sonos speaker at $TWIDDLE_ANCHOR:1400 from this container." >&2
    echo "  Sonos will be missing from dial; This Chromebook still works. Continuing." >&2
fi

cd "$(dirname "$0")/.." || exit 1

sink_volume() {   # "40%" for the default sink, or "?"
    pactl get-sink-volume @DEFAULT_SINK@ 2>/dev/null | grep -o '[0-9]*%' | head -1 || true
}

want="${TWIDDLE_SINK_VOLUME:-100%}"
before="$(sink_volume)"; before="${before:-?}"
if [ "$want" != "keep" ] && command -v pactl >/dev/null; then
    pactl set-sink-volume @DEFAULT_SINK@ "$want" 2>/dev/null || true
fi
after="$(sink_volume)"; after="${after:-?}"
# pipewire's age: a small number means the audio server restarted since last time.
pw_age="$(ps -o etimes= -C pipewire 2>/dev/null | head -1 | tr -d ' ')"
mkdir -p logs
printf '%s sink before=%s after=%s wanted=%s pipewire_age_s=%s uptime=%s\n' \
    "$(date -Is)" "$before" "$after" "$want" "${pw_age:-?}" \
    "$(cut -d' ' -f1 /proc/uptime)" >> logs/chromebook.log
[ $# -eq 0 ] && set -- dial
exec uv run twiddle "$@"
