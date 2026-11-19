# Trainer releases

The first stable, fully Rust trainer release is `1.0.0` (Git tag `v1.0.0`).
For routine releases, increment the patch number from 0 through 9. After patch
9, increment the minor number and reset the patch to 0. For example, release
`1.1.0` after `1.0.9`.

Keep the full Rust source, tests, Cargo manifest and lockfile, and build
workflow in this GitHub repository. Starting with `v1.0.0`, GitHub Actions
builds and publishes one standalone Windows x64 EXE as the installable
release asset. The EXE must run without companion files and handle update
installation through the same binary. Keep the executable filename and
SHA-256 verification compatible with the existing updater.

Use GitHub Actions for all builds, tests, and release packaging of the Rust
trainer. Local formatting and lockfile generation are fine. Download the
workflow artifact for Windows verification.

When a requested trainer change increments the version, run the tests,
commit the related changes, and push the version to `origin/main`. Verify
that the workflow tags and publishes the matching release and that the
updater recognizes its executable. Before the Rust cutover, the Python
workflow still reads `VERSION` in `trainer.py` and uploads the EXE, ZIP, and
checksums.

Preserve unrelated local changes and publish only the intended trainer source. Do not include local saves, settings, diagnostics, caches, or build artifacts in the commit.
