"""Smoke tests — `mytool selftest` (full) / `mytool selftest --core` (no LLM).

The self-healing verifier runs the CORE suite so a broken LLM endpoint can
never cause a good code fix to be rolled back.
"""
from __future__ import annotations

import sys

from . import config, db, paths
from . import updater


def run(core: bool = False) -> tuple:
    checks = []

    def check(name, fn, critical=True):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"exception: {e}"
        checks.append((name, ok, detail, critical))

    def _paths():
        d = paths.data_dir()
        probe = d / "logs" / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, str(d)

    def _db():
        tid = db.record_task("selftest-probe", status="running")
        db.set_task(tid, status="done", result_summary="ok")
        row = db.get_task(tid)
        db.meta_set("selftest", "1")
        assert row and row["status"] == "done"
        return True, f"sqlite ok ({paths.db_path()})"

    def _config():
        cfg = config.load_config()
        assert "provider" in cfg
        return True, f"provider={cfg.get('provider')}"

    def _imports():
        from . import brain, healer, runner, scheduler, updater  # noqa: F401
        return True, "all modules import cleanly"

    def _git():
        v = updater.current_version()
        return True, f"code version {v}"

    def _brain():
        from . import brain as b
        ok, detail = b.ping()
        return ok, detail

    check("data-dir-writable", _paths)
    check("database", _db)
    check("config", _config)
    check("module-imports", _imports)
    check("git-version", _git)
    if not core:
        check("llm-reachable", _brain, critical=False)

    ok_all = all(ok for name, ok, _, critical in checks if critical)
    return ok_all, checks


def main(core: bool = False) -> int:
    ok, checks = run(core=core)
    print(f"mytool selftest ({'core' if core else 'full'}):")
    for name, ok_i, detail, critical in checks:
        icon = "\u2705" if ok_i else ("\u26a0\ufe0f " if not critical else "\u274c")
        print(f"  {icon} {name:20s} {detail}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1
