"""JSON config stored in the persistent data dir.

Config survives code updates because it lives in ~/.my_ai_tool/config.json.
Supports dotted keys:  config get heal.mode  /  config set heal.mode report
"""
from __future__ import annotations

import copy
import json

from . import paths

DEFAULTS: dict = {
    "provider": "ollama",                       # ollama | openai | mock
    "ollama": {
        "url": "http://localhost:11434",
        "model": "hermes3:3b",                  # Nous Research Hermes (default)
        "timeout_sec": 300,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
    "update": {
        "repo_url": "https://github.com/luffy45k/RDP.git",
        "branch": "main",
        "auto": True,                           # daemon/cron checks for updates
        "check_interval_min": 60,
    },
    "runner": {
        "max_steps": 6,                         # max AI->command->feedback rounds
        "confirm": True,                        # ask y/N before running commands (interactive)
        "background_auto": True,                # queued tasks run without confirmation
        "timeout_sec": 120,                     # per shell command
    },
    "heal": {
        "mode": "apply",                        # apply | report
        "auto": True,                           # heal automatically after a crash
        "max_attempts_per_file": 3,
    },
    "vault": {
        "path": "",                             # archive location (default ~/.my_ai_tool/vault.zip)
        "scratch": "auto",                      # auto=/dev/shm (RAM) -> /tmp | ya custom dir
        "format": "zip",                        # zip | tar.gz (vault-init ke liye)
        "max_file_mb": 256,                     # zip-bomb guard per file
        "max_total_mb": 1024,                   # total extraction budget
        "keep_backups": 3,                      # pre-repack archive backups
        "auto_add_new_files": True,             # AI ne banayi files repack ho jayein
        "max_steps": 6,
        "timeout_sec": 120,
    },
    "schedule": {
        "interval_min": 15,                     # cron / daemon wake-up interval
    },
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    p = paths.config_path()
    if not p.exists():
        cfg = copy.deepcopy(DEFAULTS)
        save_config(cfg)
        return cfg
    try:
        stored = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULTS)
    return _deep_merge(DEFAULTS, stored)


def save_config(cfg: dict) -> None:
    p = paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def get_value(cfg: dict, dotted: str):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def set_value(cfg: dict, dotted: str, raw: str):
    """Parse raw string into bool/int/float/JSON if possible, else keep str."""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        value = raw
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise KeyError(dotted)
    node[parts[-1]] = value
    return value
