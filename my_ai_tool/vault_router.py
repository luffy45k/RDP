"""AI Command Router for the Vault — 'library system' brain.

    mytool vault-run "analyze the user data"

Flow (exactly the spec):
  1. INDEX      read the archive's file list (+ cached AI purposes)
  2. ROUTE      LLM reads TASK + INDEX -> returns {needed_files, commands}
  3. LAZY LOAD  extract ONLY those files into /dev/shm (RAM) scratch dir
  4. EXECUTE    run commands there (safety-checked, timeout, feedback loop —
                the AI can modify/patch the extracted files to fix bugs)
  5. DIFF       sha256 before/after -> which files changed / new / deleted
  6. REPACK     update exactly those members back into the archive (atomic,
                with backup) — the rest of the archive stays compressed
  7. CLEANUP    scratch dir deleted immediately (finally block)
"""
from __future__ import annotations

import os
import re
import sys

from . import brain, config, db, vault
from .logging_setup import get_logger
from .runner import _run_one, _trim, safety_check

log = get_logger("vault_router")

PLANNER_SYSTEM = """You are the vault router of 'mytool', an autonomous agent on Linux.
A compressed archive (the vault) holds the project's scripts, databases and assets.
You get the TASK and the ARCHIVE_INDEX (file list, sizes, optional purposes).
Decide the MINIMAL set of files needed for this task and the first command(s) to run.
Reply ONLY valid JSON, no fences:
{"explanation": "...", "needed_files": ["path/a.py", "data/b.csv"], "commands": ["python3 path/a.py"]}
Rules:
- needed_files MUST exist in ARCHIVE_INDEX (copy names exactly).
- Commands run with cwd = the sandbox dir where those files were extracted,
  non-interactively, with a timeout. Keep file paths relative.
- Extract as few files as possible (lazy loading).
"""

FOLLOWUP_SYSTEM = """You are the vault router of 'mytool'. Commands were executed inside the
extracted sandbox; some failed or the task is incomplete. You may run more
commands (inspect output, patch the files with sed/python, write new files).
Reply ONLY valid JSON, either:
{"commands": ["next", ...]}   to continue, or
{"done": true, "summary": "..."}  to finish.
"""

ADD_FILE_CAP = 50


def _index_lines(vlt) -> str:
    purposes = db.vault_get_purposes()
    lines = []
    for name, size in vlt.index():
        p = purposes.get(name)
        extra = f"  [purpose: {p}]" if p else ""
        lines.append(f"- {name} ({size} bytes){extra}")
    return "\n".join(lines) or "(empty archive)"


def route_task(prompt: str, vlt) -> dict:
    user = (f"TASK: {prompt}\n\nARCHIVE_INDEX:\n{_index_lines(vlt)}\n\n"
            f"Which files do you need, and what do we run first?")
    return brain.chat([{"role": "system", "content": PLANNER_SYSTEM},
                       {"role": "user", "content": user}])


def _followup(prompt: str, transcript: list) -> dict:
    lines = [f"TASK: {prompt}", "VAULT_TASK_INCOMPLETE. Results so far:"]
    for cmd, rc, out, err in transcript:
        lines.append(f"$ {cmd}\nexit={rc}\nstdout: {_trim(out, 800)}\n"
                     f"stderr: {_trim(err, 800)}")
    lines.append("Continue (commands) or finish ({\"done\": true, \"summary\": \"...\"}).")
    return brain.chat([{"role": "system", "content": FOLLOWUP_SYSTEM},
                       {"role": "user", "content": "\n\n".join(lines)}])


def _walk_files(root) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for f in filenames:
            p = os.path.join(dirpath, f)
            out.append((os.path.relpath(p, root).replace(os.sep, "/"), p))
    return sorted(out)


