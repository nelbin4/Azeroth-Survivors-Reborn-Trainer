# Azeroth Survivor Reborn Trainer 0.7.0

Portable Windows trainer for Azeroth Survivors: Reborn. Live controls support the
original 0.6.3 game build and the newer `d76345ce7a4e`, `a02914e85f9b` and
`6c2b16d2b363` executable layouts.
A separate trainer for **[Azeroth Survivors: Reborn](https://azreborn.com/)**.
Reborn is a different game fork with its own executable, player layout and save
format. This repository has independent releases and version history.
Derived from the process validation and recovery approach of
[Azeroth Survivors Trainer](https://github.com/nelbin4/Azeroth-Survivors-Trainer).

Download the portable zip from [Reborn Trainer Releases](https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases),
extract it, and launch **Azeroth Survivor Reborn Trainer.exe**. If the game is
closed, click **Run Azeroth Survivors Reborn**. Start a solo run in the game,
then click **Find Hero**. After verifying your solo hero, the trainer automatically
applies **Max all stats**, God mode and XP assistance. Keep the trainer open.
Player controls require a verified solo character.

## Launch and process controls

**Run Azeroth Survivors Reborn** launches the saved game executable from its own
folder and pins the returned PID, process creation time and executable path.
On first use, select the installed `AzerothSurvivorsGame.exe`. The path is saved;
**Game executable…** changes it later without launching the game.

Run is disabled whenever a Reborn process is detected, including unsupported
builds. The process row shows the selected PID. Existing single instances are
automatically detected. With several instances, the saved executable path can
select a unique instance; ambiguous instances are never attached or stopped.
An inaccessible process also blocks a duplicate launch.

**Stop game** terminates only the selected, revalidated process after a confirmation
showing its PID and path. Unsaved run progress can be lost. The trainer first
stops holds and restores eligible changes when attached. Stop remains available
when memory-layout detection fails; no game compatibility checks are bypassed.
Run becomes available again after the process exits. Closing the trainer alone
leaves the game running.

## Interface and recovery

The Compact Companion layout places essential controls and six favorite stats
beside a selected-stat editor. Click Edit to view a stat's live value, supported
range, target and Hold control. Show all 15 controls expands the complete list;
selecting a stat brings its editor into view. Max, Pause and Restore remain
above the favorites. Mouse-wheel scrolling works in Live controls; the window
can be resized. Run options and Saves retain the existing native and save tools.
Pause holds stops repeated stat/XP writes and leaves current values in place.
Restore changes restores eligible values and cancels pending hero searches.

Hero discovery runs on a background read-only connection. The interface remains
responsive while searching, and results from stale process identities or cancelled
searches are discarded. Reconnecting uses bounded retry delays instead of
repeated rapid attach attempts. A disconnect clears stale readings and hold flags.

## Trainer updates

The footer and Updates tab check this repository's latest stable GitHub release.
Automatic checks run shortly after startup and every six hours; disable them
with Automatic checks. Check updates and Release page are always available.
Checks run in the background and do not interrupt hero discovery.

When a newer executable is available, click Install update & restart and confirm.
The trainer validates the repository URL, declared size, GitHub SHA-256 digest
and Windows x64 executable header before staging it. It restores eligible game
changes, exits, replaces its executable and restarts. Settings and save backups
stay in the same folder. Installation is never automatic. Source installations
open the release page instead of replacing Python files.

The previous executable is retained as `Azeroth Survivor Reborn Trainer.previous.exe`.
If an update cannot start, close the trainer and copy that backup over the main
executable. The trainer folder must be writable. Network failures leave the
installed version usable; GitHub API rate limits can delay checks. No GitHub
login or credentials are required.

The restart helper resets PyInstaller's runtime environment so the updated
executable extracts a fresh Python runtime. If updating from v0.6.0 through
v0.6.3 shows a missing Python DLL error immediately after installation, dismiss
the error and open the trainer executable normally once. The downloaded binary
is intact; v0.6.4 fixes subsequent update restarts.

## Update compatibility

v0.7.0 derives the 12 combat/utility stat fields from their actual names in the
game's Stats formatter, confirms their 32-bit integer types, and independently
checks the player array, name, health/XP and reroll serializers. The player
structure size is no longer restricted to a family list when that evidence is
available. Relocated player structures and reordered named stats can be supported
without adding a new executable hash or size entry. Verified older layouts remain
as a fallback for stripped metadata.

This improves update tolerance; it cannot guarantee arbitrary future versions.
Compiler changes that remove metadata, rename fields, change types or alter the
verified helper contracts can still require an updated detector. Incomplete,
conflicting or out-of-bounds evidence blocks writes. Detection does not execute
game code or scan by guessing plausible live numbers.

The `6c2b16d2b363` layout resolves through independent named-field discovery,
including its changed player size, name/stat/reroll offsets and game vector.
The native configuration and all 15 supported player fields were read back
successfully from a live solo hero in the running game.

Discovery uses a bounded cache with file identity and a five-second freshness window; loaded instructions and hero
identity are rechecked before writes. Duplicate stack references to one hero are
grouped by allocation and identity. Distinct hero allocations remain blocked.
Hold polling skips unchanged values and uses no injected code.

Permanent save upgrades remain restricted to the separately verified original
save format/build. A compatible live layout does not establish save compatibility.

## Player controls

**Max all stats** holds all 15 supported stats and resources at the trainer's limits,
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

Each stat has Apply value and Hold this value in its editor. Hold reapplies after game recalculations.
One-time controls heal, set run gold (up to 99,999,999), or place XP one point
below the next level. All 15 fields, healing, God mode and XP assistance were read back in live solo
play; the visual and gameplay effects of each remaining field need broader testing.
Max also enables XP assistance: XP stays one pickup below the next level, letting native pickups perform level-ups and card rewards. Time and kills are run counters.
Click Find Hero again after a new run to verify and max the new character.
Pause stops repeated writes; they resume only when you enable holds, use Max all
stats, or find a hero again. Leave the trainer open to keep Hold active.

## Native options

God mode, auto-pick cards, all-zone access, showcase pickups, rift/bar/clock/vault
options, run clock speed (0.1 to 10), and next-run starting minute (0 to 60).
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

Originally verified executable SHA-256:
`ab7b6332a6c47e4c29188e44932939e560b274cf06e0f212cb3ada119a3a3d35`.
The trainer records the executable hash and checks discovered instruction signatures, process identity,
solo allocation, character GUID/name and readback before accepting writes.
Unverified or ambiguous field mappings are blocked. Player discovery reads thread stacks without hooks.
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
The trainer uses psutil and Capstone; the portable build includes both.
Research helpers additionally require numpy.
Unofficial fan tool. MIT license; see LICENSE.
