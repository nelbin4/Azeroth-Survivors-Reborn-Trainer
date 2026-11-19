Azeroth Survivor Reborn Trainer v1.0.1

- Wait for the replacement trainer to confirm UI initialization during updates and restore the previous EXE if startup fails.
- Pause and retry temporary freeze verification failures. Incomplete role scans keep writes paused.
- Include the detected build in Hunt diagnostics and show which build supports writes.
- Recover from malformed settings while preserving the original file.
- Rotate the background log at 4 MiB and retain one previous log.
- Stat writes still support game build ab1d6c071b56.
