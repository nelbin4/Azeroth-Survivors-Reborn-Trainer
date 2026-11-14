Compatibility and detection update for Azeroth Survivors: Reborn.

- Supports the newer d76345ce7a4e build alongside the original 0.6.3 executable.
- Discovers live configuration addresses and player layout fields from unique code patterns.
- Compatible updates within verified layout families no longer need a hash allowlist entry.
- Groups duplicate stack references by hero allocation and identity; distinct heroes stay blocked.
- Caches layout discovery with file identity and bounded freshness; skips unchanged Hold writes.
- Preserves live instruction, process, solo-player and readback checks.
- 28 automated tests; live damage and God-mode write/restore checks passed on the newer build.

Unknown structural changes remain blocked. Permanent save upgrades still require
the separately verified original save format/build; this release does not enable
cloud overwrites or unverified newer save editing.

Official game: https://azreborn.com/
