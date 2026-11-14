Self-update restart fix for Azeroth Survivor Reborn Trainer v0.6.4.

- Fixes the missing python314.dll startup error after an in-place trainer update.
- Resets the PyInstaller environment and removes inherited temporary-runtime metadata before launching the update helper.
- The helper also explicitly requests a fresh Python runtime for the restarted executable.
- Adds a real frozen executable replacement/restart smoke test to the release workflow, alongside 55 automated tests.
- Preserves the previous executable, settings and save backups.

Includes v0.6.3 compatibility support for Reborn build a02914e85f9b, its relocated
configuration and player fields, and all previous Run/Stop, Find Hero and stat controls.

If an update from v0.6.0 through v0.6.3 ends with a Python DLL error, dismiss it
and open the trainer executable normally once. Its downloaded executable is intact;
v0.6.4 fixes subsequent update restarts. Future game layouts still require validation.
Official game: https://azreborn.com/
