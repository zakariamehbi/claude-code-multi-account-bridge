"""Leaf folders, app-wide files and the Cowork tree, on synthetic trees."""

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

import bridge_testlib as tl
import bridge

A, B = tl.uid(1), tl.uid(2)
ORG_A, ORG_B = tl.uid(11), tl.uid(22)


class StateCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state_dir = os.path.join(self.base, "state")
        self.root = tl.make_root(self.base)
        self.a = tl.make_leaf(self.base, A, ORG_A)
        self.b = tl.make_leaf(self.base, B, ORG_B)

    def sync(self, app_running=False):
        return bridge.sync_once(self.root, execute=True, state_dir=self.state_dir,
                                app_running=app_running)

    def sync_app(self, app_running=False):
        return bridge.sync_app_files(self.base, [self.a, self.b], self.state_dir,
                                     execute=True, app_running=app_running)

    def set_active(self, account):
        tl.write_other(self.base, "config.json", json.dumps(
            {"lastKnownAccountUuid": account}).encode("utf-8"))

    def write_json(self, name, data, indent=2):
        return tl.write_other(self.base, name, json.dumps(
            data, indent=indent).encode("utf-8"))

    def read_json(self, name):
        with open(os.path.join(self.base, name), "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))


def queued(leaf, session_id, mtime_ns, body=b'{"v":1}'):
    return tl.write_other(os.path.join(leaf, "waiting-input"),
                          "local_%s.json" % session_id, body, mtime_ns)


class LeafFoldersTest(StateCase):
    def test_queued_input_is_copied_to_the_other_account(self):
        path = queued(self.a, tl.uid(5), 5_000_000_000)

        result = self.sync()

        copy = os.path.join(self.b, "waiting-input", os.path.basename(path))
        with open(copy, "rb") as handle:
            self.assertEqual(handle.read(), b'{"v":1}')
        self.assertEqual(os.lstat(copy).st_mtime_ns, 5_000_000_000)
        self.assertEqual(result.applied["dir_files_written"], 1)

    def test_input_consumed_on_one_account_is_not_sent_again_from_the_other(self):
        path = queued(self.a, tl.uid(5), 5_000_000_000)
        self.sync()
        os.unlink(path)

        result = self.sync()

        self.assertEqual(result.applied["dir_files_deleted"], 1)
        self.assertEqual(os.listdir(os.path.join(self.b, "waiting-input")), [])

    def test_newest_copy_wins(self):
        queued(self.a, tl.uid(5), 5_000_000_000, b"old")
        queued(self.b, tl.uid(5), 6_000_000_000, b"new")

        self.sync()

        with open(os.path.join(self.a, "waiting-input",
                               "local_%s.json" % tl.uid(5)), "rb") as handle:
            self.assertEqual(handle.read(), b"new")

    def test_memory_files_are_mirrored_with_their_subfolders(self):
        tl.write_other(os.path.join(self.a, "memory", "notes"), "a.md", b"x",
                       5_000_000_000)

        self.sync()

        self.assertTrue(os.path.isfile(os.path.join(self.b, "memory", "notes", "a.md")))

    def test_the_loaded_account_waits_for_the_app_to_quit(self):
        self.set_active(B)
        queued(self.a, tl.uid(5), 5_000_000_000)

        result = self.sync(app_running=True)

        self.assertFalse(os.path.exists(os.path.join(self.b, "waiting-input")))
        self.assertEqual(result.dirs.held_back, (self.b,))

    def test_converged_folders_write_nothing(self):
        queued(self.a, tl.uid(5), 5_000_000_000)
        self.sync()
        before = tl.snapshot(self.root)

        result = self.sync()

        self.assertEqual((result.dirs.writes, result.dirs.deletes), ((), ()))
        self.assertEqual(tl.snapshot(self.root), before)


class AppFilesTest(StateCase):
    def config(self, sections_a, sections_b=None, extra=None):
        prefs = {"epitaxyPrefs": {"dframe-code-sections": {
            "%s/%s" % (A, ORG_A): sections_a}},
            "bypassPermissionsOptInByAccount": {A: True},
            "coworkModelAutoFallbackByAccount": {A: False, B: True}}
        if sections_b is not None:
            prefs["epitaxyPrefs"]["dframe-code-sections"]["%s/%s" % (B, ORG_B)] = sections_b
        prefs.update(extra or {})
        return self.write_json("claude_desktop_config.json",
                               {"preferences": prefs, "other": 1})

    def test_sidebar_sections_reach_an_account_that_has_none(self):
        self.config({"sections": [{"id": "pinned"}]})

        self.sync_app()

        sections = self.read_json("claude_desktop_config.json")[
            "preferences"]["epitaxyPrefs"]["dframe-code-sections"]
        self.assertEqual(sections["%s/%s" % (B, ORG_B)], {"sections": [{"id": "pinned"}]})

    def test_bypass_permissions_and_unlisted_keys_are_never_touched(self):
        self.config({"sections": []})

        self.sync_app()

        data = self.read_json("claude_desktop_config.json")
        self.assertEqual(data["preferences"]["bypassPermissionsOptInByAccount"], {A: True})
        self.assertEqual(data["other"], 1)

    def test_a_conflict_with_no_history_goes_to_the_loaded_account(self):
        self.set_active(B)
        self.config({"sections": []})

        self.sync_app()

        self.assertEqual(self.read_json("claude_desktop_config.json")["preferences"][
            "coworkModelAutoFallbackByAccount"], {A: True, B: True})

    def test_the_account_that_changed_a_value_wins_afterwards(self):
        self.set_active(B)
        self.config({"sections": [{"id": "a"}]})
        self.sync_app()
        data = self.read_json("claude_desktop_config.json")
        data["preferences"]["coworkModelAutoFallbackByAccount"][A] = False
        self.write_json("claude_desktop_config.json", data)

        self.sync_app()

        self.assertEqual(self.read_json("claude_desktop_config.json")["preferences"][
            "coworkModelAutoFallbackByAccount"], {A: False, B: False})

    def test_presets_union_by_id_and_a_delete_carries_across(self):
        self.write_json("ccd-session-presets.json", {"byAccount": {
            A: {"presets": [{"id": "p1"}, {"id": "p2"}], "builtIns": {}},
            B: {"presets": [{"id": "p3"}], "builtIns": {}}}})
        self.sync_app()
        data = self.read_json("ccd-session-presets.json")
        self.assertEqual(sorted(p["id"] for p in data["byAccount"][B]["presets"]),
                         ["p1", "p2", "p3"])
        data["byAccount"][B]["presets"] = [p for p in data["byAccount"][B]["presets"]
                                           if p["id"] != "p2"]
        self.write_json("ccd-session-presets.json", data)

        self.sync_app()

        data = self.read_json("ccd-session-presets.json")
        for account in (A, B):
            self.assertEqual(sorted(p["id"] for p in data["byAccount"][account]["presets"]),
                             ["p1", "p3"])

    def test_tool_toggles_are_merged_as_a_set(self):
        self.write_json("mcp-user-tool-toggles.json",
                        {"v": 3, "owners": {A: ["x", "y"], B: ["z"]}})

        self.sync_app()

        owners = self.read_json("mcp-user-tool-toggles.json")["owners"]
        self.assertEqual(sorted(owners[A]), ["x", "y", "z"])
        self.assertEqual(sorted(owners[B]), ["x", "y", "z"])

    def test_nothing_is_written_while_the_app_runs_and_a_backup_precedes_writes(self):
        path = self.config({"sections": [{"id": "pinned"}]})
        with open(path, "rb") as handle:
            original = handle.read()

        plan, backup, _ = self.sync_app(app_running=True)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(plan.held_back, ("claude_desktop_config.json",))
        self.assertIsNone(backup)

        _, backup, applied = self.sync_app()
        self.assertEqual(applied["app_files_written"], 1)
        with open(os.path.join(backup, "claude_desktop_config.json"), "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_the_file_keeps_its_indent(self):
        self.write_json("mcp-user-tool-toggles.json",
                        {"v": 3, "owners": {A: ["x"]}}, indent="\t")

        self.sync_app()

        with open(os.path.join(self.base, "mcp-user-tool-toggles.json"), "rb") as handle:
            self.assertIn(b'\n\t"v": 3', handle.read())

    def test_converged_files_write_nothing(self):
        self.config({"sections": [{"id": "pinned"}]})
        self.sync_app()

        plan, backup, applied = self.sync_app()

        self.assertEqual((plan.writes, backup, applied), ((), None, None))


class CoworkTreeTest(StateCase):
    def test_the_cli_syncs_the_cowork_tree_too(self):
        cowork_a = os.path.join(self.base, bridge.COWORK_DIRNAME, A, ORG_A)
        cowork_b = os.path.join(self.base, bridge.COWORK_DIRNAME, B, ORG_B)
        os.makedirs(cowork_a)
        os.makedirs(cowork_b)
        sid = tl.uid(5)
        tl.write_record(cowork_a, sid, mtime_ns=5_000_000_000)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bridge.main(["sync", "--execute", "--user-data", self.base,
                                "--state-dir", self.state_dir])

        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(cowork_b, "local_%s.json" % sid)))
        self.assertIn("sync %s (execute)" % bridge.COWORK_DIRNAME, out.getvalue())


