Broader verified discovery and run defaults for Azeroth Survivor Reborn Trainer v0.7.1.

- Replaces register-specific player-array, name and independent resource searches with instruction-validated matches.
- Supports equivalent register allocations, short/near bounds branches, alignment NOPs and reordered stat-vector loads.
- Broadens configuration readers across registers while requiring related constructor flags, clock storage, and independent initialization/speed corroboration.
- Reuses verified layout proofs only after refreshing the full executable hash; changed binaries are revalidated and loaded guards still run before writes.
- Supports solo vector capacities through 64 while retaining hero identity and multiplayer write guards.
- Run options now default to clock speed 3, next-run minute 20 and Bosses also arrive on the clock enabled.
- Applies run defaults once per verified process identity; subsequent manual edits survive reconnects.
- 76 automated tests pass, including renamed/padded independent helpers, mismatched/duplicate candidates, code-change cache invalidation, larger solo allocations, and native minutes-to-seconds/default behavior.

Retains v0.7.0 named stat discovery and the verified updater restart workflow.
Future compiler/schema changes can still require an update. Uncertain mappings
remain blocked; save-format and cloud-save restrictions are unchanged.
Official game: https://azreborn.com/
