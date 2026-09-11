"""Filesystem locations. Code and data are strictly separated:

    CODE  -> the git repository this package lives in (self-updated by git pull)
    DATA  -> OS user folder (~/.my_ai_tool on Linux, %APPDATA%/my_ai_tool on
             Windows). Survives code deletion and code updates.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import DATA_DIR_NAME


def data_dir() -> Path:
    """Persistent data folder (never inside the code repo)."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
        d = base / DATA_DIR_NAME
    else:
        base = Path(os.environ.get("MYTOOL_DATA_DIR") or Path.home())
        d = base / ("." + DATA_DIR_NAME)
    _ensure_data_dirs(d)
    return d


def _ensure_data_dirs(d: Path) -> None:
    for sub in ("logs", "backups", "reports", "crashes"):
        try:
            (d / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def code_dir() -> Path:
    """Folder containing the installed code (the git repo)."""
    env = os.environ.get("MYTOOL_CODE_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def db_path() -> Path:
    return data_dir() / "database.db"


def config_path() -> Path:
    return data_dir() / "config.json"


def log_file() -> Path:
    return data_dir() / "logs" / "tool.log"


def backup_dir() -> Path:
    p = data_dir() / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def crash_dir() -> Path:
    p = data_dir() / "crashes"
    p.mkdir(parents=True, exist_ok=True)
    return p


def report_dir() -> Path:
    p = data_dir() / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def lock_file() -> Path:
    return data_dir() / "scheduler.lock"
