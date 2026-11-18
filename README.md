# Azeroth Survivor Reborn Trainer

Portable Windows trainer for [Azeroth Survivors: Reborn](https://azreborn.com/).
Find adjusts verified stats for your selected character in solo runs and
verified co-op host sessions.

## Quick start

1. Download and extract the portable ZIP, then open **Azeroth Survivor Reborn Trainer.exe**. [Latest release](https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases/latest)
2. Click **Run Game** or open Reborn yourself. Start a run and wait for the trainer to show the connected game process.
3. Click **Find**. Hunt searches in the background. Each checked stat applies and freezes at the trainer limit as soon as Hunt verifies the character, session, and stat.

Damage bonus, Cooldown reduction, Area of effect, and Luck bonus are checked by default.
Selections and manual targets are stored in
`%LOCALAPPDATA%\Azeroth Survivor Reborn Trainer\reborn-settings.json`.
On upgrade, the trainer imports older settings from beside the executable
if the new settings file does not exist.
Keep the trainer open for active freezes.

## Launch and process controls

**Run Game** launches the saved game executable from its own
folder and pins the returned PID, process creation time and executable path.
On first use, select the installed `AzerothSurvivorsGame.exe`. The path is saved;
**Locate exe file** changes it later without launching the game.

Run is disabled whenever a Reborn process is detected, including unsupported
builds. The process row shows the selected PID. Existing single instances are
automatically detected. With several instances, the saved executable path can
select a unique instance; ambiguous instances are never attached or stopped.
An inaccessible process also blocks a duplicate launch.

**Stop Game** closes the selected game process after a confirmation showing
its PID and path. Unsaved run progress can be lost. Closing the trainer leaves
Reborn running and ends active holds.

## Stats interface and recovery

**Stats** shows current values, targets, freeze settings and status in one
table. Select a row to edit it in the side panel.
Click a row’s **Freeze** checkbox or press Space on that row. Checked stats
apply their maximum and hold it once verified. Unchecked stats can be changed
once by editing the target and pressing **Apply**. **Use maximum** fills the
manual target with the trainer limit.

**Find** starts background discovery after the trainer connects to Reborn.
**Stop search** stops finding new allocations while verified holds can continue.
**Stop Freeze** suspends holds while allowing manual Apply. **Resume Freeze**
reapplies the checked stats.

**Advanced** opens Detection details first as readable plain text. **Copy details**
copies the displayed text. The other tab is Manual value search.

Game, build, and player ownership changes invalidate stale results before any further write.

## Troubleshoot missing stats

Open **Advanced → Detection details** and check **Character**, **Session**, and
**Verified stats** together. Find needs a live selected character and a verified
solo or host session before it can apply changes.

| What you see | What it means |
| --- | --- |
| **Finding the selected player allocation** | Hunt is still looking for a live character. Start a run if you're at the dashboard. |
| **Stats found. Checking session role** | Hunt found stat mappings and is waiting for live lobby verification. Co-op writes require the game to report this client as host. |
| **Guest · read-only** | The game reports this client as a guest. If the in-game lobby shows you as host, capture that indicator and the detection details from the same run. |
| **Player array unavailable** | Hunt could not corroborate a unique player array for this build. Waiting on the same executable will not change that result. |
| **Some stats are Unavailable** | Hunt can apply mapped stats. The remaining fields need game-build support. |

When reporting an issue, include the trainer version, whether the run was solo
or co-op, and the output from **Copy details**.

## Trainer updates

The trainer checks for a stable GitHub release once on startup. Use **Check updates**
in the footer to check again. When a newer release is available, the button
becomes **Update**. Clicking it downloads and verifies the Windows executable,
backs up the current executable, and restarts the trainer. **Github Page**
opens the project repository. Source runs and releases without a verified
executable open the release page for manual installation.

If an update cannot start, close the trainer and copy that backup over the main
executable. The executable folder must be writable for the self-update; settings
and logs live under `%LOCALAPPDATA%`, so the trainer still starts from a
read-only folder. Network failures leave the
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
No numeric values need to be entered. Hunt runs without taking focus from the game.
It uses the same Stats interface on supported older layouts and newer
independently proven layouts, and discards stale results after process changes.

Checked stats freeze at maximum as soon as they are verified.
After a complete initial search and two stable observations of the selected character,
each independently proven field is maximized and held without waiting for the
remaining fields. Holds run every half-second and skip unchanged values. Search
continues while established holds remain active. Uncheck a stat or use Stop Freeze
to suspend holds. Values already applied stay in place, including
when the trainer closes, until the game recalculates them or the run ends.

The `d79ca7e2a239` build independently proves maximum health, current
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
alone never enable writes. The Stats table distinguishes active freezes from pending or unverified fields.

Hunt first looks for the stat serializer in the game executable. If that search
is missing or ambiguous, Hunt checks whether the character identity and typed
resource routines agree on one player array. A fallback discovery can verify
health, gold, and rerolls even when the combat-stat block cannot be verified.
In v0.9.17, this fallback recovered those mappings from a test copy of build
`950599aec161` with the primary signature removed.
Hunt checks the corroborating instructions in the loaded image and searches
thread stacks for the selected character. It hunts integer value pairs and stat-block values
inside candidate allocations, then compares successive snapshots. Shared health
header and integer-stat plausibility bounds narrow candidates; these heuristics
do not establish named stat mappings. Independent per-field code evidence is
required before each write. Changed types or headers can require detector updates.
The search status distinguishes unavailable player arrays, pending session checks,
and unavailable stat mappings. Advanced shows which detector located the array.

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
or verified host ownership, hero identity and level before writes. A field-specific proof failure
skips that field; ownership changes stop the batch. A per-process writer lock
prevents competing trainers from writing at once. No addresses are saved across
sessions and Hunt does not edit saves. On `d79ca7e2a239`, the old full Player/name
contract is still unavailable, so Stats uses independent Hunt field proofs.
Verified older layouts feed the same Stats controls;
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
branches, alignment NOPs and reordered vector loads. Hunt groups duplicate stack
references by character allocation and identity. Distinct allocations keep
writes blocked until Hunt can identify the selected character.
Hold polling skips unchanged values and uses no injected code.

## Player controls

Checked stats use the trainer limits below.
Current-health holds heal to full. These are bounded
trainer limits; they are not a claim that every value has a native maximum.

| Stat | Trainer limit |
| --- | ---: |
| Damage bonus | 9,999% |
| Cooldown reduction | 80% native cap |
| Critical strike | 100%, including the native 8% base |
| Crit damage bonus | 9,999% plus the native 50% base |
| Movement speed bonus | 200% |
| Damage reduction | 60% native cap |
| Regeneration bonus | 9,999 |
| Pickup range bonus | 9,999% |
| Gold find bonus | 9,999% |
| Area bonus | 1,000% |
| Extra targets | 32 |
| Echo chance | 100% while the Echo effect is active |
| Luck bonus | 9,999% |
| Maximum/current health | 1,000,000 |
| Run gold | 99,999,999 |
| Free rerolls | 9,999 |

Holds reapply after native recalculations. Check **Current health** to keep it
at verified maximum health.

After Find, Hunt follows new runs and verifies the selected character and session before
resuming eligible holds. Stop search does not locate a new allocation until Find
is used again or an unverified stat is enabled while connected. Stop Freeze leaves
configured targets intact. Use Resume Freeze to reapply enabled holds.

## Validation

Stat values can persist until the game recalculates them or the run ends.

Originally verified executable SHA-256:
`ab7b6332a6c47e4c29188e44932939e560b274cf06e0f212cb3ada119a3a3d35`.
The trainer records the executable hash and checks discovered instruction signatures, process identity,
the selected character and session, and verifies changes by reading them back.
Unverified or ambiguous field mappings are blocked. Player discovery reads thread stacks without hooks.
No game executable files are patched. Separate from the original game's trainer.

## Development

```powershell
pip install -r requirements.txt
python trainer.py
python -m unittest discover -s . -p "test_*.py"
pip install pyinstaller
python build_exe.py
```

The portable ZIP includes the standalone executable, README, and MIT license.

### Selected-player validation and host sessions

Hunt verifies the selected character's live identity string using corroborating code proofs. Names are read dynamically; no test character name is hardcoded. The scan resumes at the exact candidate after its probe budget is exhausted, so dense stacks do not hide the active player array.

Host detection follows the game's live lobby role. A verified host session allows
changes to the host's selected character. Guest and unverified sessions stay
read-only, even if the selected character is the only player found.

Role discovery runs read-only in the background when the game is detected. A cold scan or a UI runtime reload may take longer on processes with large memory footprints; the UI stays responsive and stat writes wait for verification. A process-bound address cache is always revalidated against the live Lua main thread, global environment and current lobby table. Runtime, PID, global-table and lobby changes invalidate old ownership. Role is checked again before every stat write. Find still starts stat discovery and automatic max/freeze.

The reader validates the game's Lua 5.1 runtime object layout. An unsupported runtime stays unknown/read-only; future game versions are not guaranteed compatible.

**Applying; verifying** means the write was read back immediately. **Frozen** requires a subsequent observation at the requested value, with the same player and code proofs. A recalculated value returns to applying status.
