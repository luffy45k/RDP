"""Auto-Run: daemon loop + cron one-shot tick, with a lock against overlap.

    mytool cron-tick            -> run once and exit (cron job calls this)
    mytool daemon --interval 15 -> stay alive, tick every 15 minutes

Each tick:  check updates -> heal open crashes -> run queued tasks.
After an update or a successful heal the daemon re-execs itself so the
new/healed code takes effect immediately.
"""
from __future__ import annotations

import os
import sys
import time

from . import config, db, paths
from .logging_setup import get_logger

log = get_logger("scheduler")


class _Lock:
    def __init__(self):
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
            self.fh = open(paths.lock_file(), "w")
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, AttributeError):
            pass  # platform without fcntl (Windows): run without lock
        except BlockingIOError:
            raise Busy()
        return self

    def __exit__(self, *a):
        try:
            if self.fh:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
        except Exception:
            pass
        return False


class Busy(RuntimeError):
    pass


def tick(quiet: bool = False) -> list:
    """One full background cycle. Returns list of action strings."""
    actions = []
    try:
        with _Lock():
            actions = _tick_locked(quiet)
    except Busy:
        log.info("another tick is already running — skipping this one")
        if not quiet:
            print("another mytool tick is already running — skipping")
    return actions


def _tick_locked(quiet: bool) -> list:
    os.chdir(os.path.expanduser("~"))
    actions = []

    from . import updater
    status = updater.maybe_check_update()
    if status == "updated":
        actions.append("updated")
    elif status not in ("up_to_date", "throttled", "disabled"):
        actions.append(f"update_{status}")

    from . import healer, runner
    cfg = config.load_config().get("heal", {})
    if cfg.get("auto", True):
        counts = healer.heal_open_crashes(quiet=quiet)
        if any(counts.values()):
            actions.append(f"healed:{counts}")
            if counts.get("fixed"):
                actions.append("code_changed")

    ran = runner.run_queued(quiet=quiet)
    if ran:
        actions.append(f"tasks_run:{ran}")

    if actions:
        log.info("tick actions: %s", actions)
    return actions


def cron_tick(quiet: bool = False) -> int:
    actions = tick(quiet=quiet)
    if not quiet and actions:
        print("cron-tick:", ", ".join(str(a) for a in actions))
    return 0


def daemon(interval_min: int | None = None) -> int:
    cfg = config.load_config().get("schedule", {})
    interval = int(interval_min or cfg.get("interval_min", 15))
    log.info("daemon started, interval=%s min, pid=%s", interval, os.getpid())
    print(f"\U0001f9ff mytool daemon running (every {interval} min, pid {os.getpid()})"
          " — Ctrl+C to stop")
    while True:
        start = time.time()
        try:
            actions = tick(quiet=False)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # the daemon must never die: log crash, self-heal machinery handles it
            from . import healer
            healer.record_crash(e, context="daemon-tick")
            log.exception("tick failed")
            actions = []
        if "updated" in actions or "code_changed" in actions:
            print("\U0001f504 code changed — restarting with fresh code...")
            time.sleep(1)
            updater.restart_self(["daemon", "--interval", str(interval)])
        sleep_s = max(5.0, interval * 60 - (time.time() - start))
        try:
            time.sleep(sleep_s)
        except KeyboardInterrupt:
            print("\ndaemon stopped")
            return 0
