"""SQLite persistent storage — ~/.my_ai_tool/database.db (WAL mode).

Tables: tasks, runs, crashes, fixes, meta.
"""
from __future__ import annotations

import sqlite3

from . import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed|needs_confirm
    result_summary TEXT,
    created_at     TEXT DEFAULT (datetime('now','localtime')),
    updated_at     TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS runs(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER,
    step       INTEGER,
    command    TEXT,
    exit_code  INTEGER,
    stdout     TEXT,
    stderr     TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS crashes(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    crash_type  TEXT,
    message     TEXT,
    traceback   TEXT,
    source_file TEXT,
    report_path TEXT,
    status      TEXT DEFAULT 'open',                  -- open|fixed|failed|reported|ignored
    attempts    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS fixes(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    crash_id    INTEGER,
    file        TEXT,
    backup_path TEXT,
    applied     INTEGER DEFAULT 0,
    verified    INTEGER DEFAULT 0,
    analysis    TEXT,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(paths.db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------- tasks/runs

def record_task(prompt: str, status: str = "running") -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO tasks(prompt, status) VALUES(?, ?)", (prompt, status))
        return int(cur.lastrowid)


def set_task(task_id: int, status: str | None = None,
             result_summary: str | None = None) -> None:
    q, args = [], []
    if status is not None:
        q.append("status=?"); args.append(status)
    if result_summary is not None:
        q.append("result_summary=?"); args.append(result_summary)
    if not q:
        return
    q.append("updated_at=datetime('now','localtime')")
    args.append(task_id)
    with connect() as c:
        c.execute(f"UPDATE tasks SET {', '.join(q)} WHERE id=?", args)


def get_task(task_id: int):
    with connect() as c:
        return c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def queued_tasks():
    with connect() as c:
        return c.execute(
            "SELECT * FROM tasks WHERE status='queued' ORDER BY id").fetchall()


def recent_tasks(limit: int = 20):
    with connect() as c:
        return c.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def task_runs(task_id: int):
    with connect() as c:
        return c.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY id", (task_id,)).fetchall()


def record_run(task_id: int, step: int, command: str, exit_code,
               stdout: str, stderr: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO runs(task_id, step, command, exit_code, stdout, stderr)"
            " VALUES(?,?,?,?,?,?)",
            (task_id, step, command, exit_code, stdout[:8000], stderr[:8000]))


# ------------------------------------------------------------------- crashes

def record_crash(crash_type: str, message: str, tb: str, source_file: str,
                 report_path: str) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO crashes(crash_type, message, traceback, source_file,"
            " report_path) VALUES(?,?,?,?,?)",
            (crash_type, message, tb, source_file, report_path))
        return int(cur.lastrowid)


def get_crash(crash_id: int):
    with connect() as c:
        return c.execute(
            "SELECT * FROM crashes WHERE id=?", (crash_id,)).fetchone()


def open_crashes():
    with connect() as c:
        return c.execute(
            "SELECT * FROM crashes WHERE status IN ('open') ORDER BY id").fetchall()


def latest_open_crash():
    with connect() as c:
        return c.execute(
            "SELECT * FROM crashes WHERE status='open' ORDER BY id DESC LIMIT 1"
        ).fetchone()


def recent_crashes(limit: int = 20):
    with connect() as c:
        return c.execute(
            "SELECT * FROM crashes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def update_crash(crash_id: int, status: str | None = None,
                 attempts: int | None = None,
                 source_file: str | None = None) -> None:
    q, args = [], []
    if status is not None:
        q.append("status=?"); args.append(status)
    if attempts is not None:
        q.append("attempts=?"); args.append(attempts)
    if source_file is not None:
        q.append("source_file=?"); args.append(source_file)
    if not q:
        return
    q.append("updated_at=datetime('now','localtime')")
    args.append(crash_id)
    with connect() as c:
        c.execute(f"UPDATE crashes SET {', '.join(q)} WHERE id=?", args)


# --------------------------------------------------------------------- fixes

def record_fix(crash_id: int, file: str, backup_path: str, applied: bool,
               verified: bool, analysis: str) -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO fixes(crash_id, file, backup_path, applied, verified,"
            " analysis) VALUES(?,?,?,?,?,?)",
            (crash_id, file, backup_path, int(applied), int(verified), analysis))
        return int(cur.lastrowid)


def fixes_for_crash(crash_id: int):
    with connect() as c:
        return c.execute(
            "SELECT * FROM fixes WHERE crash_id=? ORDER BY id",
            (crash_id,)).fetchall()


# ---------------------------------------------------------------------- meta

def meta_get(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        row = c.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def meta_set(key: str, value: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO meta(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
