# Azeroth Survivor Reborn Trainer 0.3.0

Portable Windows trainer for Azeroth Survivors: Reborn 0.6.3 (744e2b307).
A separate trainer for **[Azeroth Survivors: Reborn](https://azreborn.com/)**.
Reborn is a different game fork with its own executable, player layout and save
format. This repository has independent releases and version history.
Derived from the process validation and recovery approach of
[Azeroth Survivors Trainer](https://github.com/nelbin4/Azeroth-Survivors-Trainer).

Download the portable zip from [Reborn Trainer Releases](https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases),
extract it, and launch **Azeroth Survivor Reborn Trainer.exe**. Start a solo run,
then click **Find hero** and **Max all stats + God mode**. Keep the trainer open.
Player controls require a verified solo character.

## Player controls

**Max all stats + God mode** holds all 15 supported stats and resources at the trainer's limits,
heals to full, and enables the native invulnerability flag. These are bounded
trainer limits; they are not a claim that every value has a native maximum.

| Stat | Trainer limit |
| --- | ---: |
| Damage bonus | 9,999% |
| Cooldown reduction | 80% native cap |
| Crit bonus | 92% plus the native 8% base |
| Crit damage bonus | 9,999% plus the native 50% base |
| Movement speed bonus | 200% |
| Damage reduction | 60% native cap |
| Regeneration bonus | 9,999 |
| Pickup range bonus | 9,999% |
| Gold find bonus | 9,999% |
| Area bonus | 1,000% |
| Extra targets | 32 |
| Luck bonus | 9,999% |
| Maximum/current health | 1,000,000 |
| Run gold | 99,999,999 |
| Free rerolls | 9,999 |

Each stat also has Set and Hold. Hold reapplies after game recalculations.
One-time controls heal, set run gold (up to 99,999,999), or place XP one point
below the next level. All 15 fields, healing, God mode and XP assistance were read back in live solo
play; the visual and gameplay effects of each remaining field need broader testing.
Max also enables XP assistance: XP stays one pickup below the next level, letting native pickups perform level-ups and card rewards. Time and kills are run counters.
Find hero again after a new run. Leave the trainer open to keep Hold active.

## Native options

God mode, auto-pick cards, all-zone access, showcase pickups, rift/bar/clock/vault
options, run clock speed (0.1–10), and next-run starting minute (0–60).
Some options are read only on menu entry or a new run. The native unlock-all
flag alone has not been verified to grant progression; use the save upgrade tab.
The game can mark these configurations as modified/test runs.

## Permanent progression

Exit Reborn, then use **Max progression**. The trainer authenticates the save,
backs it up, creates and verifies an upgrade, and atomically installs it only
while the game is closed. With the supported content catalog it adds 1,080 gear
entries across 12 classes and nine rarities, sets five stars using native save
syntax, selects the highest tier, unlocks all 83 catalog achievements, grants
maximum earned class talent XP, account level 30, and at least 99,999,999 bank gold.
Existing character identities, talent allocations and unrelated records are kept.
Individual talent ranks, paid entitlements, zone completion, and cloud-sync
controls are not modified. Cloud availability is managed by the game. Local
upgrades do not force a cloud overwrite; synchronization may replace them with
the cloud account. No cloud force-upload feature is included.

**Save game backup** copies the latest authenticated disk save and on-disk WTF
settings, including saved talent allocations. It does not force a running fight
to serialize or upload to the cloud.

Backups are in the trainer folder's backups directory. To undo a permanent
upgrade, close Reborn and restore the corresponding before-max.sav to
UserData/survivors-save.tsv in the game directory. Keep your backup private.

## Restore and validation

Restore changes or closing the trainer conditionally restores native options and
unchanged player stats from the same level. Run gold and rerolls are one-time resource grants reapplied while held; Restore leaves their balances intact. Gold, XP, healing and permanent
progression are not undone. Process restarts invalidate recovery records.
A crash may leave held stats until the game recalculates them or the run ends.

Executable SHA-256:
`ab7b6332a6c47e4c29188e44932939e560b274cf06e0f212cb3ada119a3a3d35`.
The trainer checks the executable hash, instruction signatures, process identity,
solo allocation, character GUID/name and readback before accepting writes.
Unknown builds are blocked. Player discovery reads thread stacks without hooks.
No game executable files are patched. Separate from the original game's trainer.

## Development

```powershell
pip install -r requirements.txt
python trainer.py
python trainer.py --max-player
python -m unittest discover -s . -p "test_*.py"
pip install pyinstaller
python build_exe.py
```

Portable release includes the standalone executable, README and MIT license.
Research helpers require capstone and numpy; neither is a trainer dependency.
Unofficial fan tool. MIT license; see LICENSE.
