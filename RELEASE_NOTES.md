Incremental Max & Freeze for Azeroth Survivor Reborn Trainer v0.8.1.

- Adds enabled-by-default Max & freeze each verified stat as soon as it is found.
- Applies independently proven fields without waiting for a complete player layout, while background discovery continues.
- Holds verified fields every half-second and skips unchanged values.
- Proves resource fields using named format arguments, concrete integer formatter widths and an independent serializer of the same player array.
- Supports independent named Stats formatter evidence for additional combat fields.
- Requires a complete initial search, stable observations of one solo owner, loaded-code guards and fresh ownership checks before writing.
- Blocks unverified candidates, conflicting fields, changed code and stale owners. A field-specific proof failure does not block unrelated verified fields.
- Uses the existing per-process writer lock and closes writable handles after each batch.
- Displays MAX & FROZEN fields separately from raw candidates. Disabling freeze/Automatic Hunt stops writes and leaves current values in place; closing also leaves applied Hunt values in place.
- 104 tests pass, including independent field discovery, typed/label proof rejection, incremental freezing, value races, cancellation, ownership changes, conflicting fields and unchanged-value suppression.

Live testing on d79ca7e2a239 applied and read back maximum/current health
1,000,000, run gold 99,999,999 and rerolls 9,999. Its other combat/utility stat
slots are still unverified, so they are not written. The full-layout Max all
stats button remains blocked until the remaining mapping is proven.

Retains run defaults (speed 3, next-run minute 20, bosses on the clock), the
verified updater restart workflow, and earlier verified automatic layouts.
Official game: https://azreborn.com/