if __name__ == "__main__":
    unittest.main()


class BackupRingTest(StateCase):
    def backups(self):
        root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)
        return sorted(os.listdir(root)) if os.path.isdir(root) else []

    def test_one_full_snapshot_then_only_the_files_a_run_overwrites(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000, title="one")
        self.sync()
        stale = tl.write_record(self.b, sid, mtime_ns=4_000_000_000, title="old")
        with open(stale, "rb") as handle:
            stale_bytes = handle.read()
        tl.write_record(self.a, sid, mtime_ns=6_000_000_000, title="two")

        self.sync()

        full, changes = self.backups()
        self.assertTrue(changes.endswith(bridge.CHANGES_SUFFIX))
        saved = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME, changes,
                             os.path.relpath(stale, self.base))
        with open(saved, "rb") as handle:
            self.assertEqual(handle.read(), stale_bytes)
        self.assertEqual(sorted(os.listdir(os.path.join(
            self.state_dir, bridge.BACKUPS_DIRNAME, changes))),
            [bridge.SESSIONS_DIRNAME])

    def test_a_day_old_full_snapshot_is_renewed(self):
        old = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        os.makedirs(os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME,
                                 old.strftime(bridge.STAMP_FORMAT),
                                 bridge.SESSIONS_DIRNAME))
        self.assertTrue(bridge.needs_full_backup(self.root, self.state_dir))

    def test_the_two_rings_are_pruned_separately(self):
        root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)
        for index in range(3):
            os.makedirs(os.path.join(root, "20200101T0000%02d000000Z" % index))
            os.makedirs(os.path.join(root, "20200102T0000%02d000000Z%s" % (
                index, bridge.CHANGES_SUFFIX)))

        bridge.prune_backups(root, keep=1, changes_kept=2)

        self.assertEqual(self.backups(), [
            "20200101T000002000000Z",
            "20200102T000001000000Z" + bridge.CHANGES_SUFFIX,
            "20200102T000002000000Z" + bridge.CHANGES_SUFFIX])


