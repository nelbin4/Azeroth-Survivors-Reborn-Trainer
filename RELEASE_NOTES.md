All combat stats and native Run options for Azeroth Survivor Reborn Trainer v0.8.3.

- Discovers all 12 combat/utility fields from the newer compact named formatter, including damage reduction and flat regeneration.
- Follows the copied Stats fields through bounded control-flow/data-flow analysis instead of assuming old offsets. Reordered fields work; conflicting origins are rejected.
- Guards verified single-input math helpers and concrete integer formatter widths alongside the formatter code and labels.
- Adds Echo chance up to 100% when its active-effects entry exists. Revalidates the current table, bucket, key and header; missing entries and rehashes skip Echo without blocking other fields. Does not insert effects or allocate game memory.
- Discovers Run options independently from the full player layout. Clock speed, starting minute, bosses on the clock and native switches can work even when the old player-name formatter changes.
- Shares the native trainer's process mutex and write gate with Hunt, avoiding competing writers while preserving configuration recovery.
- Replaces the unavailable Find Hero action with View Hunt stats when the native-only connection is active.
- Retains automatic background hunting and incremental max/freeze, with speed 3, next-run minute 20 and bosses on the clock as the run defaults.
- Skips exited threads without a TEB and retries incomplete module capture after transient process-query failures.
- 121 tests pass, including reordered compact fields, conflicting/unreachable branches, duplicate roles, partial overwrites, scalar widths, unary-helper validation, native-only configuration, Echo entries, absent effects, table rehash races and thread-lifetime recovery.

Live testing on d79ca7e2a239 applied all 12 combat/utility limits plus health,
maximum health, run gold and rerolls. Native controls were independently read
and the requested run defaults applied. Echo's active entry is conditional; it
is held when present and remains pending when absent. The legacy full Player
name contract is still unavailable, so this build uses Hunt for player holds.

Official game: https://azreborn.com/
