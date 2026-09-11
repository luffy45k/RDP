"""mytool command line interface."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, config, db, paths
from .logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mytool",
        description="Self-running, self-updating, self-healing terminal AI agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  mytool do-task "organize ~/Downloads by file type"
  mytool do-task "check disk usage and write a report" --queue
  mytool status
  mytool heal                      # fix all open crashes now
  mytool update                    # git-pull latest code + restart
  mytool daemon --interval 15      # stay running in background
  mytool config set provider ollama
  mytool crash-test --demo         # intentionally crash to watch self-healing""")
    p.add_argument("--version", action="version",
                   version=f"mytool {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("do-task", help="give a natural-language task to the AI")
    s.add_argument("prompt")
    s.add_argument("--queue", action="store_true",
                   help="queue it; cron-tick/daemon will execute in background")
    s.add_argument("-y", "--yes", action="store_true",
                   help="run commands without confirmation prompt")
    s.set_defaults(func=cmd_do_task)

    s = sub.add_parser("queue", help="alias of: do-task --queue")
    s.add_argument("prompt")
    s.set_defaults(func=cmd_queue)

    s = sub.add_parser("tasks", help="list recent tasks")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_tasks)

    s = sub.add_parser("results", help="show a task's command results")
    s.add_argument("task_id", type=int)
    s.set_defaults(func=cmd_results)

    s = sub.add_parser("crashes", help="list crashes (or show one: crashes ID)")
    s.add_argument("crash_id", nargs="?", type=int)
    s.set_defaults(func=cmd_crashes)

    s = sub.add_parser("heal", help="self-heal all open crashes now")
    s.add_argument("--id", type=int, help="heal a specific crash id")
    s.set_defaults(func=cmd_heal)

    s = sub.add_parser("update", help="check GitHub for new code and update")
    s.add_argument("--check-only", action="store_true")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("daemon", help="run as a background daemon")
    s.add_argument("--interval", type=int, help="minutes between ticks")
    s.set_defaults(func=cmd_daemon)

    s = sub.add_parser("cron-tick", help="one background cycle (called by cron)")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_cron_tick)

    s = sub.add_parser("selftest", help="run smoke tests")
    s.add_argument("--core", action="store_true",
                   help="core tests only (skip LLM connectivity)")
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("config", help="config: list | get KEY | set KEY VALUE")
    s.add_argument("action", choices=["list", "get", "set"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("status", help="overall tool status")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("version", help="print version")
    s.set_defaults(func=cmd_version)

    s = sub.add_parser("crash-test", help=argparse.SUPPRESS)
    s.add_argument("--demo", action="store_true",
                   help="crash on examples/broken.py (ZeroDivisionError)")
    s.set_defaults(func=cmd_crash_test)

    s = sub.add_parser("init", help="create data dir, config and database")
    s.set_defaults(func=cmd_init)
    return p


# ------------------------------------------------------------------ commands

def cmd_do_task(a):
    from . import runner
    runner.do_task(a.prompt, queue=a.queue, yes=a.yes)
    return 0


def cmd_queue(a):
    from . import runner
    runner.do_task(a.prompt, queue=True)
    return 0


def cmd_tasks(a):
    for row in db.recent_tasks(a.limit):
        print(f"#{row['id']:<5} [{row['status']:^13}] {row['created_at']}  "
              f"{(row['prompt'] or '')[:60]}"
              + (f"  -> {(row['result_summary'] or '')[:60]}"
                 if row["result_summary"] else ""))
    return 0


def cmd_results(a):
    t = db.get_task(a.task_id)
    if not t:
        print("task not found")
        return 1
    print(f"task #{t['id']} [{t['status']}] {t['prompt']}")
    print(f"summary: {t['result_summary']}\n")
    for r in db.task_runs(a.task_id):
        print(f"--- step {r['step']}: $ {r['command']}  (exit={r['exit_code']})")
        if r["stdout"].strip():
            print(r["stdout"])
        if r["stderr"].strip():
            print("[stderr] " + r["stderr"])
    return 0


def cmd_crashes(a):
    from . import db as _db
    if a.crash_id:
        c = _db.get_crash(a.crash_id)
        if not c:
            print("crash not found")
            return 1
        print(f"crash #{c['id']} [{c['status']}] attempts={c['attempts']} "
              f"{c['created_at']}\nfile: {c['source_file']}\n"
              f"type: {c['crash_type']}: {c['message']}\nreport: {c['report_path']}")
        fixes = _db.fixes_for_crash(c["id"])
        for f in fixes:
            print(f"  fix: applied={bool(f['applied'])} verified={bool(f['verified'])}"
                  f" backup={f['backup_path']}\n       {f['analysis'][:120]}")
        return 0
    for c in _db.recent_crashes():
        print(f"#{c['id']:<4} [{c['status']:^8}] attempts={c['attempts']} "
              f"{c['created_at']}  {c['crash_type']}: {(c['message'] or '')[:60]}"
              f"  ({c['source_file']})")
    return 0


def cmd_heal(a):
    from . import healer
    if a.id:
        crash = db.get_crash(a.id)
        if not crash:
            print("crash not found")
            return 1
        result = healer.heal_crash(crash)
        print("result:", result)
        return 0 if result in ("fixed", "reported") else 1
    counts = healer.heal_open_crashes()
    print("heal summary:", counts)
    if counts.get("fixed"):
        print("code changed — restart any running daemon to load fresh code")
    return 0 if not counts.get("failed") else 1


def cmd_update(a):
    from . import updater
    result = updater.check_updates(apply=not a.check_only)
    print(f"update: [{result['status']}] {result['detail']}")
    if result["status"] == "updated":
        print("new code is in place — restart daemons or re-run your command")
        return 0
    return 0 if result["status"] in ("up_to_date", "available") else 1


def cmd_daemon(a):
    from . import scheduler
    return scheduler.daemon(a.interval)


def cmd_cron_tick(a):
    from . import scheduler
    return scheduler.cron_tick(quiet=a.quiet)


def cmd_selftest(a):
    from . import selftest
    return selftest.main(core=a.core)


def cmd_config(a):
    cfg = config.load_config()
    if a.action == "list":
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return 0
    if not a.key:
        print("need KEY (dotted, e.g. heal.mode)")
        return 2
    if a.action == "get":
        try:
            print(json.dumps(config.get_value(cfg, a.key)))
        except KeyError:
            print("unknown key")
            return 1
        return 0
    if a.value is None:
        print("need VALUE")
        return 2
    try:
        value = config.set_value(cfg, a.key, a.value)
    except KeyError:
        print("unknown key path")
        return 1
    config.save_config(cfg)
    print(f"saved: {a.key} = {json.dumps(value)}")
    return 0


def cmd_status(a):
    from . import updater, brain
    cfg = config.load_config()
    tasks = db.recent_tasks(1000)
    crashes = db.recent_crashes(1000)
    open_cr = sum(1 for c in crashes if c["status"] == "open")
    ok, detail = brain.ping()
    print(f"mytool {updater.current_version()}")
    print(f"  code dir : {paths.code_dir()}")
    print(f"  data dir : {paths.data_dir()}  (db: {paths.db_path().stat().st_size} bytes)")
    print(f"  provider : {cfg.get('provider')}  ({'OK: ' + detail if ok else 'PROBLEM: ' + detail})")
    print(f"  heal     : mode={cfg.get('heal', {}).get('mode')} "
          f"auto={cfg.get('heal', {}).get('auto')}")
    print(f"  update   : repo={cfg.get('update', {}).get('repo_url')} "
          f"branch={cfg.get('update', {}).get('branch')} "
          f"auto={cfg.get('update', {}).get('auto')}")
    print(f"  tasks    : {len(tasks)} total, "
          f"{sum(1 for t in tasks if t['status'] == 'queued')} queued")
    print(f"  crashes  : {len(crashes)} total, {open_cr} open")
    print(f"  last update check: {db.meta_get('last_update_check', 'never')}")
    print(f"  cron     : {_cron_installed()}")
    return 0


def _cron_installed() -> str:
    import subprocess
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if "# mytool-cron" in (p.stdout or ""):
            return "installed"
        return "not installed (run install.sh)"
    except FileNotFoundError:
        return "crontab not available on this machine"


def cmd_version(a):
    from . import updater
    print(f"mytool {updater.current_version()}")
    return 0


def cmd_crash_test(a):
    if a.demo:
        import importlib.util
        p = paths.code_dir() / "examples" / "broken.py"
        spec = importlib.util.spec_from_file_location("broken_demo", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print("6 / 3 =", mod.divide(6, 3))
        print("5 / 0 =", mod.divide(5, 0))  # ZeroDivisionError on purpose
        return 0
    raise RuntimeError("Intentional crash-test (no --demo)")


def cmd_init(a):
    cfg = config.load_config()
    db.meta_set("installed_version", __version__)
    print(f"init OK\n  data dir : {paths.data_dir()}\n  database : {paths.db_path()}"
          f"\n  config   : {paths.config_path()}"
          f"\n  provider : {cfg.get('provider')}")
    return 0


# ------------------------------------------------------------------ crash net

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging()
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    from . import healer
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n[interrupted]")
        return 130
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — the crash net IS the feature
        return healer.handle_crash_cli(e, context=args.cmd or "cli")


if __name__ == "__main__":
    sys.exit(main())
