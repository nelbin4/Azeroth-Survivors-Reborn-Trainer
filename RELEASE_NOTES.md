Game launch and exact process controls for Azeroth Survivor Reborn Trainer v0.6.2.

- Larger, prominent Find Hero button that automatically applies Max all stats, God mode and XP assistance after solo-hero validation.
- Replaces the primary Locate game action with Run Azeroth Survivors Reborn.
- Saves the selected executable and launches it from its installation folder.
- Pins the launched PID, creation time and executable path for background attachment and hero discovery.
- Disables Run whenever Reborn is already running, including unsupported or inaccessible processes.
- Displays the detected PID and adds Stop game with a PID/path confirmation and unsaved-progress notice.
- Stops holds and restores eligible changes before terminating only the validated process.
- Rejects PID reuse, ambiguous instances and stale connection results after changing the executable or stopping the game.
- Retains Game executable as a secondary path-selection action and keeps launch/stop detection independent of memory-layout compatibility.
- 47 automated tests passed, plus native launch, stop, cancellation and button-state checks using an isolated disposable executable.

Retains the Compact Companion interface, verified opt-in GitHub updater and all
existing stat, native-option and save-backup features. No cloud-save behavior or
memory-layout compatibility restrictions are changed. Official game: https://azreborn.com/
