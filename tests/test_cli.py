"""cli.py — argument wiring, the crash net, and the end-to-end demo."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from unittest import mock

from my_ai_tool import cli, db, logging_setup, paths, selftest

from .support import REPO_ROOT, ToolTestCase


def run_cli(*argv):
    """Call cli.main() and capture what the user would see."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


class TestParser(ToolTestCase):
    def test_no_command_prints_help(self):
        code, out = run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage:", out)

    def test_every_subcommand_has_a_handler(self):
        parser = cli.build_parser()
        subs = [a for a in parser._actions
                if hasattr(a, "choices") and isinstance(a.choices, dict)][0]
        self.assertGreaterEqual(len(subs.choices), 20)
        for name, sub in subs.choices.items():
            with self.subTest(cmd=name):
                self.assertTrue(callable(sub.get_default("func")), name)

    def test_version(self):
        code, out = run_cli("version")
        self.assertEqual(code, 0)
        self.assertIn("mytool", out)


class TestConfigCommands(ToolTestCase):
    def test_list_get_set(self):
        code, out = run_cli("config", "list")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["provider"], "ollama")

        self.assertEqual(run_cli("config", "set", "provider", "mock")[0], 0)
        code, out = run_cli("config", "get", "provider")
        self.assertEqual((code, out.strip()), (0, '"mock"'))

    def test_bad_usage_is_reported(self):
        self.assertEqual(run_cli("config", "get")[0], 2)
        self.assertEqual(run_cli("config", "set", "provider")[0], 2)
        self.assertEqual(run_cli("config", "get", "nope.key")[0], 1)


class TestInformationalCommands(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.use_mock_brain()

    def test_init_and_status(self):
        self.assertEqual(run_cli("init")[0], 0)
        self.assertEqual(db.meta_get("installed_version"), cli.__version__)
        code, out = run_cli("status")
        self.assertEqual(code, 0)
        for field in ("code dir", "data dir", "provider", "crashes", "cron"):
            self.assertIn(field, out)

    def test_selftest_core_passes_on_a_healthy_checkout(self):
        code, out = run_cli("selftest", "--core")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)

    def test_full_selftest_includes_the_llm_check(self):
        ok, checks = selftest.run(core=False)
        self.assertTrue(ok)
        self.assertIn("llm-reachable", [c[0] for c in checks])

    def test_llm_problems_do_not_fail_the_selftest(self):
        self.set_config(provider="openai")
        os.environ.pop("OPENAI_API_KEY", None)
        ok, checks = selftest.run(core=False)
        self.assertTrue(ok, "a broken LLM endpoint must stay non-critical")
        llm = [c for c in checks if c[0] == "llm-reachable"][0]
        self.assertFalse(llm[1])
        self.assertFalse(llm[3])            # not critical

    def test_tasks_results_and_crashes_listings(self):
        self.assertEqual(run_cli("do-task", "demo", "-y")[0], 0)
        code, out = run_cli("tasks")
        self.assertEqual(code, 0)
        self.assertIn("demo", out)
        code, out = run_cli("results", "1")
        self.assertEqual(code, 0)
        self.assertIn("step 1", out)
        self.assertEqual(run_cli("results", "999")[0], 1)
        self.assertEqual(run_cli("crashes")[0], 0)
        self.assertEqual(run_cli("crashes", "999")[0], 1)

    def test_heal_without_crashes(self):
        code, out = run_cli("heal")
        self.assertEqual(code, 0)
        self.assertIn("heal summary", out)
        self.assertEqual(run_cli("heal", "--id", "42")[0], 1)


