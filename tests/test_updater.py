"""updater.py — git-based self-update that must never brick the install."""
from __future__ import annotations

import os
import subprocess
from unittest import mock

from my_ai_tool import __version__, db, updater

from .support import ToolTestCase

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True)


class TestVersion(ToolTestCase):
    def test_version_includes_the_commit_when_in_a_git_repo(self):
        v = updater.current_version()
        self.assertTrue(v.startswith(__version__), v)

    def test_version_falls_back_outside_git(self):
        os.environ["MYTOOL_CODE_DIR"] = str(self.tmp)
        self.assertEqual(updater.current_version(), __version__)


class TestCheckUpdates(ToolTestCase):
    def setUp(self):
        super().setUp()
        os.environ.update(GIT_ENV)

    def _origin_and_clone(self):
        origin = self.tmp / "origin"
        work = self.tmp / "work"
        clone = self.tmp / "clone"
        origin.mkdir()
        git(origin, "init", "--bare", "--initial-branch=main")
        subprocess.run(["git", "clone", str(origin), str(work)], check=True,
                       capture_output=True)
        (work / "file.txt").write_text("v1\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-m", "first")
        git(work, "push", "-u", "origin", "main")
        subprocess.run(["git", "clone", str(origin), str(clone)], check=True,
                       capture_output=True)
        os.environ["MYTOOL_CODE_DIR"] = str(clone)
        return origin, work, clone

    def test_not_a_git_repo_is_reported_not_crashed(self):
        os.environ["MYTOOL_CODE_DIR"] = str(self.tmp)
        result = updater.check_updates()
        self.assertEqual(result["status"], "not_a_git_repo")

    def test_up_to_date(self):
        self._origin_and_clone()
        self.assertEqual(updater.check_updates()["status"], "up_to_date")
        self.assertEqual(db.meta_get("last_update_result"), "up_to_date")

    def test_available_then_applied(self):
        origin, work, clone = self._origin_and_clone()
        (work / "file.txt").write_text("v2\n", encoding="utf-8")
        git(work, "commit", "-am", "second")
        git(work, "push")

        check = updater.check_updates(apply=False)
        self.assertEqual(check["status"], "available")
        self.assertIn("1 new commit", check["detail"])
        self.assertEqual((clone / "file.txt").read_text(), "v1\n")

        applied = updater.check_updates(apply=True)
        self.assertEqual(applied["status"], "updated")
        self.assertEqual((clone / "file.txt").read_text(), "v2\n")
        self.assertNotEqual(applied["before"], applied["after"])

    def test_local_self_heal_commits_survive_an_update(self):
        origin, work, clone = self._origin_and_clone()
        (clone / "healed.py").write_text("# fixed by the healer\n",
                                         encoding="utf-8")
        git(clone, "add", "-A")
        git(clone, "commit", "-m", "self-heal(crash-1): local fix")
        (work / "file.txt").write_text("v2\n", encoding="utf-8")
        git(work, "commit", "-am", "upstream change")
        git(work, "push")

        self.assertEqual(updater.check_updates()["status"], "updated")
        self.assertTrue((clone / "healed.py").exists())   # rebased on top
        self.assertEqual((clone / "file.txt").read_text(), "v2\n")

    def test_a_conflicting_update_keeps_the_old_code(self):
        origin, work, clone = self._origin_and_clone()
        (clone / "file.txt").write_text("local\n", encoding="utf-8")
        git(clone, "commit", "-am", "local edit")
        (work / "file.txt").write_text("remote\n", encoding="utf-8")
        git(work, "commit", "-am", "remote edit")
        git(work, "push")

        result = updater.check_updates()
        self.assertEqual(result["status"], "error")
        self.assertIn("old code kept intact", result["detail"])
        self.assertEqual((clone / "file.txt").read_text(), "local\n")
        # the repo is left usable, not mid-rebase
        state = subprocess.run(["git", "-C", str(clone), "status",
                                "--porcelain=v1"], capture_output=True,
                               text=True)
        self.assertEqual(state.returncode, 0)

    def test_fetch_failure_is_reported(self):
        _, _, clone = self._origin_and_clone()
        git(clone, "remote", "set-url", "origin",
            str(self.tmp / "does-not-exist"))
        self.assertEqual(updater.check_updates()["status"], "error")


class TestThrottling(ToolTestCase):
    def test_auto_update_can_be_switched_off(self):
        self.set_config(**{"update__auto": False})
        self.assertEqual(updater.maybe_check_update(), "disabled")

    def test_checks_are_throttled(self):
        import time
        db.meta_set("last_update_check", str(int(time.time())))
        self.assertEqual(updater.maybe_check_update(), "throttled")

    def test_stale_check_triggers_an_update_run(self):
        db.meta_set("last_update_check", "0")
        with mock.patch.object(updater, "check_updates",
                               return_value={"status": "up_to_date",
                                             "detail": "ok"}) as check:
            self.assertEqual(updater.maybe_check_update(), "up_to_date")
        check.assert_called_once_with(apply=True)


class TestRestart(ToolTestCase):
    def test_restart_reexecs_the_module_with_the_code_dir_on_the_path(self):
        with mock.patch.object(updater.os, "execve") as execve:
            updater.restart_self(["daemon", "--interval", "15"])
        (exe, argv, env), _ = execve.call_args
        self.assertEqual(argv[1:], ["-m", "my_ai_tool", "daemon",
                                    "--interval", "15"])
        self.assertEqual(env["MYTOOL_CODE_DIR"], str(updater.paths.code_dir()))
        self.assertIn(str(updater.paths.code_dir()), env["PYTHONPATH"])
