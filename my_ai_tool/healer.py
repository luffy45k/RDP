"""Auto Bug-Fix (Self-Healing Logic).

The full pipeline — "All" flows supported:

  1. CATCH        every CLI/daemon entry point is wrapped: an exception is
                  captured (type, message, traceback, last log lines) and
                  saved as a crash report in ~/.my_ai_tool (DB + markdown).
  2. LOG          the crash stays 'open' in the database — nothing is lost.
  3. NEXT-RUN     cron-tick / daemon picks up open crashes automatically, or
                  you run `mytool heal` manually.
  4. DIAGNOSE     traceback + suspect source file are sent to the LLM, which
                  returns {analysis, file_to_fix, full_corrected_code}.
  5. APPLY        original file is backed up to ~/.my_ai_tool/backups, then
                  the corrected file is written.
  6. VERIFY       py_compile + module --heal-verify self-check + core
                  selftest must ALL pass.
  7. COMMIT/ROLLBACK  verified -> git autocommit "self-heal(crash-N)" and the
                  daemon restarts itself; any verification failure -> instant
                  rollback from the backup, crash stays open for another try.
  8. REPORT-ONLY  heal.mode=report generates an analysis report without
                  touching any code.
  9. GUARDS       max attempts per crash, path-escaping protection, and
                  .py-only targets keep self-healing safe.
"""
from __future__ import annotations

import datetime
import os
import pathlib
import py_compile
import re
import shutil
import subprocess
import sys
import traceback as tb_mod

from . import brain, config, db, paths
from . import updater
from .logging_setup import get_logger, setup_logging

log = get_logger("healer")


class HealError(RuntimeError):
    pass


# ------------------------------------------------------------------ 1. CATCH

def _guess_source_file(tb_text: str) -> str:
    """Relative path (to code dir) of the last project frame in the traceback."""
    code_dir = str(paths.code_dir())
    hit = None
    for m in re.finditer(r'File "([^"]+)"', tb_text):
        path = m.group(1)
        if not os.path.isabs(path):
            continue  # skip <frozen importlib...> and other pseudo frames
        try:
            real = os.path.realpath(path)
            rel = os.path.relpath(real, os.path.realpath(code_dir))
        except ValueError:
            continue
        if not rel.startswith(".."):
            hit = rel.replace(os.sep, "/")   # keep LAST matching project frame
    return hit or "unknown"


def record_crash(exc: BaseException, context: str = "") -> tuple:
    """Store crash in DB + markdown report. Returns (crash_id, report_path)."""
    setup_logging()
    tb_text = tb_mod.format_exc()
    crash_type = type(exc).__name__
    message = str(exc)
    source_file = _guess_source_file(tb_text)

    now = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = str(paths.crash_dir() / f"crash-{now}-{os.getpid()}.md")
    version = updater.current_version()
    tail = _log_tail()

    md = [
        f"# Crash report #{now} — {crash_type}",
        f"- **time:** {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"- **version:** {version}",
        f"- **context:** {context or 'n/a'}",
        f"- **suspect file:** `{source_file}`",
        f"- **message:** {message}",
        "",
        "## Traceback",
        "```",
        tb_text.strip(),
        "```",
        "",
        "## Last log lines",
        "```",
        tail,
        "```",
    ]
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")
    except OSError:
        report_path = ""

    crash_id = db.record_crash(crash_type, message, tb_text, source_file,
                               report_path)
    log.error("crash #%s recorded: %s: %s (file=%s)", crash_id, crash_type,
              message, source_file)
    return crash_id, report_path