class AgentFastPathTest(StateCase):
    def run_agent(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bridge.main(["sync", "--execute", "--if-changed", "--user-data",
                                self.base, "--state-dir", self.state_dir])
        return code, out.getvalue()

    def test_a_quiet_tree_prints_nothing_and_a_change_is_picked_up(self):
        code, out = self.run_agent()
        self.assertEqual((code, out), (0, ""))
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)

        code, out = self.run_agent()

        self.assertEqual(code, 0)
        self.assertIn("copies_written=1", out)
        self.assertTrue(os.path.exists(os.path.join(self.b, "local_%s.json" % sid)))

    def test_an_unchanged_tree_skips_the_run(self):
        self.run_agent()
        self.assertTrue(bridge.unchanged_since_last_run(self.base, self.state_dir))
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.assertFalse(bridge.unchanged_since_last_run(self.base, self.state_dir))

    def test_a_full_run_still_happens_every_interval(self):
        self.run_agent()
        later = __import__("time").time() + bridge.FULL_RUN_INTERVAL + 1
        self.assertFalse(bridge.unchanged_since_last_run(self.base, self.state_dir,
                                                          now=later))


class LogRotationTest(unittest.TestCase):
    def test_a_large_log_keeps_its_newest_lines(self):
        home = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, home, True)
        out_path, _ = bridge.log_paths(home)
        os.makedirs(os.path.dirname(out_path))
        with open(out_path, "wb") as handle:
            handle.write(b"old line\n" * (bridge.LOG_MAX_BYTES // 9 + 10))
            handle.write(b"newest line\n")

        bridge.rotate_logs(home)

        with open(out_path, "rb") as handle:
            kept = handle.read()
        self.assertLessEqual(len(kept), bridge.LOG_KEEP_BYTES)
        self.assertTrue(kept.endswith(b"newest line\n"))
        self.assertTrue(kept.startswith(b"old line\n"))


class LeafFolderReviewTest(StateCase):
    def test_a_consumed_message_is_not_resent_after_a_held_back_run(self):
        """Review #2."""
        path = queued(self.a, tl.uid(5), 5_000_000_000)
        self.sync()
        self.set_active(A)
        os.unlink(os.path.join(self.b, "waiting-input", os.path.basename(path)))

        self.sync(app_running=True)
        self.sync(app_running=True)
        self.assertEqual(os.listdir(os.path.join(self.b, "waiting-input")), [])

        self.sync(app_running=False)
        self.assertFalse(os.path.exists(path))

    def test_an_unlistable_subfolder_deletes_nothing_elsewhere(self):
        """Review #3."""
        for leaf in (self.a, self.b):
            tl.write_other(os.path.join(leaf, "memory", "notes"), "n.md", b"x",
                           5_000_000_000)
        self.sync()
        locked = os.path.join(self.a, "memory", "notes")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync()

        self.assertEqual((result.dirs.deletes, result.dirs.deleted), ((), ()))
        self.assertTrue(os.path.exists(os.path.join(self.b, "memory", "notes", "n.md")))

    def test_a_folder_the_app_removed_counts_as_emptied(self):
        """Review #4: the app removes an empty waiting-input/."""
        path = queued(self.a, tl.uid(5), 5_000_000_000)
        self.sync()
        os.unlink(path)
        os.rmdir(os.path.dirname(path))

        self.sync()

        self.assertFalse(os.path.exists(path))
        self.assertEqual(os.listdir(os.path.join(self.b, "waiting-input")), [])

    def test_a_symlinked_folder_is_left_alone_and_never_written_through(self):
        """Review #7."""
        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.b, "memory"))
        tl.write_other(os.path.join(self.a, "memory", "sub"), "s.md", b"x",
                       5_000_000_000)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            first = self.sync()
        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            second = self.sync()

        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(first.dirs.writes, ())
        self.assertEqual(second.dirs.writes, ())

    def test_temp_files_inside_leaf_folders_are_swept(self):
        """Review #10."""
        stale = tl.write_other(os.path.join(self.a, "memory", "sub"),
                               bridge.TEMP_PREFIX + "1-x", b"half")

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            self.sync()

        self.assertFalse(os.path.exists(stale))


