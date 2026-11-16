Independent named-field discovery for Azeroth Survivor Reborn Trainer v0.7.0.

- Supports build 6c2b16d2b363 without adding another player-size family entry.
- Derives all 12 combat/utility stat offsets from their field names and verifies their concrete 32-bit integer formatter types.
- Independently cross-checks the player array, name, health/XP, combat and two resource serializers before enabling memory controls.
- Supports verified future player relocations and reordered named stats without a new hash or family entry; retains known older layouts as a metadata fallback.
- Rejects mismatched arrays, duplicate/out-of-bounds fields, wrong formatter types, partial pointer overwrites, pointer truncation and incomplete code evidence.
- Bounds disassembly to candidate formatter functions and caches the discovered layout; live holds only recheck loaded guards and hero identity.
- Includes Capstone's native decoder in the portable executable, with a real packaged --help startup check in the release workflow.
- 64 automated tests pass, including an unlisted future layout with reordered stats and a backend write/hold/restore test using that mapping.

The native configuration and all 15 supported fields were verified read-only
on a live solo hero in the running 6c2b16d2b363 game. Arbitrary future rewrites still cannot
be guaranteed compatible; the trainer blocks writes when proof is incomplete.
Existing Run/Stop, Find Hero with automatic Max, updater restart fixes and save
restrictions remain in place. Official game: https://azreborn.com/
