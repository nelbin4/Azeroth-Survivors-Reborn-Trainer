# Azeroth Survivor Reborn Trainer

Standalone Windows x64 trainer for [Azeroth Survivors: Reborn](https://azreborn.com/),
written in Rust. [Download the latest standalone EXE](https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases/latest)

## Use the trainer

1. Run **Azeroth.Survivor.Reborn.Trainer.exe**, then start Reborn.
2. Choose the stats in the **Enabled** column and set their targets in the side panel.
3. Start a solo run or host a co-op run, then click **Find**. The trainer applies and holds the enabled targets after verifying the selected character and session.
4. Use **Stop Freeze** to stop automatic writes. **Apply** sets the selected target once.

The Rust stat mappings currently support game build `ab1d6c071b56`. Unverified
builds and co-op guests have read-only diagnostics. Current health follows
verified maximum health. Echo requires the active Echo effect.

**Advanced → Detection details** shows the detected game and character. The
trainer logs session checks, searches, and stat changes to
`%LOCALAPPDATA%\Azeroth Survivor Reborn Trainer\reborn-rust.log`.

```powershell
Get-Content -LiteralPath "$env:LOCALAPPDATA\Azeroth Survivor Reborn Trainer\reborn-rust.log" -Tail 40 -Wait
```

## Updates

The trainer checks GitHub when it starts. **Check updates** shows **Using Latest**
when the installed version is current. When **Update Available** appears, click
**Download & Install** to verify the update, replace the EXE, and reopen the trainer.
The installation folder needs to be writable. The updater keeps the previous EXE
as a backup.

## Repository

- `src/` contains the Rust trainer, stat verification, Windows integration, and updater.
- `assets/` contains the app icon and logo.
- `.github/workflows/release.yml` builds and tests on Windows and publishes the standalone EXE for newer versions on `main`.

The Python trainer remains available in the [v0.9.19 release and source tag](https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases/tag/v0.9.19).
See [LICENSE](LICENSE).
