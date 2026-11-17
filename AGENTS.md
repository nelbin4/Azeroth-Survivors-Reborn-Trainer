# Trainer releases

When a requested trainer change results in a newer local `VERSION` in `trainer.py`, run the tests, commit the related changes, and push the version to `origin/main` as part of completing the task. Check that the GitHub release workflow publishes the matching version and that the updater recognizes its executable. The workflow creates the version tag and uploads the EXE, ZIP, and checksums.

Preserve unrelated local changes and publish only the intended trainer source. Do not include local saves, settings, diagnostics, caches, or build artifacts in the commit.
