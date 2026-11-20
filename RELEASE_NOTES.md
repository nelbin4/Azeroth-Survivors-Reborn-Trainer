Azeroth Survivor Reborn Trainer v1.0.6

- Remove the lobby-role lookup from Find and Apply. Apply uses the discovered character and verified stat mapping directly, rechecking character identity and field mappings before each write.
- Acquire the shared write mutex only while a trainer is actively applying stats, so an idle mutex handle does not block Apply.
- Check live stat instructions per group so a changed build fingerprint alone does not block compatible layouts.
- Add a verified resource mapping for health, maximum health, run gold, and rerolls on game build c639287405d0. Add 11 combat mappings for the same build. Cooldown reduction and Echo remain unavailable.
- Show layout and instruction-check results in Advanced and the diagnostic log.
- List available stats when none of the requested targets has a verified mapping. Find still applies supported targets in a mixed selection.
- Read the in-game Stats panel with Windows OCR from Advanced and include the visible readings in Copy details.
- Show the number of stats found separately from Apply status and log why session verification stops.
