"""Shared harness: isolated data dir / code dir for every test."""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from my_ai_tool import config, logging_setup, paths  # noqa: E402


class ToolTestCase(unittest.TestCase):
    """Base case: temp data dir, optional temp copy of the code tree.

    `paths.data_dir()` reads MYTOOL_DATA_DIR on every call, so pointing that
    env var at a temp folder is enough to isolate the database, config, logs,
    backups and reports of a single test.
    """

    #: subclasses that let the healer rewrite code must set this to True so
    #: the patches land in a copy of the repo, never in the real checkout
    clone_code = False

    def setUp(self):
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)

        self.tmp = Path(tempfile.mkdtemp(prefix="mytool-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        home = self.tmp / "home"
        home.mkdir(parents=True, exist_ok=True)
        os.environ["MYTOOL_DATA_DIR"] = str(home)

        if self.clone_code:
            self.code_dir = self.tmp / "code"
            shutil.copytree(
                REPO_ROOT, self.code_dir,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc",
                                              ".venv", "venv"))
            os.environ["MYTOOL_CODE_DIR"] = str(self.code_dir)
        else:
            self.code_dir = REPO_ROOT

        self.data_dir = paths.data_dir()
        # hard safety net — a bug in the harness must never hit real user data
        assert str(self.data_dir).startswith(str(self.tmp)), self.data_dir

        self._silence_logging()
        self.addCleanup(self._reset_logging)

        # the tool is chatty by design; keep the suite readable unless asked
        if not os.environ.get("MYTOOL_TEST_VERBOSE"):
            self.stdout = io.StringIO()
            patcher = mock.patch.object(sys, "stdout", self.stdout)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    @staticmethod
    def _reset_logging():
        log = logging.getLogger("mytool")
        for h in list(log.handlers):
            log.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        log.propagate = True
        logging_setup._initialized = False

    @classmethod
    def _silence_logging(cls):
        """Swallow the tool's log output: pre-mark logging as initialised so
        setup_logging() keeps its hands off, and attach a null sink."""
        cls._reset_logging()
        if os.environ.get("MYTOOL_TEST_VERBOSE"):
            return
        log = logging.getLogger("mytool")
        log.addHandler(logging.NullHandler())
        log.propagate = False
        logging_setup._initialized = True

    # ------------------------------------------------------------- helpers
    def set_config(self, **dotted):
        """set_config(provider="mock", **{"heal.mode": "report"})"""
        cfg = config.load_config()
        for key, value in dotted.items():
            config.set_value(cfg, key.replace("__", "."), json.dumps(value)
                             if not isinstance(value, str) else value)
        config.save_config(cfg)
        return cfg

    def use_mock_brain(self):
        return self.set_config(provider="mock")

    def write(self, rel: str, text: str) -> Path:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def make_project(self, root: str = "proj") -> Path:
        """A small project tree used by the vault tests."""
        base = self.tmp / root
        (base / "scripts").mkdir(parents=True, exist_ok=True)
        (base / "data").mkdir(parents=True, exist_ok=True)
        (base / "scripts" / "report.py").write_text(
            "import csv, pathlib\n"
            "rows = list(csv.DictReader(open('data/users.csv')))\n"
            "pathlib.Path('out').mkdir(exist_ok=True)\n"
            "pathlib.Path('out/report.txt').write_text(f'users={len(rows)}\\n')\n"
            "print('users', len(rows))\n", encoding="utf-8")
        (base / "data" / "users.csv").write_text(
            "name,age\nasha,31\nraj,24\n", encoding="utf-8")
        (base / "assets_readme.txt").write_text("docs\n", encoding="utf-8")
        return base


def load_dev_module(name: str = "mock_ollama_server"):
    """Import dev/mock_ollama_server.py (not a package) by path."""
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "dev" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MockOllama:
    """The dev fake-Ollama server, bound to an ephemeral localhost port."""

    def __init__(self):
        handler = load_dev_module().Handler
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        return False
