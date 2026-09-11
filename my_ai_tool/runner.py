"""Task Runner — natural language in, shell commands out.

Flow:  prompt -> Brain plans JSON commands -> safety check -> execute
       -> output fed back to Brain -> repeat (max N rounds) -> DB record.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

from . import brain, config, db
from .logging_setup import get_logger

log = get_logger("runner")

# Best-effort guard rails: these commands are refused even in auto mode.
_DANGEROUS = [
    (r"\brm\b[^;&|]*\s-[a-z]*r[a-z]*f[a-z]*\s+(?:/|~|\$home)(?:\s|$)", "rm -rf of / or home"),
    (r"\brm\b[^;&|]*\s-[a-z]*r[a-z]*f[a-z]*\s+/(?:etc|usr|var|bin|sbin|boot|dev|proc|sys|lib|opt|home|root)", "rm -rf of system dir"),
    (r"\brm\b[^;&|]*\s/(?:etc|usr|var|bin|sbin|boot|dev|proc|sys|lib)\b", "rm of system dir"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bmkfs\b", "mkfs (disk format)"),
    (r"\bdd\b[^;&|]*\bof=/dev/", "dd writing to raw device"),
    (r">\s*/dev/(?:sd|hd|nvme|vd)", "redirect to raw device"),
    (r"\b(?:shutdown|poweroff|halt|reboot)\b", "shutdown/reboot"),
    (r"\bchmod\s+(?:-[a-z]+\s+)?777\s+/(?:\s|$)", "chmod 777 /"),
    (r"\b(?:curl|wget)\b[^;&|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", "piping downloads straight into a shell"),
    (r"\bmkswap\b[^;&|]*/dev/", "mkswap on raw device"),
    (r"\bcrontab\s+-r\b", "crontab -r (wipes all cron jobs)"),
]
_BLOCKED = [(re.compile(p, re.I), why) for p, why in _DANGEROUS]


def safety_check(cmd: str) -> str | None:
    for pat, why in _BLOCKED:
        if pat.search(cmd):
            return why
    return None


def _run_one(cmd: str, timeout: int, cwd: str):
    why = safety_check(cmd)
    if why:
        return 66, "", f"BLOCKED by safety policy: {why}"
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"TIMED OUT after {timeout}s"


def _trim(text: str, n: int = 1500) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n] + f"\n... (truncated, {len(text)} chars total)"


def plan_task(prompt: str) -> dict:
    cfg = config.load_config()
    cwd = os.getcwd()
    user = (f"TASK: {prompt}\n"
            f"CONTEXT: cwd={cwd}, os=linux, python={sys.version.split()[0]}")
    return brain.chat([{"role": "system", "content": brain.PLANNER_SYSTEM},
                       {"role": "user", "content": user}])


def _continue_after_failure(transcript: list) -> dict:
    lines = ["TASK INCOMPLETE. Command results so far:"]
    for cmd, rc, out, err in transcript:
        lines.append(f"$ {cmd}\nexit={rc}\nstdout: {_trim(out, 800)}\nstderr: {_trim(err, 800)}")
    lines.append("Continue with next commands, or reply {\"done\": true, \"summary\": \"...\"}.")
    return brain.chat([{"role": "system", "content": brain.FOLLOWUP_SYSTEM},
                       {"role": "user", "content": "\n\n".join(lines)}])


def execute_plan(task_id: int, prompt: str, yes: bool, quiet: bool = False) -> tuple:
    """Returns (ok: bool, summary: str)."""
    cfg = config.load_config()
    max_steps = int(cfg.get("runner", {}).get("max_steps", 6))
    timeout = int(cfg.get("runner", {}).get("timeout_sec", 120))
    interactive = sys.stdin.isatty()

    def say(msg: str):
        if not quiet:
            print(msg)

    plan = plan_task(prompt)
    say(f"\U0001f9e0 plan: {plan.get('explanation', '')}")

    transcript: list = []
    failed = False
    summary = ""

    for _round in range(max_steps):
        if plan.get("done"):
            summary = plan.get("summary") or plan.get("explanation", "")
            break
        commands = plan.get("commands") or []
        if not commands:
            summary = plan.get("explanation") or "nothing to do"
            break

        round_failed = False
        for cmd in commands:
            say(f"\u25b8 $ {cmd}")
            if not yes and interactive and cfg.get("runner", {}).get("confirm", True):
                ans = input("   execute? [y/N] ").strip().lower()
                if ans not in ("y", "yes"):
                    db.record_run(task_id, len(transcript) + 1, cmd, 65,
                                  "", "skipped by user")
                    say("   skipped.")
                    transcript.append((cmd, 65, "", "skipped by user"))
                    round_failed = True
                    continue
            rc, out, err = _run_one(cmd, timeout, os.getcwd())
            db.record_run(task_id, len(transcript) + 1, cmd, rc, out, err)
            transcript.append((cmd, rc, out, err))
            say(f"   exit={rc}" + (f"\n   {_trim(out, 500)}" if out.strip() else "")
                + (f"\n   [stderr] {_trim(err, 500)}" if err.strip() else ""))
            if rc != 0:
                round_failed = True
                if "BLOCKED by safety policy" in err:
                    say("   \u26d4 refused.")
                    failed = True
                    summary = f"refused unsafe command: {cmd}"
                    db.set_task(task_id, "failed", summary)
                    return False, summary

        if not round_failed:
            summary = plan.get("explanation", "") or "all commands succeeded"
            break

        failed = True
        try:
            plan = _continue_after_failure(transcript[-6:])
        except brain.BrainError as e:
            summary = f"stopped: follow-up planning failed ({e})"
            break
    else:
        summary = summary or f"reached max_steps={max_steps} rounds"

    status = "failed" if failed else "done"
    if failed and not summary:
        summary = "some commands failed"
    db.set_task(task_id, status, summary)
    log.info("task %s finished status=%s summary=%s", task_id, status, summary)
    return (not failed), summary


def do_task(prompt: str, queue: bool = False, yes: bool = False) -> tuple:
    """Entry for `mytool do-task`. Returns (task_id, ok, summary)."""
    if queue:
        task_id = db.record_task(prompt, status="queued")
        print(f"\U0001f465 queued as task #{task_id} — cron-tick / daemon will run it "
              f"(see `mytool tasks`)")
        return task_id, True, "queued"
    task_id = db.record_task(prompt, status="running")
    ok, summary = execute_plan(task_id, prompt, yes=yes)
    mark = "OK " if ok else "FAIL"
    print(f"[{mark}] task #{task_id}: {summary}")
    return task_id, ok, summary


def run_queued(quiet: bool = False) -> int:
    """Run queued tasks in the background (cron-tick / daemon)."""
    cfg = config.load_config()
    auto = bool(cfg.get("runner", {}).get("background_auto", True))
    ran = 0
    for row in db.queued_tasks():
        ran += 1
        if not auto:
            db.set_task(row["id"], "needs_confirm",
                        "background_auto=false — run manually: mytool results "
                        f"{row['id']} / re-run: mytool do-task")
            continue
        db.set_task(row["id"], "running")
        try:
            prompt = row["prompt"] or ""
            if prompt.startswith("vault: "):
                from . import vault_router
                vault_router.execute_vault_task(row["id"],
                                                prompt[len("vault: "):],
                                                yes=True, quiet=quiet)
            else:
                execute_plan(row["id"], prompt, yes=True, quiet=quiet)
        except Exception as e:  # one bad task must not kill the batch
            log.exception("queued task %s crashed", row["id"])
            db.set_task(row["id"], "failed", f"runner error: {e}")
    return ran
