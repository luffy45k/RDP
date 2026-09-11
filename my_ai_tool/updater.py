"""Self-Update & Build System.

check_updates():
    1. git fetch origin <branch>
    2. count commits we are behind
    3. git pull --rebase --autostash  (keeps local self-heal commits, stashes
       uncommitted changes, rebases them on top)
    4. on rebase conflict -> abort, keep old code (never brick the tool)

The tool restarts itself with os.execv so the daemon immediately runs the
new code. Data lives in ~/.my_ai_tool, so an update can never touch it.
"""
from __future__ import annotations

import os
import subprocess
import time

from . import __version__, paths
from . import db
from .logging_setup import get_logger

log = get_logger("updater")


class UpdateError(RuntimeError):
    pass


def _git(code_dir: str, *args: str, timeout: int = 120) -> tuple:
    p = subprocess.run(["git", "-C", code_dir, *args], capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def current_version() -> str:
    code_dir = str(paths.code_dir())
    rc, out, _ = _git(code_dir, "rev-parse", "--short", "HEAD")
    if rc == 0:
        return f"{__version__}+{out}"
    return __version__


def check_updates(force: bool = False, apply: bool = True) -> dict:
    """Returns dict(status=..., detail=..., before=?, after=?)."""
    code_dir = str(paths.code_dir())
    db.meta_set("last_update_check", str(int(time.time())))

    rc, _, _ = _git(code_dir, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return {"status": "not_a_git_repo",
                "detail": f"{code_dir} is not a git repo — self-update disabled"}

    cfg_branch = "main"
    try:
        from . import config
        cfg_branch = config.load_config().get("update", {}).get("branch", "main")
    except Exception:
        pass

    remote = os.environ.get("MYTOOL_UPDATE_REMOTE", "origin")
    rc, _, err = _git(code_dir, "fetch", remote, cfg_branch, timeout=300)
    if rc != 0:
        return {"status": "error", "detail": f"git fetch failed: {err}"}

    rc, behind, _ = _git(code_dir, "rev-list", "--count", f"HEAD..FETCH_HEAD")
    if rc != 0:
        return {"status": "error", "detail": "could not compare versions"}

    before = current_version()
    if behind == "0":
        db.meta_set("last_update_result", "up_to_date")
        return {"status": "up_to_date", "detail": f"already at latest ({before})",
                "before": before}

    if not apply:
        return {"status": "available", "detail": f"{behind} new commit(s) available",
                "before": before}

    log.info("updating: %s commit(s) behind", behind)
    rc, out, err = _git(code_dir, "pull", "--rebase", "--autostash",
                        remote, cfg_branch, timeout=600)
    if rc != 0:
        _git(code_dir, "rebase", "--abort")   # best-effort cleanup
        db.meta_set("last_update_result", "error")
        return {"status": "error",
                "detail": f"update failed, old code kept intact: {err or out}"}

    after = current_version()
    db.meta_set("last_update_result", "updated")
    db.meta_set("last_update_before", before)
    log.info("updated %s -> %s", before, after)
    return {"status": "updated", "detail": f"{before} -> {after}",
            "before": before, "after": after}


def maybe_check_update() -> str:
    """Throttled auto check used by cron-tick / daemon. Returns action string."""
    try:
        from . import config
        cfg = config.load_config().get("update", {})
    except Exception:
        return "check_failed"
    if not cfg.get("auto", True):
        return "disabled"
    interval = int(cfg.get("check_interval_min", 60)) * 60
    last = db.meta_get("last_update_check", "0")
    try:
        if time.time() - float(last) < interval:
            return "throttled"
    except ValueError:
        pass
    result = check_updates(apply=True)
    log.info("update check: %s (%s)", result["status"], result["detail"])
    return result["status"]  # up_to_date | updated | available | error | ...


def restart_self(extra_args: list | None = None) -> None:
    """Re-exec this process so updated/healed code takes effect immediately."""
    log.info("restarting tool with fresh code: %s", extra_args or [])
    code_dir = str(paths.code_dir())
    env = dict(os.environ)
    env["MYTOOL_CODE_DIR"] = code_dir
    env["PYTHONPATH"] = code_dir + os.pathsep + env.get("PYTHONPATH", "")
    args = [sys_executable(), "-m", "my_ai_tool", *(extra_args or [])]
    os.execve(sys_executable(), args, env)


def sys_executable() -> str:
    return os.environ.get("MYTOOL_PYTHON") or __import__("sys").executable
