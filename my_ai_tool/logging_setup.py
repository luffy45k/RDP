"""Rotating file log in the persistent data dir + console echo."""
from __future__ import annotations

import logging

from . import paths

_initialized = False


def setup_logging(level: str = "INFO") -> None:
    global _initialized
    root = logging.getLogger("mytool")
    if _initialized:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S")

    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(str(paths.log_file()), maxBytes=1_000_000,
                                 backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass  # data dir not writable — console logging still works

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
    _initialized = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger("mytool." + name)
