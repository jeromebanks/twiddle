# The rules a reviewer checks a change against

Every Codex review (a slice's pull request, a milestone, a plan) holds the change to
these rules. They come from `CLAUDE.md`'s read-only vs writing table, which stays the
reference for people. This file is what a review reads, and it is part of the review
logic's fingerprint (`tools/codex_eval.py`): editing it means a new eval.

twiddle's commands can change what a real speaker in someone's home is doing, play or
pause someone's Spotify, or set their alarms. A change is unsafe when any of these
doesn't hold:

1. **`--dry-run`.** Every command that writes to a speaker, Spotify or the household's
   alarms takes `--dry-run`. With it, the command resolves its target and prints what it
   would do, and touches nothing.
2. **Journalled.** Every such write is journalled to `logs/interventions.jsonl`, before and
   after where there is a before. A serving span counts as a write too. `analyse` discounts
   events within 45 seconds of a journalled write, which is the only thing separating real
   faults from ones the tools caused. A change must keep that intact.
3. **Rooms by name.** Speakers are targeted by room name, never by IP. A bonded follower
   can't take commands, so the write goes to its coordinator (or, for an alarm, its room's
   primary), and the output says so. `Invisible`, not `is_satellite`, marks a speaker
   unaddressable, and a group's coordinator comes from its `Coordinator` attribute, never
   from the group ID's prefix.
4. **Read-only stays read-only.** A command documented as read-only must not write to a
   speaker, Spotify or alarms, on any path, including error paths and fallbacks.
5. **Offline tests.** No test touches the network, a speaker or Spotify. Sockets listen on
   loopback only, and anything that would open one to a real or made-up address is stubbed.
6. **Nothing private committed.** No secret, token, sign-in or real device identifier
   (a speaker's RINCON ID, serial or MAC address, a household ID, a home's IP address) in
   code, tests, fixtures or docs.