def _log_tail(lines: int = 40) -> str:
    try:
        with open(paths.log_file(), "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip() or "(empty)"
    except OSError:
        return "(no log file)"


# ------------------------------------------------------- 4-7. DIAGNOSE..END

def _build_prompt(crash) -> str:
    code_dir = paths.code_dir()
    rel = crash["source_file"] or "unknown"
    target = (code_dir / rel)
    if not target.exists():
        raise HealError(f"suspect file not found: {rel}")
    code = target.read_text(encoding="utf-8", errors="replace")
    if len(code) > 200_000:
        raise HealError(f"{rel} is too large to auto-heal")
    return (
        f"CRASH_TYPE: {crash['crash_type']}\n"
        f"CRASH_MESSAGE: {crash['message']}\n"
        f"SUSPECT_FILE: {rel}\n\n"
        f"CURRENT_CODE:\n```python\n{code}\n```\n\n"
        f"CRASH_TRACEBACK:\n```\n{crash['traceback'][:4000]}\n```"
    )


def _resolve_target(raw: str):
    """Path-safety: the fix may only touch existing .py files inside the repo."""
    code_dir = paths.code_dir()
    p = Path_abs(raw, code_dir)
    try:
        p.relative_to(code_dir)
    except ValueError:
        raise HealError(f"refusing to touch file outside code dir: {raw}")
    if p.suffix != ".py":
        raise HealError(f"only .py files can be auto-healed: {raw}")
    if not p.exists():
        raise HealError(f"target does not exist: {p}")
    return p, p.relative_to(code_dir).as_posix()


def _backup(target, rel: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    flat = rel.replace("/", "__")
    dst = paths.backup_dir() / f"{flat}.{stamp}.bak"
    shutil.copy2(target, dst)
    return str(dst)


def _verify(target) -> tuple:
    """py_compile + optional file --heal-verify selfcheck + core selftest."""
    py = sys.executable if os.name != "nt" else sys.executable
    env = dict(os.environ)
    env["MYTOOL_CODE_DIR"] = str(paths.code_dir())
    env["PYTHONPATH"] = env["MYTOOL_CODE_DIR"] + os.pathsep + env.get("PYTHONPATH", "")

    p = subprocess.run([py, "-m", "py_compile", str(target)],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return False, f"py_compile failed:\n{p.stderr[-1500:]}"

    p = subprocess.run([py, str(target), "--heal-verify"],
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        unknown_arg = p.returncode == 2 and re.search(
            r"unrecognized|invalid choice|no such option", p.stderr or "", re.I)
        if not unknown_arg:
            return False, f"--heal-verify failed:\n{(p.stderr or p.stdout)[-1500:]}"

    p = subprocess.run([py, "-m", "my_ai_tool", "selftest", "--core"],
                       capture_output=True, text=True, timeout=180, env=env,
                       cwd=str(paths.code_dir()))
    if p.returncode != 0:
        return False, f"core selftest failed:\n{(p.stdout or p.stderr)[-1500:]}"
    return True, "all verifications passed"


def _git_autocommit(rel: str, crash_id: int, analysis: str) -> None:
    code_dir = str(paths.code_dir())
    try:
        rc, _, _ = updater._git(code_dir, "rev-parse", "--is-inside-work-tree")
        if rc != 0:
            return
        updater._git(code_dir, "add", "--", rel)
        msg = f"self-heal(crash-{crash_id}): {analysis.splitlines()[0][:80]}"
        updater._git(code_dir, "commit", "-m", msg, "--", rel)
    except Exception as e:  # never fail healing because of git
        log.warning("git autocommit skipped: %s", e)


def heal_crash(crash) -> str:
    """Returns 'fixed' | 'reported' | 'failed' | 'skipped'."""
    crash_id = crash["id"]
    max_attempts = int(config.load_config().get("heal", {})
                       .get("max_attempts_per_file", 3))
    if crash["attempts"] >= max_attempts:
        db.update_crash(crash_id, status="failed")
        log.warning("crash #%s exceeded max attempts — marked failed", crash_id)
        return "skipped"

    mode = config.load_config().get("heal", {}).get("mode", "apply")
    prompt = _build_prompt(crash)
    resp = brain.chat([{"role": "system", "content": brain.HEALER_SYSTEM},
                       {"role": "user", "content": prompt}])

    analysis = str(resp.get("analysis", "")).strip() or "(no analysis)"
    target, rel = _resolve_target(str(resp.get("file_to_fix", "")))
    new_code = resp.get("full_corrected_code")
    if not isinstance(new_code, str) or not new_code.strip():
        raise HealError("LLM returned no full_corrected_code")

    if mode == "report":
        path = _write_heal_report(crash, analysis, rel, new_code)
        db.update_crash(crash_id, status="reported")
        log.info("crash #%s: report-only mode, report at %s", crash_id, path)
        print(f"\U0001f4c4 report-only mode: analysis saved to {path} (code NOT touched)")
        return "reported"

    backup = _backup(target, rel)
    target.write_text(new_code, encoding="utf-8")
    log.info("crash #%s: patch applied to %s (backup %s)", crash_id, rel, backup)

    ok, detail = _verify(target)
    db.record_fix(crash_id, rel, backup, applied=True, verified=ok,
                  analysis=analysis)

    if ok:
        db.update_crash(crash_id, status="fixed")
        _git_autocommit(rel, crash_id, analysis)
        print(f"\u2705 self-heal OK: {rel} fixed (crash #{crash_id}). Backup: {backup}")
        return "fixed"

    # VERIFY FAILED -> instant rollback
    shutil.copy2(backup, target)
    attempts = crash["attempts"] + 1
    status = "failed" if attempts >= max_attempts else "open"
    db.update_crash(crash_id, status=status, attempts=attempts)
    log.error("crash #%s: verification failed, rolled back (%s). attempts=%s",
              crash_id, detail.splitlines()[0], attempts)
    print(f"\u26a0\ufe0f verification failed for {rel} — rolled back from backup."
          f" ({attempts}/{max_attempts} attempts used)")
    return "failed"


def _write_heal_report(crash, analysis: str, rel: str, code: str) -> str:
    now = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = paths.report_dir() / f"heal-report-crash-{crash['id']}-{now}.md"
    md = (f"# Heal report — crash #{crash['id']}\n\n"
          f"- **suspect file:** `{rel}`\n- **crash:** {crash['crash_type']}: "
          f"{crash['message']}\n\n## AI analysis\n\n{analysis}\n\n"
          f"## Suggested fixed code (NOT applied — heal.mode=report)\n\n"
          f"```python\n{code}\n```\n")
    path.write_text(md, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------- entrypoints

def heal_open_crashes(quiet: bool = False) -> dict:
    """Heal every open crash (next-run flow). Returns counts."""
    counts = {"fixed": 0, "failed": 0, "reported": 0, "skipped": 0}
    for crash in db.open_crashes():
        try:
            result = heal_crash(crash)
        except (brain.BrainError, HealError) as e:
            log.warning("crash #%s heal attempt failed: %s", crash["id"], e)
            if not quiet:
                print(f"\u26a0\ufe0f crash #{crash['id']}: {e}")
            result = "failed"
        except Exception:
            log.exception("crash #%s heal crashed", crash["id"])
            result = "failed"
        counts[result] = counts.get(result, 0) + 1
    return counts


def heal_latest() -> str:
    crash = db.latest_open_crash()
    if not crash:
        return "no_open_crashes"
    return heal_crash(crash)


def handle_crash_cli(exc: BaseException, context: str) -> int:
    """Called by cli.main() around every command — flow: catch -> report ->
    (config heal.auto) immediate standard-flow fix, else next-run healing."""
    crash_id, report_path = record_crash(exc, context)
    print(f"\n\u274c Crash captured: #{crash_id} {type(exc).__name__}: {exc}")
    if report_path:
        print(f"   full report: {report_path}")

    cfg = config.load_config().get("heal", {})
    if cfg.get("auto", True) and cfg.get("mode", "apply") == "apply":
        print("\U0001fa79 attempting standard self-heal flow (LLM -> patch -> verify)...")
        try:
            result = heal_latest()
        except Exception as e:
            print(f"   heal attempt failed: {e}")
            result = "failed"
        if result == "fixed":
            print("   Fixed automatically! Re-run your command now.")
            return 0
        if result == "no_open_crashes":
            return 1
        print("   Could not fix right now — crash stays logged; cron/daemon"
              " will retry on the next run (`mytool heal` to force).")
    else:
        print("   heal.auto is off / report mode — run `mytool heal` or check"
              " ~/.my_ai_tool/reports/")
    return 1