def execute_vault_task(task_id: int, prompt: str, yes: bool,
                       quiet: bool = False) -> tuple:
    """Full lazy-load transaction. Returns (ok, summary)."""
    cfg = config.load_config()
    vcfg_v = cfg.get("vault", {})
    max_steps = int(vcfg_v.get("max_steps", cfg.get("runner", {})
                               .get("max_steps", 6)))
    timeout = int(vcfg_v.get("timeout_sec", cfg.get("runner", {})
                             .get("timeout_sec", 120)))
    auto_add = bool(vcfg_v.get("auto_add_new_files", True))
    interactive = sys.stdin.isatty()

    def say(msg: str):
        if not quiet:
            print(msg)

    vlt = vault.open_vault()
    with vault.ArchiveLock(vlt.path):
        say(f"\U0001f4da vault: {vlt.path}")
        plan = route_task(prompt, vlt)
        needed = [n for n in (plan.get("needed_files") or [])
                  if isinstance(n, str)]
        say(f"\U0001f9e0 route: {plan.get('explanation', '')}")

        scratch = vault.new_scratch_dir()
        try:
            # ---- 3. LAZY LOAD (only these members leave the archive) ----
            hashes = vlt.extract(needed, scratch)
            say(f"\U0001f4e4 lazy-extracted {len(hashes)} file(s) -> {scratch}"
                f" (rest of archive stays compressed)")
            db.vault_log(task_id, "extract", ", ".join(sorted(hashes)) or "-")

            # ---- 4. EXECUTE with feedback loop --------------------------
            transcript = []
            status = "done"
            summary = plan.get("explanation", "")
            plan_cmds = plan.get("commands") or []
            for _round in range(max_steps):
                if plan.get("done"):
                    summary = plan.get("summary") or plan.get("explanation", "")
                    break
                cmds = plan.get("commands") or plan_cmds or []
                if not cmds:
                    summary = plan.get("explanation") or "nothing to run"
                    break
                plan_cmds = []
                round_failed = False
                for cmd in cmds:
                    say(f"\u25b8 $ {cmd}")
                    if not yes and interactive and \
                            cfg.get("runner", {}).get("confirm", True):
                        if input("   execute? [y/N] ").strip().lower() \
                                not in ("y", "yes"):
                            transcript.append((cmd, 65, "", "skipped"))
                            round_failed = True
                            continue
                    if safety_check(cmd):
                        status = "failed"
                        summary = f"refused unsafe command: {cmd}"
                        db.set_task(task_id, "failed", summary)
                        return False, summary
                    rc, out, err = _run_one(cmd, timeout, str(scratch))
                    db.record_run(task_id, len(transcript) + 1, cmd, rc,
                                  out, err)
                    transcript.append((cmd, rc, out, err))
                    say(f"   exit={rc}"
                        + (f"\n   {_trim(out, 500)}" if out.strip() else "")
                        + (f"\n   [stderr] {_trim(err, 500)}"
                           if err.strip() else ""))
                    if rc != 0:
                        round_failed = True
                if not round_failed:
                    break
                status = "fixing"
                plan = _followup(prompt, transcript[-6:])

            # ---- 5. DIFF (sha256 before vs after) -----------------------
            replace, add, delete = {}, [], set()
            after = dict(_walk_files(scratch))
            for name, h in hashes.items():
                p = after.pop(name, None)
                if p is None:
                    delete.add(name)    # AI removed/renamed it
                elif vault.sha256_file(p) != h:
                    replace[name] = p   # AI modified it
            if auto_add:
                for name, p in after.items():
                    if len(add) >= ADD_FILE_CAP:
                        break
                    if p.stat().st_size <= vault._limits()[0]:
                        add.append((name, p))   # AI created new files
            db.vault_log(task_id, "diff",
                         f"modified={len(replace)} new={len(add)} "
                         f"deleted={len(delete)}")

            # ---- 6. REPACK (only the changed members) -------------------
            if replace or add or delete:
                result = vlt.repack_changes(replace, dict(add), delete)
                say(f"\U0001f4e6 repacked into vault: modified={len(replace)}"
                    f" added={len(add)} deleted={len(delete)}"
                    f"  (backup: {result.get('backup', '-')})")
                db.vault_log(task_id, "repack",
                             f"modified={sorted(replace)} added={[a[0] for a in add]}"
                             f" deleted={sorted(delete)}")
            else:
                say("\U0001f4e6 archive untouched (nothing changed)")

            if status == "fixing":
                status, summary = "failed", summary or "rounds exhausted"
            ok = status != "failed"
            db.set_task(task_id, "done" if ok else "failed", summary)
            log.info("vault task %s done ok=%s", task_id, ok)
            return ok, summary
        finally:
            # ---- 7. CLEANUP — RAM/tmp files mitao, turant ---------------
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)
            db.vault_log(task_id, "cleanup", str(scratch))


def do_vault_task(prompt: str, queue: bool = False, yes: bool = False) -> tuple:
    """Entry for `mytool vault-run`. Returns (task_id, ok, summary)."""
    if queue:
        task_id = db.record_task(f"vault: {prompt}", status="queued")
        print(f"\U0001f465 queued as vault task #{task_id} — cron-tick / daemon"
              " will run it")
        return task_id, True, "queued"
    task_id = db.record_task(f"vault: {prompt}", status="running")
    ok, summary = execute_vault_task(task_id, prompt, yes=yes)
    print(f"[{'OK ' if ok else 'FAIL'}] vault task #{task_id}: {summary}")
    return task_id, ok, summary


# ------------------------------------------------------- AI index (cached)

def refresh_ai_index(quiet: bool = False) -> int:
    """LLM reads the index and writes a one-line purpose per file (cached)."""
    vlt = vault.open_vault()
    names = [n for n, _ in vlt.index()]
    if not names:
        return 0
    user = ("ARCHIVE_INDEX:\n" + _index_lines(vlt) +
            "\n\nFor EVERY file write a short purpose (max 12 words). Reply"
            ' ONLY JSON: {"files": [{"file": "name", "purpose": "..."}]}')
    resp = brain.chat([{"role": "system", "content":
                        "You are a precise file-indexer for an archive vault."},
                       {"role": "user", "content": user}])
    known = set(names)
    n = 0
    for item in resp.get("files", []):
        name, purpose = str(item.get("file", "")), str(item.get("purpose", ""))
        if name in known and purpose:
            db.vault_set_purpose(name, purpose)
            if not quiet:
                print(f"  {name:40s} {purpose}")
            n += 1
    return n