class AppFilesReviewTest(StateCase):
    def fallback(self):
        return self.read_json("claude_desktop_config.json")["preferences"][
            "coworkModelAutoFallbackByAccount"]

    def write_fallback(self, values):
        self.write_json("claude_desktop_config.json", {"preferences": {
            "coworkModelAutoFallbackByAccount": values}})

    def test_a_change_made_while_the_app_runs_survives_until_it_quits(self):
        """Review #5a."""
        self.write_fallback({A: False, B: False})
        self.sync_app()
        self.write_fallback({A: True, B: False})
        self.set_active(B)

        self.sync_app(app_running=True)
        self.sync_app(app_running=False)

        self.assertEqual(self.fallback(), {A: True, B: True})

    def test_a_preset_deleted_while_the_app_runs_stays_deleted(self):
        """Review #5b."""
        self.write_json("ccd-session-presets.json", {"byAccount": {
            A: {"presets": [{"id": "p1"}, {"id": "p2"}]},
            B: {"presets": [{"id": "p1"}, {"id": "p2"}]}}})
        self.sync_app()
        data = self.read_json("ccd-session-presets.json")
        data["byAccount"][A]["presets"] = [{"id": "p1"}]
        self.write_json("ccd-session-presets.json", data)

        self.sync_app(app_running=True)
        self.sync_app(app_running=False)

        for account in (A, B):
            self.assertEqual(self.read_json("ccd-session-presets.json")[
                "byAccount"][account]["presets"], [{"id": "p1"}])

    def test_a_new_accounts_default_does_not_overwrite_the_others(self):
        """Review #6."""
        self.write_fallback({A: True, B: True})
        self.sync_app()
        c = tl.make_leaf(self.base, tl.uid(3), tl.uid(33))
        self.write_fallback({A: True, B: True, tl.uid(3): False})
        self.set_active(tl.uid(3))

        bridge.sync_app_files(self.base, [self.a, self.b, c], self.state_dir,
                              execute=True, app_running=False)

        self.assertEqual(self.fallback(), {A: True, B: True, tl.uid(3): True})

    def test_mixed_last_used_types_do_not_crash(self):
        """Review #8."""
        self.write_json("ccd-session-presets.json", {"byAccount": {
            A: {"presets": [{"id": "p", "lastUsedAt": "yesterday"}]},
            B: {"presets": [{"id": "p", "lastUsedAt": 5}]}}})

        self.sync_app()

        self.assertEqual(self.read_json("ccd-session-presets.json")[
            "byAccount"][A]["presets"], [{"id": "p", "lastUsedAt": 5}])


class UnknownAccountTest(StateCase):
    def test_every_leaf_is_held_while_the_app_runs_with_no_known_account(self):
        """Review #9."""
        self.assertEqual(bridge.held_back_leaves(self.root, [self.a, self.b],
                                                 app_running=True),
                         (self.a, self.b))
