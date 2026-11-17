# Azeroth Survivor Reborn Trainer 0.9.8

Portable Windows trainer for Azeroth Survivors: Reborn. Verified stat controls support the
original 0.6.3 game build and the newer `d76345ce7a4e`, `a02914e85f9b` and
`6c2b16d2b363` executable layouts.
The `d79ca7e2a239` build uses automatic Hunt holds for combat/resources and an
independently verified Run options connection.
A separate trainer for **[Azeroth Survivors: Reborn](https://azreborn.com/)**.
Reborn is a different game fork with its own executable, player layout and save
format. This repository has independent releases and version history.
Derived from the process validation and recovery approach of
[Azeroth Survivors Trainer](https://github.com/nelbin4/Azeroth-Survivors-Trainer).

Download the portable zip from [Reborn Trainer Releases](https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases),
extract it, and launch **Azeroth Survivor Reborn Trainer.exe**. If the game is
closed, click **Run Azeroth Survivors Reborn**. Start a solo run in the game,
then open **Stats** and click **Find**. This starts automatic background Hunt.
**Auto max & freeze newly found stats** is enabled by default. Verified fields
are maximized on their first usable read while discovery continues. No stat search or stat
write starts automatically before Find. Native run-option defaults still apply
when the game connection is verified. Keep the trainer open.

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

## Stats interface and recovery

**Stats** shows current values, targets, freeze settings and status in one
table. Filter by category and select a row to edit it in the side panel.
Click a verified row's Freeze cell or press Space on that row to toggle its hold.

**Find** becomes available after the trainer connects to the selected game process. It starts background discovery. The progress bar and elapsed timer show when Hunt is working. A first search typically takes 1–3 minutes. **Stop search** stops finding new
allocations while verified existing holds can continue. **Pause freezes** stops
stat and XP writes while discovery continues. Resume keeps configured targets
and freeze preferences. Pending, ambiguous or stale fields are never written.

Set a target and click **Apply**. Check **Keep frozen** to keep applying it,
or clear it to apply once. **Use maximum** fills the target field.
**Max & freeze all** sets every target to its trainer limit and starts Find if
no fields are verified. Crit targets are displayed as total chance/damage; the backend
converts them to bonuses above the game's native base. Echo uses percentage
units in the editor and its native float representation in memory. Current
health follows the live verified maximum. Maximum-health targets are minimums;
larger native bonuses are preserved.

God mode is in **Run options**. Find checks the live Run options configuration after finding player stats and retries the requested settings if the game reset them. Run options reports readback or errors. Next-run settings are consumed by the game when the next run starts. **Advanced…** contains manual searches,
detection details, and XP controls for verified solo layouts.

Disabling a per-stat hold leaves its current value in place. **Restore native
options** pauses stat freezes and restores eligible configuration changes; it
does not roll back Hunt stat values. Closing the trainer leaves applied stat
values in place and restores eligible native option changes. Game, build,
owner and level changes invalidate stale results before any further write.

## Trainer updates

The trainer checks for a stable GitHub release once on startup. Use **Check updates**
in the footer to check again. When a newer release is available, the button
becomes **Update**. Clicking it downloads and verifies the Windows executable,
backs up the current executable, and restarts the trainer. **Github Page**
opens the project repository. Source runs and releases without a verified
executable open the release page for manual installation.

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

### Hunt values on an unsupported build

**Find** starts automatic background Hunt for the selected game process.
No numeric values need to be entered. It keeps running when another tab is selected, without
switching tabs or taking focus from the game. It uses the same Stats interface on supported older layouts and newer
independently proven layouts, and discards stale results after process changes.

**Max & freeze each verified stat as soon as it is found** is enabled by default.
After a complete initial search and two stable observations of one solo owner,
each independently proven field is maximized and held without waiting for the
remaining fields. Holds run every half-second and skip unchanged values. Search
continues while established holds remain active. Uncheck this option or pause
Automatic Hunt to stop writes; values already applied stay in place, including
when the trainer closes. Hunt does not add changes to the normal Restore journal.

The running `d79ca7e2a239` build independently proves maximum health, current
health, run gold and rerolls through the named resource formatter, its integer
formatter widths, and a second serializer using the same player array. These
fields are joined by all 12 combat/utility stats through the newer compact named
formatter. Bounded control-flow analysis follows each 32-bit copied Stats field
through calculations into its named display argument; it tolerates reordered
fields and rejects ambiguous dependencies. Verified single-input math helpers
and integer formatter widths are included in the loaded-code guards.

Echo uses an independently traced active-effects table entry. When that effect
is present, Hunt maxes its chance to 100% and revalidates the table header, key
and bucket before writes. It keeps hunting when the entry is absent; it does
not insert effects, allocate game memory or change equipment. Table movement or
a missing effect skips Echo while other stats remain held. Numeric matches
alone never enable writes. The category rows distinguish active freezes from pending or unverified fields.

**Run options** are discovered independently of the player layout. Verified
configuration anchors enable clock speed, starting minute, boss timing and the
native switches even if the old player-name formatter has changed. Native writes
and Hunt holds share the same in-app write gate and process mutex. The requested
defaults remain speed 3, next-run minute 20, and bosses on the clock enabled.

Hunt independently finds the player-array serializer in the game executable,
checks those instructions in the loaded image, and searches thread stacks for
candidate solo array owners. It hunts integer value pairs and stat-block values
inside candidate allocations, then compares successive snapshots. Shared health
header and integer-stat plausibility bounds narrow candidates; these heuristics
do not establish named stat mappings. Independent per-field code evidence is
required before each write. Changed types/headers can require detector
updates. The current array-search strategy still requires a unique code anchor.

Stack searches run in bounded background rounds (up to 8 MiB of stack content,
with a 0.3-second scheduling target and bounded candidate probes). Partial rounds
resume automatically. Located roots are revalidated and sampled every two seconds;
full discovery repeats periodically. Active freezes run at half-second intervals.
Missing heroes/errors retry with delays up
to 15 seconds. Unchanged failed code searches are cached until the image changes.
Cancellation and process identity are checked throughout the search.

The optional **Advanced… → Manual value search** view retains exact-value searching.
Select **64-bit integer** for known integer health, enter its current displayed
value, and click **New hunt**. When it changes, enter the new value and refine.
Use **32-bit integer** for known integer bonuses or run gold. Types can change
between game versions; manual searching is not required by Automatic Hunt.

Each manual search examines up to 256 MiB of readable private memory and retains at most
4,096 aligned candidates. **Continue hunt** examines the next portion using the
same value. A changing value may require a new search; refinement only checks
the candidates already found. A candidate limit requires refinement or a more
distinctive value. Results show up to 500 addresses. Searches run in the
background and validate the PID, creation time and executable path.

Manual searches and unmapped candidates are read-only. A numeric match, even a
single one, does not establish an authoritative player field. Verified Hunt
freezes recheck executable identity, loaded code, the selected process, solo
ownership, hero identity and level before writes. A field-specific proof failure
skips that field; ownership changes stop the batch. A per-process writer lock
prevents competing trainers from writing at once. No addresses are saved across
sessions and Hunt does not edit saves. On `d79ca7e2a239`, the old full Player/name
contract is still unavailable, so Stats uses independent Hunt field proofs.
Run options remain usable. Verified older layouts feed the same Stats controls;
no old player-name contract is guessed.

### Automatic player mapping

The trainer derives the 12 combat/utility stat fields from their actual names in the
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

Discovery uses file identity and a five-second freshness window. After refreshing
the complete executable SHA-256, unchanged bytes reuse a bounded cache of verified
instruction proofs instead of repeating disassembly. Loaded instructions and hero
identity are still rechecked before writes.

Instruction searches accept verified register allocations, short/near bounds
branches, alignment NOPs and reordered vector loads. Configuration candidates must
agree with their initialization/speed readers and a constructor's related flag
stores. Solo discovery supports bounded vector capacities through 64 while still
requiring exactly one active hero; multiple players remain blocked. Duplicate stack references to one hero are
grouped by allocation and identity. Distinct hero allocations remain blocked.
Hold polling skips unchanged values and uses no injected code.

Permanent save upgrades remain restricted to the separately verified original
save format/build. A compatible live layout does not establish save compatibility.

## Player controls

**Max & freeze all** holds supported stats and resources at the trainer's limits.
Current-health holds heal to full; God mode is controlled separately. These are bounded
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

Each stat has an editor and a per-stat Freeze setting. Holds reapply
after native recalculations. Current health can be applied once to heal to full.
XP assistance and One XP from level-up retain the independently verified full
player/name contract and are unavailable when that contract is missing.

After Find, Hunt follows new runs and validates their fresh solo ownership before
resuming eligible holds. Stop search does not locate a new allocation until Find
is used again. Pause freezes leaves configured targets intact and resumes only
through Resume freezes or Max & freeze all. Keep the trainer open for active holds.

## Native options

The Run options tab defaults to **run clock speed 3**, **next-run starting minute
20**, and **Bosses also arrive on the clock enabled**. The GUI applies these values
once after attaching to each verified game process. Later edits and Restore are
preserved across reconnects to that same process. The starting minute applies to
the next run when the game reads it.

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


### Selected-player validation and host sessions

Hunt verifies the selected character's live identity string using corroborating code proofs. Names are read dynamically; no test character name is hardcoded. The scan resumes at the exact candidate after its probe budget is exhausted, so dense stacks do not hide the active player array.

**Host session (automatic)** follows the game's live lobby role. It checks itself for a verified host and clears itself for a guest or a solo/no-lobby session. Guests and unknown roles cannot authorize stat writes, including a guest currently alone in a player array. There is no manual host override. Only the character selected by the verified game index is eligible; other party members are left alone. XP assistance remains solo-only.

Role discovery runs read-only in the background when the game is detected. A cold scan or a UI runtime reload may take longer on processes with large memory footprints; the UI stays responsive and stat writes wait for verification. A process-bound address cache is always revalidated against the live Lua main thread, global environment and current lobby table. Runtime, PID, global-table and lobby changes invalidate old ownership. Role is checked again before every stat write. Find still starts stat discovery and automatic max/freeze. Native Run options use their independent detector and remain available while role detection runs.

The reader validates the game's Lua 5.1 runtime object layout. An unsupported runtime stays unknown/read-only; future game versions are not guaranteed compatible.

**Applying; verifying** means the write was read back immediately. **Frozen** requires a subsequent observation at the requested value, with the same player and code proofs. A recalculated value returns to applying status.
