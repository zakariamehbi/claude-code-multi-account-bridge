"""Leaf folders, app-wide files and the Cowork tree, on synthetic trees."""

import contextlib
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
        with open(os.path.join(backup, bridge.APP_FILES_BACKUP_DIRNAME,
                               "claude_desktop_config.json"), "rb") as handle:
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
