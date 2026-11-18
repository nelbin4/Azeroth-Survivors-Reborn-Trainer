Azeroth Survivor Reborn Trainer v0.9.18

- Keep the trainer responsive when a settings write, render, or status update fails: the 500 ms heartbeat now logs the error and keeps running.
- Store settings, logs and the role cache under %LOCALAPPDATA% so installs in Program Files or read-only folders start and save correctly.
- Retry transient Hunt discovery failures (file locks, updates, antivirus) after a short backoff instead of disabling Hunt until the game image changes; Find clears cached discovery errors.
- Only hold stats that are currently verified, so a character change cannot keep writing stale fields.
- Reject one-time targets for Current health and disable its Apply button; current health always follows verified maximum health.
- Clear queued one-time targets when the session role makes writes impossible instead of showing "Queued" forever.
- Show the last role-scan error in Advanced detection details and throttle repeated role scans.
- Guard against an empty game-module map with a clear reconnect message.
- Scan the complete manual value-search address space instead of silently truncating at 100,000 regions.
- Validate the update download redirect before creating the file, ignore reused PIDs while waiting for the trainer to exit, and roll back the previous executable if the new build exits immediately.
- Handle a missing logo, non-standard DPI scaling, and window resizing without crashing or clipping.
