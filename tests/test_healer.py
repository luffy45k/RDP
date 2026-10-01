"""healer.py — catch, log, diagnose, apply, verify, commit/rollback."""
from __future__ import annotations

from unittest import mock

from my_ai_tool import brain, db, healer, paths

from .support import ToolTestCase

BUGGY_MODULE = '''"""A package module that needs healing."""
from __future__ import annotations

from . import config  # relative import -> can only run via `python -m`


def divide(a: float, b: float) -> float:
    return 1 / b
'''


def fake_reply(code: str, analysis: str = "test fix",
               file_to_fix: str = "my_ai_tool/helper.py") -> dict:
    return {"analysis": analysis, "file_to_fix": file_to_fix,
            "full_corrected_code": code}


class TestCatchAndLog(ToolTestCase):
    def test_crash_is_stored_in_db_and_as_markdown(self):
        try:
            raise ValueError("kaboom")
        except ValueError as e:
            crash_id, report = healer.record_crash(e, context="unit-test")
        row = db.get_crash(crash_id)
        self.assertEqual(row["crash_type"], "ValueError")
        self.assertEqual(row["status"], "open")
        self.assertIn("kaboom", row["message"])
        self.assertIn("Traceback", row["traceback"])
        text = paths.crash_dir().joinpath(report.split("/")[-1]).read_text()
        self.assertIn("unit-test", text)
        self.assertIn("kaboom", text)

    def test_suspect_file_is_the_last_project_frame(self):
        code_dir = paths.code_dir()
        tb = (f'Traceback (most recent call last):\n'
              f'  File "/usr/lib/python3/runpy.py", line 1, in <module>\n'
              f'  File "{code_dir}/my_ai_tool/cli.py", line 2, in main\n'
              f'  File "{code_dir}/examples/broken.py", line 3, in divide\n'
              f'  File "<frozen importlib._bootstrap>", line 4, in x\n')
        self.assertEqual(healer._guess_source_file(tb), "examples/broken.py")

    def test_unknown_frames_do_not_crash_the_guess(self):
        self.assertEqual(healer._guess_source_file("no frames here"), "unknown")


class TestPathGuards(ToolTestCase):
    def test_only_existing_py_files_inside_the_repo_can_be_patched(self):
        for bad in ("/etc/passwd", "../../outside.py", "README.md",
                    "my_ai_tool/does_not_exist.py"):
            with self.subTest(bad=bad), self.assertRaises(healer.HealError):
                healer._resolve_target(bad)

    def test_a_real_module_resolves(self):
        target, rel = healer._resolve_target("my_ai_tool/healer.py")
        self.assertEqual(rel, "my_ai_tool/healer.py")
        self.assertTrue(target.exists())


class TestVerifyHelpers(ToolTestCase):
    clone_code = True

    def test_module_name_only_for_importable_packages(self):
        self.assertEqual(
            healer._module_name(self.code_dir / "my_ai_tool" / "db.py"),
            "my_ai_tool.db")
        self.assertIsNone(
            healer._module_name(self.code_dir / "examples" / "broken.py"))
        self.assertIsNone(healer._module_name(self.code_dir / "install.sh"))

    def test_heal_verify_is_skipped_when_the_file_has_no_self_check(self):
        target = self.code_dir / "my_ai_tool" / "db.py"
        self.assertIsNone(healer._heal_verify_argv(target, "python3"))

    def test_package_modules_are_launched_with_dash_m(self):
        target = self.code_dir / "my_ai_tool" / "helper.py"
        target.write_text(BUGGY_MODULE + '\nif "--heal-verify" in []:\n    pass\n',
                          encoding="utf-8")
        self.assertEqual(healer._heal_verify_argv(target, "python3"),
                         ["python3", "-m", "my_ai_tool.helper", "--heal-verify"])

    def test_scripts_are_launched_by_path(self):
        target = self.code_dir / "examples" / "broken.py"
        argv = healer._heal_verify_argv(target, "python3")
        self.assertEqual(argv, ["python3", str(target), "--heal-verify"])


