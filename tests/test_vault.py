"""vault.py + vault_router.py — the lazy-loading compressed archive."""
from __future__ import annotations

import os
import stat
import zipfile

from my_ai_tool import config, db, vault, vault_router

from .support import ToolTestCase


class TestMemberGuards(ToolTestCase):
    def test_traversal_and_absolute_paths_are_rejected(self):
        for bad in ("../evil.txt", "/etc/passwd", "a/../../b", "",
                    "x\x00y", "./../../y", "/".join(["deep"] * 40)):
            with self.subTest(bad=bad), self.assertRaises(vault.VaultError):
                vault.validate_member(bad)

    def test_harmless_names_are_normalised(self):
        self.assertEqual(vault.validate_member("./a/b.txt"), "a/b.txt")
        self.assertEqual(vault.validate_member("a\\b.txt"), "a/b.txt")
        self.assertEqual(vault.validate_member("a/./b.txt"), "a/b.txt")


class TestArchive(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.use_mock_brain()
        self.project = self.make_project()

    def _init(self, fmt="zip"):
        self.set_config(**{"vault__format": fmt})
        return vault.init_from_dir(str(self.project))

    def test_init_creates_archive_and_remembers_it(self):
        path = self._init()
        self.assertTrue(path.exists())
        self.assertEqual(config.load_config()["vault"]["path"], str(path))
        self.assertEqual(
            sorted(n for n, _ in vault.open_vault().index()),
            ["assets_readme.txt", "data/users.csv", "scripts/report.py"])

    def test_extract_only_the_requested_members(self):
        self._init()
        vlt = vault.open_vault()
        dest = self.tmp / "scratch"
        dest.mkdir()
        hashes = vlt.extract(["data/users.csv"], dest)
        self.assertEqual(list(hashes), ["data/users.csv"])
        self.assertTrue((dest / "data" / "users.csv").exists())
        self.assertFalse((dest / "scripts").exists())  # stayed compressed

    def test_extract_unknown_member_fails(self):
        self._init()
        with self.assertRaises(vault.VaultError):
            vault.open_vault().extract(["nope.txt"], self.tmp)

    def test_per_file_cap_blocks_oversized_members(self):
        self._init()
        self.set_config(**{"vault__max_file_mb": 0})
        with self.assertRaises(vault.VaultError):
            vault.open_vault().extract(["data/users.csv"], self.tmp)

    def test_repack_replaces_adds_and_deletes(self):
        self._init()
        vlt = vault.open_vault()
        new = self.write("new.txt", "hello")
        changed = self.write("users.csv", "name,age\nonly,1\n")
        result = vlt.repack_changes({"data/users.csv": str(changed)},
                                    {"new.txt": str(new)},
                                    {"assets_readme.txt"})
        self.assertTrue(result["changed"])
        self.assertTrue(os.path.exists(result["backup"]))
        names = sorted(n for n, _ in vault.open_vault().index())
        self.assertEqual(names, ["data/users.csv", "new.txt",
                                 "scripts/report.py"])
        dest = self.tmp / "out"
        dest.mkdir()
        vault.open_vault().extract(["data/users.csv"], dest)
        self.assertIn("only", (dest / "data" / "users.csv").read_text())

    def test_repack_never_duplicates_members(self):
        """Regression: re-adding an existing member used to append a second
        entry with the same name, so the archive grew on every run."""
        self._init()
        local = self.write("report.txt", "v1")
        for i in range(3):
            local.write_text(f"v{i}", encoding="utf-8")
            vault.open_vault().repack_changes({}, {"out/report.txt": str(local)},
                                              set())
        names = zipfile.ZipFile(vault.archive_path()).namelist()
        self.assertEqual(len(names), len(set(names)), names)
        self.assertEqual(names.count("out/report.txt"), 1)

    def test_repack_without_changes_is_a_noop(self):
        self._init()
        self.assertEqual(vault.open_vault().repack_changes({}, {}, set()),
                         {"changed": False})

    def test_delete_wins_over_replace(self):
        self._init()
        local = self.write("x.txt", "ignored")
        vault.open_vault().repack_changes({"data/users.csv": str(local)}, {},
                                          {"data/users.csv"})
        self.assertNotIn("data/users.csv",
                         [n for n, _ in vault.open_vault().index()])

    def test_backups_are_rotated(self):
        self._init()
        self.set_config(**{"vault__keep_backups": 2})
        local = self.write("x.txt", "x")
        for i in range(4):
            local.write_text(str(i), encoding="utf-8")
            vault.open_vault().repack_changes({}, {"x.txt": str(local)}, set())
        bdir = vault.paths.backup_dir() / "vault"
        self.assertLessEqual(len(list(bdir.glob("*.bak"))), 2)

    def test_tar_gz_archives_work_too(self):
        path = self._init(fmt="tar.gz")
        self.assertTrue(str(path).endswith(".tar.gz"))
        vlt = vault.open_vault()
        self.assertIsInstance(vlt, vault.TarVault)
        dest = self.tmp / "scratch-tar"
        dest.mkdir()
        vlt.extract(["scripts/report.py"], dest)
        self.assertTrue((dest / "scripts" / "report.py").exists())
        local = self.write("extra.txt", "tar!")
        vlt.repack_changes({}, {"extra.txt": str(local)}, set())
        names = [n for n, _ in vault.open_vault().index()]
        self.assertIn("extra.txt", names)
        self.assertEqual(len(names), len(set(names)))

    def test_unsupported_archive_type(self):
        bad = self.write("vault.rar", "x")
        self.set_config(**{"vault__path": str(bad)})
        with self.assertRaises(vault.VaultError):
            vault.open_vault()

    def test_missing_archive_explains_itself(self):
        self.set_config(**{"vault__path": str(self.tmp / "nope.zip")})
        with self.assertRaises(vault.VaultError) as cm:
            vault.open_vault()
        self.assertIn("vault-init", str(cm.exception))


class TestScratch(ToolTestCase):
    def test_scratch_dir_is_private_and_cleanable(self):
        self.set_config(**{"vault__scratch": str(self.tmp / "scratch-root")})
        d = vault.new_scratch_dir()
        self.assertTrue(d.is_dir())
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        os.utime(d, (0, 0))                      # pretend it is old
        self.assertEqual(vault.clean_stale_scratch(), 1)
        self.assertFalse(d.exists())

    def test_archive_lock_is_exclusive(self):
        archive = self.write("vault.zip", "")
        with vault.ArchiveLock(archive):
            with self.assertRaises(vault.Busy):
                with vault.ArchiveLock(archive, timeout=0.3):
                    pass


class TestVaultRouter(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.use_mock_brain()
        vault.init_from_dir(str(self.make_project()))

    def test_full_lazy_load_transaction(self):
        task_id = db.record_task("vault: generate the report", status="running")
        ok, summary = vault_router.execute_vault_task(
            task_id, "generate the report", yes=True, quiet=True)
        self.assertTrue(ok, summary)

        names = [n for n, _ in vault.open_vault().index()]
        self.assertIn("out/report.txt", names)          # repacked result
        self.assertEqual(len(names), len(set(names)))

        actions = [r["action"] for r in db.vault_log_recent(10)]
        for step in ("extract", "diff", "repack", "cleanup"):
            self.assertIn(step, actions)

        # scratch dir was removed again
        cleanup = [r for r in db.vault_log_recent(10)
                   if r["action"] == "cleanup"][0]
        self.assertFalse(os.path.exists(cleanup["detail"]))

    def test_second_run_updates_instead_of_duplicating(self):
        for _ in range(2):
            tid = db.record_task("vault: report", status="running")
            vault_router.execute_vault_task(tid, "generate the report",
                                            yes=True, quiet=True)
        names = zipfile.ZipFile(vault.archive_path()).namelist()
        self.assertEqual(len(names), len(set(names)), names)

    def test_ai_index_is_cached(self):
        self.assertEqual(vault_router.refresh_ai_index(quiet=True), 3)
        self.assertEqual(len(db.vault_get_purposes()), 3)

    def test_queued_vault_task_runs_in_the_background(self):
        from my_ai_tool import runner
        tid, _, _ = vault_router.do_vault_task("generate the report",
                                               queue=True)
        self.assertEqual(db.get_task(tid)["status"], "queued")
        runner.run_queued(quiet=True)
        self.assertEqual(db.get_task(tid)["status"], "done")
