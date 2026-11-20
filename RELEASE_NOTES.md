Azeroth Survivor Reborn Trainer v1.0.8

- Memory Read adapts to new builds: when the stat block is found by code analysis, the combat stats (Damage, Crit, Crit Damage, Speed, Armor, Regen, Pickup, Gold Bonus, Area, Targets, Luck) are located from that block. No hand-entered offsets are needed for these stats on a new build.
- Health, Run Gold, Rerolls and Echo still need verified mappings and are unavailable on builds that have not been verified.
- OCR is not used to find or write stats.

Azeroth Survivor Reborn Trainer v1.0.7

- Add a saved Detection Mode selector in Advanced: Memory Read (default) or OCR.
- Retry memory discovery up to five times after the initial attempt, then capture the visible Stats panel with OCR as a diagnostic fallback.
- OCR-first mode captures visible values before memory discovery. OCR readings do not verify writable memory fields; stat writes still require a verified mapping.
- Automatically apply a stat when its Enabled checkbox is checked and its live mapping is already verified. Enabled targets also continue to apply after Find completes.
