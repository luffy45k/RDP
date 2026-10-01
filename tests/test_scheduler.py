"""scheduler.py — the cron tick, the daemon loop and the overlap lock."""
from __future__ import annotations

from unittest import mock

from my_ai_tool import db, runner, scheduler, updater

from .support import ToolTestCase


class TestTick(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.use_mock_brain()
        self.set_config(**{"update__auto": False})

    def test_idle_tick_does_nothing(self):
        self.assertEqual(scheduler.tick(quiet=True), [])

    def test_tick_runs_queued_tasks(self):
        tid, _, _ = runner.do_task("background job", queue=True)
        actions = scheduler.tick(quiet=True)
        self.assertIn("tasks_run:1", actions)
        self.assertEqual(db.get_task(tid)["status"], "done")

    def test_tick_heals_open_crashes(self):
        with mock.patch.object(scheduler, "_tick_locked",
                               wraps=scheduler._tick_locked):
            with mock.patch("my_ai_tool.healer.heal_open_crashes",
                            return_value={"fixed": 1, "failed": 0}) as heal:
                actions = scheduler.tick(quiet=True)
        heal.assert_called_once()
        self.assertIn("code_changed", actions)

    def test_heal_auto_off_skips_healing(self):
        self.set_config(**{"heal__auto": False})
        with mock.patch("my_ai_tool.healer.heal_open_crashes") as heal:
            scheduler.tick(quiet=True)
        heal.assert_not_called()

    def test_update_status_is_surfaced(self):
        with mock.patch.object(updater, "maybe_check_update",
                               return_value="error"):
            self.assertIn("update_error", scheduler.tick(quiet=True))

    def test_only_one_tick_at_a_time(self):
        with scheduler._Lock():
            # a second tick must skip instead of running concurrently
            self.assertEqual(scheduler.tick(quiet=True), [])

    def test_cron_tick_exit_code(self):
        self.assertEqual(scheduler.cron_tick(quiet=True), 0)


class TestDaemon(ToolTestCase):
    def test_daemon_restarts_itself_when_code_changed(self):
        """Regression: the restart path referenced an unimported name, so a
        daemon died with NameError exactly when an update/heal landed."""
        with mock.patch.object(scheduler, "tick", return_value=["code_changed"]), \
                mock.patch.object(scheduler.time, "sleep"), \
                mock.patch.object(updater, "restart_self",
                                  side_effect=SystemExit(0)) as restart:
            with self.assertRaises(SystemExit):
                scheduler.daemon(interval_min=15)
        restart.assert_called_once_with(["daemon", "--interval", "15"])

    def test_daemon_restarts_after_an_update(self):
        with mock.patch.object(scheduler, "tick", return_value=["updated"]), \
                mock.patch.object(scheduler.time, "sleep"), \
                mock.patch.object(updater, "restart_self",
                                  side_effect=SystemExit(0)) as restart:
            with self.assertRaises(SystemExit):
                scheduler.daemon(interval_min=5)
        restart.assert_called_once_with(["daemon", "--interval", "5"])

    def test_a_failing_tick_is_recorded_but_never_kills_the_daemon(self):
        calls = []

        def boom(**kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("tick exploded")
            raise KeyboardInterrupt

        with mock.patch.object(scheduler, "tick", side_effect=boom), \
                mock.patch.object(scheduler.time, "sleep"):
            with self.assertRaises(KeyboardInterrupt):
                scheduler.daemon(interval_min=1)
        self.assertEqual(len(calls), 2)                  # survived the crash
        crash = db.latest_open_crash()
        self.assertEqual(crash["crash_type"], "RuntimeError")
        self.assertEqual(crash["status"], "open")        # queued for healing
