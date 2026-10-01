"""config.py, paths.py and db.py — persistence that must survive updates."""
from __future__ import annotations

import json

from my_ai_tool import config, db, paths

from .support import ToolTestCase


class TestPaths(ToolTestCase):
    def test_data_lives_outside_the_code_folder(self):
        self.assertFalse(
            str(paths.data_dir()).startswith(str(paths.code_dir())),
            "data dir must never be inside the code repo")

    def test_standard_subdirs_are_created(self):
        for sub in ("logs", "backups", "reports", "crashes"):
            self.assertTrue((paths.data_dir() / sub).is_dir(), sub)

    def test_all_paths_resolve_under_the_data_dir(self):
        d = str(paths.data_dir())
        for p in (paths.db_path(), paths.config_path(), paths.log_file(),
                  paths.backup_dir(), paths.crash_dir(), paths.report_dir(),
                  paths.lock_file()):
            self.assertTrue(str(p).startswith(d), p)

    def test_code_dir_follows_the_env_override(self):
        import os
        os.environ["MYTOOL_CODE_DIR"] = str(self.tmp)
        self.assertEqual(paths.code_dir(), self.tmp.resolve())


class TestConfig(ToolTestCase):
    def test_first_load_writes_defaults(self):
        cfg = config.load_config()
        self.assertTrue(paths.config_path().exists())
        self.assertEqual(cfg["provider"], "ollama")
        self.assertIn("vault", cfg)

    def test_stored_values_are_merged_over_defaults(self):
        paths.config_path().write_text(json.dumps({"provider": "mock"}),
                                       encoding="utf-8")
        cfg = config.load_config()
        self.assertEqual(cfg["provider"], "mock")
        # untouched defaults are still there
        self.assertEqual(cfg["runner"]["max_steps"], 6)
        self.assertEqual(cfg["heal"]["mode"], "apply")

    def test_corrupt_config_falls_back_to_defaults(self):
        paths.config_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(config.load_config()["provider"], "ollama")

    def test_dotted_get_and_set_with_type_parsing(self):
        cfg = config.load_config()
        self.assertEqual(config.get_value(cfg, "heal.mode"), "apply")
        self.assertIs(config.set_value(cfg, "update.auto", "false"), False)
        self.assertEqual(config.set_value(cfg, "runner.max_steps", "9"), 9)
        self.assertEqual(config.set_value(cfg, "ollama.model", "hermes3:8b"),
                         "hermes3:8b")
        config.save_config(cfg)
        again = config.load_config()
        self.assertFalse(again["update"]["auto"])
        self.assertEqual(again["runner"]["max_steps"], 9)

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            config.get_value(config.load_config(), "nope.nothing")

    def test_set_creates_missing_branches(self):
        cfg = config.load_config()
        config.set_value(cfg, "custom.deep.key", "1")
        self.assertEqual(cfg["custom"]["deep"]["key"], 1)


class TestDatabase(ToolTestCase):
    def test_task_lifecycle(self):
        tid = db.record_task("do something", status="queued")
        self.assertEqual([r["id"] for r in db.queued_tasks()], [tid])
        db.set_task(tid, status="done", result_summary="all good")
        row = db.get_task(tid)
        self.assertEqual((row["status"], row["result_summary"]),
                         ("done", "all good"))
        self.assertEqual(db.queued_tasks(), [])
        self.assertEqual(db.recent_tasks(5)[0]["id"], tid)

    def test_set_task_without_fields_is_a_noop(self):
        tid = db.record_task("x")
        db.set_task(tid)
        self.assertEqual(db.get_task(tid)["status"], "running")

    def test_runs_are_recorded_and_trimmed(self):
        tid = db.record_task("x")
        db.record_run(tid, 1, "echo hi", 0, "hi", "")
        db.record_run(tid, 2, "boom", 1, "A" * 9000, "B" * 9000)
        runs = db.task_runs(tid)
        self.assertEqual([r["step"] for r in runs], [1, 2])
        self.assertEqual(len(runs[1]["stdout"]), 8000)
        self.assertEqual(len(runs[1]["stderr"]), 8000)

    def test_crash_and_fix_records(self):
        cid = db.record_crash("ZeroDivisionError", "division by zero",
                              "Traceback...", "examples/broken.py", "/r.md")
        self.assertEqual(db.latest_open_crash()["id"], cid)
        self.assertEqual([c["id"] for c in db.open_crashes()], [cid])
        db.update_crash(cid, attempts=1)
        self.assertEqual(db.get_crash(cid)["attempts"], 1)
        db.record_fix(cid, "examples/broken.py", "/b.bak", True, True, "fixed")
        fix = db.fixes_for_crash(cid)[0]
        self.assertTrue(fix["applied"] and fix["verified"])
        db.update_crash(cid, status="fixed")
        self.assertEqual(db.open_crashes(), [])
        self.assertIsNone(db.latest_open_crash())

    def test_meta_upsert(self):
        self.assertEqual(db.meta_get("missing", "fallback"), "fallback")
        db.meta_set("k", "1")
        db.meta_set("k", "2")
        self.assertEqual(db.meta_get("k"), "2")

    def test_vault_index_and_log(self):
        db.vault_set_purpose("a.py", "old")
        db.vault_set_purpose("a.py", "new")
        self.assertEqual(db.vault_get_purposes(), {"a.py": "new"})
        db.vault_log(1, "extract", "a.py")
        db.vault_log(1, "repack", "x" * 5000)
        rows = db.vault_log_recent(5)
        self.assertEqual(rows[0]["action"], "repack")
        self.assertEqual(len(rows[0]["detail"]), 4000)
