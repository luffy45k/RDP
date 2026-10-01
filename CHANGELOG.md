# Changelog

## 1.1.0 — hardening release

The feature set from 1.0.0 is unchanged; this release makes the three
self-* loops actually hold up in practice, and adds the test suite + CI that
prove it.

### Fixed

- **Self-healing could never fix the tool's own code.** The VERIFY step ran
  the patched file as a plain script (`python3 my_ai_tool/runner.py
  --heal-verify`), which always died with
  `ImportError: attempted relative import with no known parent package`.
  Every correct fix to any module inside the package was therefore rolled
  back, and only the standalone `examples/broken.py` demo ever worked.
  Package modules are now launched with `python -m my_ai_tool.<module>`, the
  `--heal-verify` self-check is skipped for files that do not provide one,
  and an explicit import of the patched module was added to the verification
  chain (`py_compile` → import → `--heal-verify` → core selftest).

- **The daemon crashed exactly when it was supposed to restart.**
  `scheduler.daemon()` called `updater.restart_self(...)` but `updater` was
  only imported inside `_tick_locked()`, so the moment an update or a
  successful self-heal reported `code_changed` the daemon died with
  `NameError: name 'updater' is not defined`.

- **The vault archive grew forever and accumulated duplicate members.**
  A file written by the AI that already existed in the archive (but had not
  been extracted for that task) was repacked as a *new* member, so the zip
  ended up with two — then three, then four — entries of the same name. The
  repack now de-overlaps the replace/add/delete buckets (delete wins, then
  replace), drops stale duplicates left by older versions, and the router
  reports such a write as `modified` instead of `added`.

- **Zip-bomb cap is enforced while decompressing** instead of after the whole
  member has been read into memory.

- **Leaked file descriptor** in the scheduler lock when another tick already
  held it.

- `crash-test --reset` now restores `examples/broken.py` byte-for-byte to its
  committed content, so a demo run no longer leaves a dirty git tree.

- Removed the stray empty `README.main` file and a handful of unused imports.

### Added

- `tests/` — 126 stdlib `unittest` tests covering config/paths/db, the brain
  (including the real HTTP path against the dev fake-Ollama), the safety
  blocklist, the runner feedback loop, the vault guards and repack
  transaction, the full healer apply→verify→rollback cycle, the updater's
  git rebase/conflict/throttling behaviour, the scheduler and the CLI —
  plus an end-to-end `crash-test --demo` run in a real subprocess and a real
  git repo. Regression tests cover each bug listed above.
- `run_tests.sh` — one-command suite + core selftest in an isolated data dir.
- `.github/workflows/ci.yml` — tests on Python 3.9/3.11/3.12, plus a job that
  installs the tool and runs the self-heal and vault demos end to end.
- README: development/testing section and the supported environment
  variables.

## 1.0.0 — initial release

Terminal AI agent with persistent data (`~/.my_ai_tool`), Ollama/OpenAI/mock
brains, safety-checked command runner, git self-update, self-healing crash
pipeline, cron/systemd/daemon auto-run, one-command Ollama + Hermes setup, and
the lazy-loading vault archive.
