Frontend and backend improvements for Azeroth Survivor Reborn Trainer.

- Wider, resizable interface with Offense, Defense, Utility and Run Resources cards.
- Improved dark theme, aligned live/target columns, visible Max and Pause holds actions, and mouse-wheel scrolling.
- Background hero discovery with an isolated read-only process connection.
- Rejects stale discovery results after restart, game selection, Restore or shutdown.
- Bounded reconnection delays and clear disconnected/paused status.
- Clears stale readings and hold indicators after a lost hero or process.
- 30 automated tests, including retry scheduling and stale session rejection.

Retains v0.4.1 pattern-based layout discovery and all existing player, native and
save-backup controls. Future structural changes and unverified save formats remain
blocked. Official game: https://azreborn.com/