class TestHealing(ToolTestCase):
    clone_code = True

    def setUp(self):
        super().setUp()
        self.use_mock_brain()
        self.helper = self.code_dir / "my_ai_tool" / "helper.py"
        self.helper.write_text(BUGGY_MODULE, encoding="utf-8")

    def _crash(self, source="my_ai_tool/helper.py"):
        tb = (f'Traceback (most recent call last):\n'
              f'  File "{self.code_dir}/{source}", line 8, in divide\n'
              f'    return 1 / b\nZeroDivisionError: division by zero\n')
        cid = db.record_crash("ZeroDivisionError", "division by zero", tb,
                              source, "")
        return db.get_crash(cid)

    def test_a_package_module_can_be_healed(self):
        """Regression: the verifier used to run package modules as plain
        scripts, so every fix to the tool's own code died with ImportError
        and was rolled back."""
        crash = self._crash()
        self.assertEqual(healer.heal_crash(crash), "fixed")
        self.assertIn("float('inf')", self.helper.read_text())
        self.assertEqual(db.get_crash(crash["id"])["status"], "fixed")
        fix = db.fixes_for_crash(crash["id"])[0]
        self.assertTrue(fix["applied"] and fix["verified"])
        self.assertTrue(paths.backup_dir().joinpath(
            fix["backup_path"].split("/")[-1]).exists())

    def test_the_shipped_demo_file_can_be_healed(self):
        crash = self._crash("examples/broken.py")
        self.assertEqual(healer.heal_crash(crash), "fixed")
        demo = (self.code_dir / "examples" / "broken.py").read_text()
        self.assertIn("float('inf')", demo)

    def test_syntax_error_is_rolled_back(self):
        crash = self._crash()
        before = self.helper.read_text()
        with mock.patch.object(brain, "chat",
                               return_value=fake_reply("def broken(:\n")):
            self.assertEqual(healer.heal_crash(crash), "failed")
        self.assertEqual(self.helper.read_text(), before)
        self.assertEqual(db.get_crash(crash["id"])["status"], "open")
        self.assertFalse(db.fixes_for_crash(crash["id"])[0]["verified"])

    def test_import_error_is_rolled_back(self):
        crash = self._crash()
        before = self.helper.read_text()
        with mock.patch.object(
                brain, "chat",
                return_value=fake_reply("import definitely_not_a_module\n")):
            self.assertEqual(healer.heal_crash(crash), "failed")
        self.assertEqual(self.helper.read_text(), before)

    def test_failing_heal_verify_self_check_is_rolled_back(self):
        crash = self._crash()
        before = self.helper.read_text()
        bad = (BUGGY_MODULE +
               '\n\nif __name__ == "__main__":\n'
               '    import sys\n'
               '    if "--heal-verify" in sys.argv:\n'
               '        raise SystemExit(1)\n')
        with mock.patch.object(brain, "chat", return_value=fake_reply(bad)):
            self.assertEqual(healer.heal_crash(crash), "failed")
        self.assertEqual(self.helper.read_text(), before)

    def test_fix_that_breaks_the_core_selftest_is_rolled_back(self):
        """A patch can compile and import fine and still wreck the tool at
        runtime — the core selftest is the last line of defence."""
        db_file = self.code_dir / "my_ai_tool" / "db.py"
        before = db_file.read_text()
        crash = self._crash("my_ai_tool/db.py")
        broken = before.replace("SELECT * FROM tasks WHERE id=?",
                                "SELECT * FROM gone_table WHERE id=?", 1)
        self.assertNotEqual(broken, before)
        with mock.patch.object(
                brain, "chat",
                return_value=fake_reply(broken,
                                        file_to_fix="my_ai_tool/db.py")):
            self.assertEqual(healer.heal_crash(crash), "failed")
        self.assertEqual(db_file.read_text(), before)

    def test_report_mode_never_touches_the_code(self):
        self.set_config(**{"heal__mode": "report"})
        crash = self._crash()
        before = self.helper.read_text()
        self.assertEqual(healer.heal_crash(crash), "reported")
        self.assertEqual(self.helper.read_text(), before)
        self.assertEqual(db.get_crash(crash["id"])["status"], "reported")
        reports = list(paths.report_dir().glob("heal-report-crash-*.md"))
        self.assertEqual(len(reports), 1)
        self.assertIn("AI analysis", reports[0].read_text())

    def test_attempts_are_capped(self):
        self.set_config(**{"heal__max_attempts_per_file": 2})
        crash = self._crash()
        with mock.patch.object(brain, "chat",
                               side_effect=brain.BrainError("llm down")):
            self.assertEqual(healer.heal_crash(db.get_crash(crash["id"])),
                             "failed")
            self.assertEqual(db.get_crash(crash["id"])["status"], "open")
            self.assertEqual(healer.heal_crash(db.get_crash(crash["id"])),
                             "failed")
            self.assertEqual(db.get_crash(crash["id"])["status"], "failed")
            # parked: further attempts are skipped, not retried forever
            db.update_crash(crash["id"], status="open")
            self.assertEqual(healer.heal_crash(db.get_crash(crash["id"])),
                             "skipped")

    def test_missing_full_corrected_code_is_an_error(self):
        crash = self._crash()
        with mock.patch.object(brain, "chat",
                               return_value=fake_reply("   ")):
            self.assertEqual(healer.heal_crash(crash), "failed")

    def test_heal_open_crashes_reports_counts(self):
        self._crash()
        self._crash("examples/broken.py")
        counts = healer.heal_open_crashes(quiet=True)
        self.assertEqual(counts["fixed"], 2)
        self.assertEqual(db.open_crashes(), [])

    def test_heal_latest_without_crashes(self):
        self.assertEqual(healer.heal_latest(), "no_open_crashes")

    def test_cli_crash_net_records_and_heals(self):
        try:
            raise ZeroDivisionError("division by zero")
        except ZeroDivisionError as e:
            rc = healer.handle_crash_cli(e, context="unit")
        # the traceback points at the test file (outside the code dir), so the
        # heal cannot succeed — but the crash must still be captured and kept
        self.assertEqual(rc, 1)
        self.assertEqual(len(db.recent_crashes()), 1)

    def test_heal_auto_off_only_logs(self):
        self.set_config(**{"heal__auto": False})
        try:
            raise RuntimeError("nope")
        except RuntimeError as e:
            rc = healer.handle_crash_cli(e, context="unit")
        self.assertEqual(rc, 1)
        crash = db.latest_open_crash()
        self.assertEqual(crash["attempts"], 0)      # nothing was attempted
