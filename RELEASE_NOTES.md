Compatibility fix for Azeroth Survivor Reborn Trainer v0.6.3.

- Supports Reborn executable build a02914e85f9b and its moved player/configuration layout.
- Accepts verified 8-byte configuration alignment with independent initialization-state and clock-speed code corroboration.
- Handles both RDI and RBX variants of the run serializer while requiring every address to use the same hero register.
- Verifies the new name, health, XP, stat and reroll layout before enabling memory controls.
- Retains support for the two previous layout families and rejects conflicting configuration blocks, inconsistent registers and changed structural fields.
- Shows the number of verified configuration blocks in detection failures.
- 53 automated tests pass, with additional read-only validation of configuration and solo-hero fields in the running a02914e85f9b build.

Includes the existing Run/Stop controls, large Find Hero button, automatic Max
all stats after solo-hero verification, and verified opt-in GitHub updater.
Future structural changes cannot be guaranteed compatible. Save-format and
cloud-save restrictions are unchanged. Official game: https://azreborn.com/
