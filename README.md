# Azeroth Survivor Reborn Trainer

Standalone Windows x64 trainer for [Azeroth Survivors: Reborn](https://azreborn.com/),
written in Rust. [Download the latest standalone EXE](https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases/latest)

## Use the trainer

1. Run **Azeroth.Survivor.Reborn.Trainer.exe**, then start Reborn.
2. Choose the stats in the **Enabled** column and set their targets in the side panel.
3. Start a solo run or host a co-op run, then click **Find**. The trainer applies each enabled target once after verifying the selected character and session.
4. Use **Apply** to set the selected stat again. Use **Find** to apply all enabled targets again.

### Game updates and session checks

**Find** looks for your character on newer builds. Stat writes use the verified
layout from build `ab1d6c071b56`. A different executable fingerprint can use
that layout if every byte apart from the PE timestamp and checksum matches and
the live checks pass. Changes to game code or data require a verified mapping.
Stat writes require a verified solo or co-op host session.

The first role check scans memory to locate the live lobby. Repeat hunts in the
same process can revalidate the cached proof. If Windows reports a partial read
(error 299), the scanner retries page by page. If a page remains unreadable,
the trainer retries session verification. Use **Cancel Apply** to stop retries. Click **Find** again if the
selected character or lobby role changes.

Current health follows verified maximum health. Echo requires the active Echo effect.

### Troubleshooting

Open **Advanced → Detection details** and click **Copy details** to copy the game
build, process, session state, last Apply error, and per-stat readings and targets.
The background log includes search, session check, update, and stat events at
`%LOCALAPPDATA%\Azeroth Survivor Reborn Trainer\reborn-rust.log`.
At 4 MiB the log rotates to `reborn-rust.previous.log`. If settings are malformed,
the trainer preserves the original and loads defaults so changes can still be saved.

```powershell
Get-Content -LiteralPath "$env:LOCALAPPDATA\Azeroth Survivor Reborn Trainer\reborn-rust.log" -Tail 40 -Wait
```

## Updates

The trainer checks GitHub when it starts. **Check updates** shows **Using Latest**
when the installed version is current. When **Update Available** appears, click
**Download & Install** to verify the update, replace the EXE, and reopen the trainer.
The installation folder needs to be writable. The updater keeps the previous EXE
as a backup. Starting with v1.0.1, the updater waits for the replacement to
confirm that its UI initialized. If startup fails, it restores and reopens the
previous EXE.

## Repository

- `src/` contains the Rust trainer, stat verification, Windows integration, and updater.
- `assets/` contains the app icon and logo.
- `.github/workflows/release.yml` builds and tests on Windows and publishes the standalone EXE for newer versions on `main`.

The Python trainer remains available in the [v0.9.19 release and source tag](https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases/tag/v0.9.19).
See [LICENSE](LICENSE).
