Automatic background Hunt for Azeroth Survivor Reborn Trainer v0.8.0.

- Automatically hunts candidate live values when normal layout detection is unavailable, without entering values or clicking Search.
- Runs in the background across tabs and keeps the game running, without switching tabs or taking focus.
- Independently identifies the player-array search region through code, validates the loaded instructions and rechecks candidate ownership.
- Uses bounded stack searches, automatic continuation, temporal comparisons, cached code searches and retry backoff.
- Revalidates process identity, resets on process changes, and pauses when normal detection is ready.
- Keeps exact-value search/refine/continue tools in the optional Advanced manual search subtab.
- Hunt opens the game read-only. Matching values never automatically enable writes or become trusted stat mappings.
- 92 automated tests pass, including automatic candidate discovery without input, changing live values, stale ownership, process reuse, hero changes, ambiguous allocations, cancellation, scan continuation, changed code, cached failures and retry timing.

Live read-only testing automatically found candidate live health and stat values
in the running d79ca7e2a239 build. Its full named player layout is still unverified: Max all
stats and other mapped memory writes remain blocked on that build. This release
adds actual value searching, not a claim of universal future-version support.

Retains run defaults (speed 3, next-run minute 20, bosses on the clock), the
verified updater restart workflow, and earlier verified automatic layouts.
Official game: https://azreborn.com/