class TestSmartRunAlias(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.use_mock_brain()

    def test_run_uses_the_plain_runner_without_a_vault(self):
        with mock.patch("my_ai_tool.runner.do_task") as plain, \
                mock.patch("my_ai_tool.vault_router.do_vault_task") as vaulted:
            run_cli("run", "do a thing")
        plain.assert_called_once()
        vaulted.assert_not_called()

    def test_run_prefers_the_vault_when_one_exists(self):
        from my_ai_tool import vault
        vault.init_from_dir(str(self.make_project()))
        with mock.patch("my_ai_tool.runner.do_task") as plain, \
                mock.patch("my_ai_tool.vault_router.do_vault_task") as vaulted:
            run_cli("run", "do a thing")
        vaulted.assert_called_once()
        plain.assert_not_called()


class TestCrashNet(ToolTestCase):
    def test_an_unexpected_exception_becomes_a_crash_record(self):
        self.set_config(**{"heal__auto": False})
        with mock.patch.object(cli, "cmd_version",
                               side_effect=RuntimeError("boom")):
            code, out = run_cli("version")
        self.assertEqual(code, 1)
        self.assertIn("Crash captured", out)
        crash = db.latest_open_crash()
        self.assertEqual(crash["crash_type"], "RuntimeError")
        self.assertEqual(len(list(paths.crash_dir().glob("crash-*.md"))), 1)

    def test_keyboard_interrupt_is_not_a_crash(self):
        with mock.patch.object(cli, "cmd_version",
                               side_effect=KeyboardInterrupt):
            code, _ = run_cli("version")
        self.assertEqual(code, 130)
        self.assertEqual(db.recent_crashes(), [])

    def test_systemexit_is_passed_through(self):
        with mock.patch.object(cli, "cmd_version", side_effect=SystemExit(3)):
            with self.assertRaises(SystemExit):
                run_cli("version")


class TestDemoFileConsistency(ToolTestCase):
    def test_crash_test_reset_restores_the_committed_demo_file(self):
        """`crash-test --reset` writes cli.BUGGY_DEMO, so it must match the
        file in the repo — otherwise a reset leaves a dirty git tree."""
        shipped = (REPO_ROOT / "examples" / "broken.py").read_text(
            encoding="utf-8")
        self.assertEqual(shipped, cli.BUGGY_DEMO)

    def test_the_demo_file_raises_on_purpose(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "demo", REPO_ROOT / "examples" / "broken.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertAlmostEqual(mod.divide(6, 3), 1 / 3)
        with self.assertRaises(ZeroDivisionError):
            mod.divide(5, 0)


class TestLogging(ToolTestCase):
    def test_logs_are_written_into_the_data_dir(self):
        import logging
        logging_setup._initialized = False
        logging_setup.setup_logging()
        log = logging.getLogger("mytool")
        for h in list(log.handlers):            # keep the console quiet
            if type(h) is logging.StreamHandler:
                log.removeHandler(h)
        logging_setup.get_logger("test").warning("hello from the test")
        self.assertIn("hello from the test",
                      paths.log_file().read_text(encoding="utf-8"))


class TestEndToEndDemo(ToolTestCase):
    """The README demo, run exactly as a user would: a real subprocess, in a
    real git repo, with the mock brain — crash -> heal -> verify -> commit."""

    clone_code = True

    def setUp(self):
        super().setUp()
        self.git_env = dict(os.environ)
        self.git_env.update({
            "GIT_AUTHOR_NAME": "mytool", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "mytool",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PYTHONPATH": str(self.code_dir),
        })
        for args in (("init", "--initial-branch=main"), ("add", "-A"),
                     ("commit", "-m", "base")):
            subprocess.run(["git", "-C", str(self.code_dir), *args],
                           check=True, capture_output=True, env=self.git_env)

    def mytool(self, *argv):
        return subprocess.run([sys.executable, "-m", "my_ai_tool", *argv],
                              cwd=str(self.code_dir), env=self.git_env,
                              capture_output=True, text=True, timeout=300)

    def test_crash_demo_is_detected_fixed_and_committed(self):
        self.assertEqual(self.mytool("config", "set", "provider",
                                     "mock").returncode, 0)

        demo = self.mytool("crash-test", "--demo")
        self.assertIn("Crash captured", demo.stdout)
        self.assertIn("self-heal OK", demo.stdout)
        self.assertEqual(demo.returncode, 0)

        crashes = self.mytool("crashes")
        self.assertIn("fixed", crashes.stdout)

        log = subprocess.run(["git", "-C", str(self.code_dir), "log",
                              "--oneline", "-1"], capture_output=True,
                             text=True, env=self.git_env)
        self.assertIn("self-heal(crash-1)", log.stdout)

        # the healed tool still works, and the demo can be reset for the next run
        self.assertEqual(self.mytool("selftest", "--core").returncode, 0)
        self.assertEqual(self.mytool("crash-test", "--reset").returncode, 0)
        self.assertIn("1 / b", (self.code_dir / "examples" / "broken.py")
                      .read_text(encoding="utf-8"))
