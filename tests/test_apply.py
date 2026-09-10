"""apply_plan / sync_once against real synthetic trees under a temp dir."""

import os
import shutil
import stat
import tempfile
import unittest

import bridge_testlib as tl
import bridge


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state_dir = os.path.join(self.base, "state")
        self.root = tl.make_root(self.base)
        self.a = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        self.b = tl.make_leaf(self.base, tl.uid(2), tl.uid(22))

    def sync(self, execute=False):
        return bridge.sync_once(self.root, execute=execute, state_dir=self.state_dir)

    def snap(self):
        return tl.snapshot(self.root)


class WriteTest(TreeCase):
    def test_missing_record_is_written_with_the_winner_bytes_and_mtime(self):
        sid = tl.uid(5)
        source = tl.write_record(self.a, sid, mtime_ns=5_123_456_789)
        with open(source, "rb") as fh:
            expected = fh.read()

        result = self.sync(execute=True)

        dest = os.path.join(self.b, "local_%s.json" % sid)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), expected)
        self.assertEqual(os.lstat(dest).st_mtime_ns, os.lstat(source).st_mtime_ns)
        self.assertEqual(result.plan.counts()["copies_written"], 1)

    def test_write_leaves_no_temp_files_behind(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.sync(execute=True)

        leftovers = [name for name in os.listdir(self.b) if name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_newer_copy_overwrites_the_older_one_in_place(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="new")
        tl.write_record(self.b, sid, mtime_ns=1_000_000_000, title="old")

        self.sync(execute=True)

        with open(os.path.join(self.b, "local_%s.json" % sid), "rb") as fh:
            self.assertIn(b"new", fh.read())
        self.assertEqual(
            os.lstat(os.path.join(self.b, "local_%s.json" % sid)).st_mtime_ns,
            9_000_000_000)

    def test_second_run_is_zero_writes(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        tl.write_record(self.b, tl.uid(6), mtime_ns=6_000_000_000)
        tl.write_tombstone(self.a, tl.uid(7), mtime_ns=7_000_000_000)

        first = self.sync(execute=True)
        self.assertGreater(first.plan.counts()["copies_written"], 0)
        before = self.snap()

        second = self.sync(execute=True)

        self.assertTrue(second.plan.is_empty())
        counts = second.plan.counts()
        self.assertEqual(counts["copies_written"], 0)
        self.assertEqual(counts["records_deleted"], 0)
        self.assertEqual(counts["tombstones_copied"], 0)
        self.assertEqual(counts["parse_failures"], 0)
        self.assertIsNone(second.backup)
        self.assertEqual(self.snap(), before)

    def test_archived_state_propagates_to_the_other_leaf(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=8_000_000_000,
                        last_activity=100, archived=True)
        tl.write_record(self.b, sid, mtime_ns=7_000_000_000,
                        last_activity=500, archived=False)

        self.sync(execute=True)

        merged = bridge.read_leaf(self.b).records[sid].parsed
        self.assertTrue(merged["isArchived"])


class DeleteTest(TreeCase):
    def test_tombstone_deletes_everywhere_and_is_copied_everywhere(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000)
        tl.write_record(self.b, sid, mtime_ns=8_000_000_000)
        tl.write_tombstone(self.a, sid, mtime_ns=5_000_000_000, content=b"1700000000009")

        self.sync(execute=True)

        self.assertFalse(os.path.exists(os.path.join(self.a, "local_%s.json" % sid)))
        self.assertFalse(os.path.exists(os.path.join(self.b, "local_%s.json" % sid)))
        copied = os.path.join(self.b, "deleted_%s" % sid)
        with open(copied, "rb") as fh:
            self.assertEqual(fh.read(), b"1700000000009")

    def test_a_tombstone_with_a_non_uuid_name_deletes_nothing(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000)
        tl.write_other(self.a, "deleted_not-a-uuid", content=b"1700000000000")
        tl.write_other(self.a, "deleted_%s.json" % sid, content=b"1700000000000")

        self.sync(execute=True)

        self.assertTrue(os.path.exists(os.path.join(self.a, "local_%s.json" % sid)))
        self.assertTrue(os.path.exists(os.path.join(self.b, "local_%s.json" % sid)))


class UnparsableTest(TreeCase):
    def test_unparsable_record_is_counted_logged_and_never_deleted(self):
        sid = tl.uid(5)
        broken = tl.write_record(self.a, sid, mtime_ns=9_000_000_000, raw=b"{ broken")

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            result = self.sync(execute=True)

        self.assertTrue(os.path.exists(broken))
        self.assertEqual(result.plan.counts()["parse_failures"], 1)
        self.assertTrue(any("local_%s.json" % sid in line for line in logs.output))

    def test_a_valid_copy_replaces_an_unparsable_one(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, raw=b"{ broken")
        tl.write_record(self.b, sid, mtime_ns=1_000_000_000, title="good")

        self.sync(execute=True)

        entry = bridge.read_leaf(self.a).records[sid]
        self.assertIsNotNone(entry.parsed)
        self.assertEqual(entry.parsed["title"], "good")


class SymlinkTest(TreeCase):
    def test_a_symlinked_leaf_is_never_written_through(self):
        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.root, tl.uid(1), tl.uid(12)))
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        self.sync(execute=True)

        self.assertEqual(os.listdir(outside), [])

    def test_a_symlinked_record_destination_is_skipped_not_followed(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="winner")
        target = tl.write_other(self.base, "target.json", content=b"untouched")
        link = os.path.join(self.b, "local_%s.json" % sid)
        os.symlink(target, link)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            self.sync(execute=True)

        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"untouched")
        self.assertTrue(os.path.islink(link))


class UntouchedFilesTest(TreeCase):
    def other_paths(self):
        paths = {}
        for leaf in (self.a, self.b):
            for rel in ("scheduled-tasks.json", "spaces.json", "backlog/item.json",
                        "arbitrary.txt", "local_not-a-uuid.json", "deleted_nope"):
                path = os.path.join(leaf, rel)
                if os.path.exists(path):
                    with open(path, "rb") as fh:
                        paths[path] = (fh.read(), os.lstat(path).st_mtime_ns)
        return paths

    def setUp(self):
        TreeCase.setUp(self)
        for leaf in (self.a, self.b):
            tl.write_other(leaf, "scheduled-tasks.json", b'{"tasks": []}', 1_000_000_000)
            tl.write_other(leaf, "spaces.json", b'{"spaces": []}', 1_000_000_000)
            tl.write_other(leaf, "backlog/item.json", b'{"x": 1}', 1_000_000_000)
            tl.write_other(leaf, "arbitrary.txt", b"hello", 1_000_000_000)
            tl.write_other(leaf, "local_not-a-uuid.json", b"{}", 1_000_000_000)
            tl.write_other(leaf, "deleted_nope", b"0", 1_000_000_000)
        tl.write_other(self.root, "root-level.json", b"{}", 1_000_000_000)

    def test_only_record_paths_appear_in_the_tree_diff(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        before = self.snap()

        self.sync(execute=True)

        after = self.snap()
        changed = {key for key in set(before) | set(after)
                   if before.get(key) != after.get(key)}
        expected = {os.path.relpath(os.path.join(self.b, "local_%s.json" % sid),
                                    self.root)}
        # directory mtimes move when an entry is added; ignore directories.
        changed = {key for key in changed
                   if after.get(key, before.get(key))[0] != "dir"}
        self.assertEqual(changed, expected)

    def test_non_record_files_are_never_touched(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        tl.write_record(self.b, tl.uid(6), mtime_ns=6_000_000_000)
        tl.write_tombstone(self.a, tl.uid(7), mtime_ns=7_000_000_000)
        before = self.other_paths()
        self.assertEqual(len(before), 12)
        root_file = os.path.join(self.root, "root-level.json")
        root_before = os.lstat(root_file).st_mtime_ns

        self.sync(execute=True)

        self.assertEqual(self.other_paths(), before)
        self.assertEqual(os.lstat(root_file).st_mtime_ns, root_before)


class PermissionTest(TreeCase):
    def test_an_unreadable_record_is_logged_and_blocked(self):
        """An unreadable record is a BLOCKED path, reported like every other.

        WARNING, not ERROR: nothing failed, the bridge declined to judge a copy
        it cannot read (B6), which is the same class of report as a directory
        wearing a record's name.
        """
        sid = tl.uid(5)
        path = tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o600)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            result = self.sync(execute=True)

        self.assertTrue(result.plan.is_empty())
        self.assertIn(path, "\n".join(logs.output))
        self.assertTrue(os.path.exists(path))

    def test_a_write_failure_is_counted_and_reported(self):
        """A write that fails at the last moment is an ERROR and a non-zero exit.

        The failure has to be injected: a leaf whose write bit is missing is
        caught at DISCOVERY now (P2-1) and no write is planned into it at all,
        precisely so the plan can converge.
        """
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        original = os.replace

        def explode(src, dst, *args, **kwargs):
            raise OSError(1, "rename refused")

        os.replace = explode
        self.addCleanup(setattr, os, "replace", original)

        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            result = self.sync(execute=True)

        self.assertEqual(result.applied["errors"], 1)
        self.assertEqual(result.applied["copies_written"], 0)


class DryRunTest(TreeCase):
    def test_dry_run_changes_nothing_but_reports_the_same_plan(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        tl.write_record(self.b, tl.uid(6), mtime_ns=6_000_000_000)
        tl.write_tombstone(self.b, tl.uid(7), mtime_ns=7_000_000_000)
        before = self.snap()

        dry = self.sync(execute=False)

        self.assertEqual(self.snap(), before)
        self.assertIsNone(dry.backup)
        self.assertFalse(dry.plan.is_empty())

        wet = self.sync(execute=True)
        self.assertEqual(dry.plan, wet.plan)
        self.assertNotEqual(self.snap(), before)

    def test_dry_run_creates_no_state_directory(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.sync(execute=False)
        self.assertFalse(os.path.exists(self.state_dir))


class EmptyTreeTest(TreeCase):
    def test_a_missing_root_is_a_clean_empty_plan(self):
        shutil.rmtree(self.root)
        result = bridge.sync_once(self.root, execute=True, state_dir=self.state_dir)

        self.assertEqual(result.plan.leaves, ())
        self.assertTrue(result.plan.is_empty())
        self.assertIsNone(result.backup)


class BlockedDestinationTest(TreeCase):
    """P-F3/P-F4: an unwritable destination must not keep the plan non-empty.

    A plan that can never converge means a FULL BACKUP on every single run,
    which shreds the backup ring, plus a temp file created (and unlinked) in
    the leaf on every run, which bumps the leaf mtime and re-fires WatchPaths.
    """

    def backups(self):
        root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)
        return sorted(os.listdir(root)) if os.path.isdir(root) else []

    def test_a_directory_named_like_the_destination_record_blocks_the_write(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        os.makedirs(os.path.join(self.b, "local_%s.json" % sid))

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync(execute=True)

        self.assertTrue(result.plan.is_empty())
        self.assertEqual(result.plan.counts()["blocked"], 1)
        self.assertEqual(self.backups(), [])
        self.assertEqual([n for n in os.listdir(self.b) if n.startswith(".")], [])

    def test_a_symlinked_destination_record_blocks_the_write(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        target = tl.write_other(self.base, "target.json", content=b"untouched")
        os.symlink(target, os.path.join(self.b, "local_%s.json" % sid))

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync(execute=True)

        self.assertTrue(result.plan.is_empty())
        self.assertEqual(result.plan.counts()["blocked"], 1)
        self.assertEqual(self.backups(), [])
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"untouched")

    def test_a_symlinked_record_under_a_tombstone_blocks_the_delete(self):
        sid = tl.uid(5)
        target = tl.write_other(self.base, "target.json", content=b"untouched")
        os.symlink(target, os.path.join(self.b, "local_%s.json" % sid))
        tl.write_tombstone(self.a, sid, mtime_ns=5_000_000_000)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync(execute=True)

        self.assertEqual(result.plan.deletes, ())
        self.assertEqual(result.plan.counts()["blocked"], 1)
        self.assertTrue(os.path.exists(target))

    def test_a_directory_named_like_the_destination_tombstone_blocks_the_copy(self):
        sid = tl.uid(5)
        tl.write_tombstone(self.a, sid, mtime_ns=5_000_000_000)
        os.makedirs(os.path.join(self.b, "deleted_%s" % sid))

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync(execute=True)

        self.assertTrue(result.plan.is_empty())
        self.assertEqual(result.plan.counts()["blocked"], 1)
        self.assertEqual(self.backups(), [])

    def test_a_blocked_destination_still_converges_the_other_leaves(self):
        sid = tl.uid(5)
        c = tl.make_leaf(self.base, tl.uid(3), tl.uid(33))
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        os.makedirs(os.path.join(self.b, "local_%s.json" % sid))

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            self.sync(execute=True)

        self.assertTrue(os.path.isfile(os.path.join(c, "local_%s.json" % sid)))
        self.assertTrue(self.sync(execute=True).plan.is_empty())


class DeleteGuardTest(TreeCase):
    """N4: the apply-time guard, tested by handing apply_plan a bad Delete.

    plan_sync can only ever emit `local_<uuid>.json` deletes, so the guard is
    unreachable through the planner. It is the last line of defence and has to
    be tested directly.
    """

    def test_a_delete_of_a_non_record_path_is_refused_and_counted(self):
        victim = tl.write_other(self.a, "scheduled-tasks.json",
                                b'{"tasks": []}', 1_000_000_000)
        plan = bridge.Plan(
            leaves=(self.a,), writes=(), tombstone_copies=(),
            deletes=(bridge.Delete(victim, tl.uid(5), "hand-built"),),
            records_seen=0, parse_failures=(), blocked=(), blocked_leaves=(),
            ignored=(), id_mismatches=())

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            applied = bridge.apply_plan(plan)

        self.assertEqual(applied["skipped"], 1)
        self.assertEqual(applied["records_deleted"], 0)
        self.assertTrue(os.path.exists(victim))
        self.assertEqual(os.lstat(victim).st_mtime_ns, 1_000_000_000)
        self.assertIn(victim, "\n".join(logs.output))

    def test_a_delete_of_a_non_uuid_record_name_is_refused(self):
        victim = tl.write_other(self.a, "local_not-a-uuid.json", b"{}")
        plan = bridge.Plan(
            leaves=(self.a,), writes=(), tombstone_copies=(),
            deletes=(bridge.Delete(victim, tl.uid(5), "hand-built"),),
            records_seen=0, parse_failures=(), blocked=(), blocked_leaves=(),
            ignored=(), id_mismatches=())

        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            applied = bridge.apply_plan(plan)

        self.assertEqual(applied["skipped"], 1)
        self.assertTrue(os.path.exists(victim))


class ApplyTimeDestinationCheckTest(TreeCase):
    """B1: the destination is re-checked immediately before os.replace.

    Between planning and applying, the desktop app can write a NEWER copy into
    the destination. Overwriting it would lose that copy for good AND stamp the
    older winner's mtime on it, so the next run would not even notice.
    """

    def plan(self):
        return bridge.plan_sync(bridge.gather(self.root))

    def test_a_destination_changed_after_planning_is_not_overwritten(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="winner")
        tl.write_record(self.b, sid, mtime_ns=1_000_000_000, title="stale")
        plan = self.plan()
        self.assertEqual(len(plan.writes), 1)

        dest = tl.write_record(self.b, sid, mtime_ns=9_500_000_000, title="fresh")
        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            applied = bridge.apply_plan(plan)

        self.assertEqual(applied["skipped"], 1)
        self.assertEqual(applied["copies_written"], 0)
        with open(dest, "rb") as handle:
            self.assertIn(b"fresh", handle.read())
        self.assertEqual(os.lstat(dest).st_mtime_ns, 9_500_000_000)
        self.assertIn(dest, "\n".join(logs.output))

    def test_a_destination_that_appeared_after_planning_is_not_overwritten(self):
        """The ABSENT direction: two runs racing, or the app creating the file."""
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="winner")
        plan = self.plan()
        self.assertEqual(plan.writes[0].expected, bridge.ABSENT)

        dest = tl.write_record(self.b, sid, mtime_ns=9_500_000_000, title="fresh")
        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            applied = bridge.apply_plan(plan)

        self.assertEqual(applied["skipped"], 1)
        self.assertEqual(applied["copies_written"], 0)
        with open(dest, "rb") as handle:
            self.assertIn(b"fresh", handle.read())

    def test_an_untouched_destination_is_still_written(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="winner")
        tl.write_record(self.b, sid, mtime_ns=1_000_000_000, title="stale")

        applied = bridge.apply_plan(self.plan())

        self.assertEqual(applied["copies_written"], 1)
        self.assertEqual(applied["skipped"], 0)
        dest = os.path.join(self.b, "local_%s.json" % sid)
        with open(dest, "rb") as handle:
            self.assertIn(b"winner", handle.read())

    def test_the_check_is_not_extended_to_deletes(self):
        """A tombstoned record is deleted even if it changed after planning.

        The tombstone is final and the id is never reused, so a newer copy of a
        deleted session is still a deleted session.
        """
        sid = tl.uid(5)
        doomed = tl.write_record(self.a, sid, mtime_ns=1_000_000_000)
        tl.write_tombstone(self.a, sid, mtime_ns=5_000_000_000)
        plan = self.plan()
        self.assertEqual([d.path for d in plan.deletes], [doomed])

        tl.write_record(self.a, sid, mtime_ns=9_500_000_000, title="fresh")
        applied = bridge.apply_plan(plan)

        self.assertEqual(applied["records_deleted"], 1)
        self.assertFalse(os.path.exists(doomed))


class DestPreStateTest(TreeCase):
    """Gap 13: ABSENT is for ENOENT and nothing else.

    ABSENT means "there was nothing here, so create it". Returning it for a
    destination we merely could not LOOK at would turn "I don't know" into
    permission to overwrite. Everything else is None, which never compares
    equal to any `expected`, so the write is refused.
    """

    def test_a_missing_name_in_a_real_directory_is_absent(self):
        self.assertIs(bridge._dest_pre_state(
            os.path.join(self.b, "local_%s.json" % tl.uid(5))), bridge.ABSENT)

    def test_a_missing_parent_directory_is_absent_too(self):
        """Still ENOENT, and still nothing there to lose."""
        self.assertIs(bridge._dest_pre_state(
            os.path.join(self.b, "gone", "local_%s.json" % tl.uid(5))),
            bridge.ABSENT)

    def test_a_parent_that_is_a_file_is_not_absent(self):
        """ENOTDIR, not ENOENT: an lstat we could not perform, so we know
        nothing about the destination."""
        parent = tl.write_other(self.b, "not-a-directory", b"x")

        self.assertIsNone(bridge._dest_pre_state(
            os.path.join(parent, "local_%s.json" % tl.uid(5))))

    def test_an_unlistable_parent_directory_is_not_absent(self):
        cage = os.path.join(self.b, "cage")
        os.makedirs(cage)
        os.chmod(cage, 0o000)
        self.addCleanup(os.chmod, cage, 0o700)

        self.assertIsNone(bridge._dest_pre_state(
            os.path.join(cage, "local_%s.json" % tl.uid(5))))

    def test_an_unreadable_destination_file_is_not_absent(self):
        path = tl.write_record(self.b, tl.uid(5), mtime_ns=5_000_000_000)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o600)

        self.assertIsNone(bridge._dest_pre_state(path))

    def test_a_directory_and_an_oversize_file_are_not_absent(self):
        directory = os.path.join(self.b, "local_%s.json" % tl.uid(6))
        os.makedirs(directory)
        big = tl.write_record(self.b, tl.uid(7), mtime_ns=5_000_000_000)
        original = bridge.MAX_RECORD_BYTES
        bridge.MAX_RECORD_BYTES = 8
        self.addCleanup(setattr, bridge, "MAX_RECORD_BYTES", original)

        self.assertIsNone(bridge._dest_pre_state(directory))
        self.assertIsNone(bridge._dest_pre_state(big))

    def test_a_readable_file_reports_its_mtime_and_digest(self):
        path = tl.write_record(self.b, tl.uid(5), mtime_ns=5_123_456_789)
        with open(path, "rb") as handle:
            digest = bridge.hashlib.sha256(handle.read()).hexdigest()

        self.assertEqual(bridge._dest_pre_state(path), (5_123_456_789, digest))

    def test_an_unreadable_destination_is_therefore_never_overwritten(self):
        """The end-to-end consequence, through apply_plan."""
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="winner")
        dest = tl.write_record(self.b, sid, mtime_ns=1_000_000_000, title="stale")
        plan = bridge.plan_sync(bridge.gather(self.root))
        self.assertEqual(len(plan.writes), 1)
        os.chmod(dest, 0o000)
        self.addCleanup(os.chmod, dest, 0o600)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            applied = bridge.apply_plan(plan)

        self.assertEqual(applied["skipped"], 1)
        self.assertEqual(applied["copies_written"], 0)
        self.assertIn("unreadable", "\n".join(logs.output))
        os.chmod(dest, 0o600)
        with open(dest, "rb") as handle:
            self.assertIn(b"stale", handle.read())


class ApplyTimeGuardTest(TreeCase):
    """Gap 16: the last-line-of-defence guards, each reached on purpose.

    plan_sync cannot emit any of these, so they are only reachable by handing
    apply_plan a plan built by hand — and each test checks WHICH guard spoke,
    not merely that something was skipped.
    """

    def plan_with(self, writes=(), deletes=()):
        return bridge.Plan(
            leaves=(self.a, self.b), writes=tuple(writes), tombstone_copies=(),
            deletes=tuple(deletes), records_seen=0, parse_failures=(),
            blocked=(), blocked_leaves=(), ignored=(), id_mismatches=())

    def write_to(self, dest, expected=bridge.ABSENT):
        return bridge.Write(dest, b'{"id": "x"}', 5_000_000_000,
                            os.path.join(self.a, "source.json"), "record",
                            expected)

    def test_a_symlinked_destination_is_refused_by_its_own_guard(self):
        target = tl.write_other(self.base, "target.json", b"untouched")
        link = os.path.join(self.b, "local_%s.json" % tl.uid(5))
        os.symlink(target, link)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            applied = bridge.apply_plan(self.plan_with(
                writes=[self.write_to(link)]))

        self.assertEqual(applied["skipped"], 1)
        self.assertEqual(applied["copies_written"], 0)
        self.assertIn("destination is a symlink", "\n".join(logs.output))
        self.assertTrue(os.path.islink(link))
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"untouched")

    def test_a_parent_that_is_not_a_directory_is_refused_by_its_own_guard(self):
        parent = tl.write_other(self.b, "not-a-directory", b"keep me")

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            applied = bridge.apply_plan(self.plan_with(writes=[
                self.write_to(os.path.join(parent,
                                           "local_%s.json" % tl.uid(5)))]))

        self.assertEqual(applied["skipped"], 1)
        self.assertIn("not a real directory", "\n".join(logs.output))
        with open(parent, "rb") as handle:
            self.assertEqual(handle.read(), b"keep me")

    def test_a_symlinked_parent_directory_is_refused_too(self):
        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)
        link = os.path.join(self.b, "linked-leaf")
        os.symlink(outside, link)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            applied = bridge.apply_plan(self.plan_with(writes=[
                self.write_to(os.path.join(link,
                                           "local_%s.json" % tl.uid(5)))]))

        self.assertEqual(applied["skipped"], 1)
        self.assertIn("not a real directory", "\n".join(logs.output))
        self.assertEqual(os.listdir(outside), [])

    def test_a_delete_of_a_symlink_is_refused_by_the_symlink_guard(self):
        """The name passes the record guard, so this is the NEXT guard's job."""
        target = tl.write_other(self.base, "target.json", b"untouched")
        link = os.path.join(self.b, "local_%s.json" % tl.uid(5))
        os.symlink(target, link)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            applied = bridge.apply_plan(self.plan_with(deletes=[
                bridge.Delete(link, tl.uid(5), "hand-built")]))

        self.assertEqual(applied["skipped"], 1)
        self.assertEqual(applied["records_deleted"], 0)
        joined = "\n".join(logs.output)
        self.assertIn("it is a symlink", joined)
        self.assertNotIn("not a local_", joined)
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.exists(target))

    def test_a_missing_delete_target_is_skipped_without_an_error(self):
        """Somebody else got there first; nothing failed."""
        applied = bridge.apply_plan(self.plan_with(deletes=[
            bridge.Delete(os.path.join(self.b, "local_%s.json" % tl.uid(5)),
                          tl.uid(5), "hand-built")]))

        self.assertEqual(applied["skipped"], 1)
        self.assertEqual(applied["errors"], 0)


class WriteRequiresExpectedTest(unittest.TestCase):
    """Gap 18: `Write.expected` has NO default, on purpose.

    A default would let a hand-built Write (or a new call site) silently take
    the "it changed under us" branch — or worse, claim ABSENT and overwrite.
    """

    def test_constructing_a_write_without_expected_is_a_type_error(self):
        with self.assertRaises(TypeError):
            bridge.Write("/synthetic/leaf/local_x.json", b"{}", 1, "src",
                         "record")

    def test_the_field_is_still_positional_and_last(self):
        write = bridge.Write("/synthetic/leaf/local_x.json", b"{}", 1, "src",
                             "record", bridge.ABSENT)
        self.assertIs(write.expected, bridge.ABSENT)
        self.assertEqual(bridge.Write._fields[-1], "expected")


class WriteMechanicsTest(TreeCase):
    """N7 / N11 / N2 / fsync: how the bytes actually land."""

    def test_the_temp_file_is_created_inside_the_destination_leaf(self):
        """os.replace is only atomic within one filesystem; same dir guarantees it."""
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        seen = []
        original = os.replace

        def spy(src, dst, *args, **kwargs):
            seen.append((src, dst))
            return original(src, dst, *args, **kwargs)

        os.replace = spy
        self.addCleanup(setattr, os, "replace", original)

        self.sync(execute=True)

        self.assertEqual(len(seen), 1)
        src, dst = seen[0]
        self.assertEqual(os.path.dirname(src), os.path.dirname(dst))
        self.assertEqual(os.path.dirname(src), self.b)
        self.assertTrue(os.path.basename(src).startswith(bridge.TEMP_PREFIX))

    def test_an_existing_destination_keeps_its_file_mode(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="winner")
        dest = tl.write_record(self.b, sid, mtime_ns=1_000_000_000, title="stale")
        os.chmod(dest, 0o600)

        self.sync(execute=True)

        self.assertEqual(stat.S_IMODE(os.lstat(dest).st_mode), 0o600)
        with open(dest, "rb") as handle:
            self.assertIn(b"winner", handle.read())

    def test_a_failing_utime_counts_the_copy_and_reports_an_error(self):
        """The bytes landed, but the mtime did not: that copy now looks NEWEST.

        Left silent it would win the next merge and propagate everywhere, so it
        counts as a written copy AND as an error (non-zero exit).
        """
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        original = os.utime

        def explode(path, *args, **kwargs):
            # only the live tree: the backup's own copystat must still work
            if str(path).startswith(self.b) and "local_" in str(path):
                raise OSError(1, "utime refused")
            return original(path, *args, **kwargs)

        os.utime = explode
        self.addCleanup(setattr, os, "utime", original)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            result = self.sync(execute=True)

        self.assertEqual(result.applied["copies_written"], 1)
        self.assertEqual(result.applied["errors"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.b, "local_%s.json" % sid)))
        self.assertIn("mtime", "\n".join(logs.output))

    def test_the_destination_directory_is_fsynced_after_the_rename(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        kinds = []
        original = os.fsync

        def spy(fd):
            kinds.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return original(fd)

        os.fsync = spy
        self.addCleanup(setattr, os, "fsync", original)

        self.sync(execute=True)

        self.assertIn(True, kinds)

    def test_a_failing_directory_fsync_does_not_fail_the_write(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        original = os.fsync

        def spy(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(1, "no fsync on directories here")
            return original(fd)

        os.fsync = spy
        self.addCleanup(setattr, os, "fsync", original)

        result = self.sync(execute=True)

        self.assertEqual(result.applied["copies_written"], 1)
        self.assertEqual(result.applied["errors"], 0)


class StaleTempFileTest(TreeCase):
    """N14: a killed run can leave `.bridge-tmp-*` behind in a leaf.

    Nothing else would ever remove it, and it sits in the folder the desktop app
    reads, so apply sweeps the leaves it is about to write to. Safe only because
    apply runs under the lock: a dry run and `status` sweep nothing.
    """

    def stale(self, leaf=None):
        path = os.path.join(leaf or self.b,
                            bridge.TEMP_PREFIX + "999-local_%s.json" % tl.uid(9))
        with open(path, "wb") as handle:
            handle.write(b"half a record")
        return path

    def test_a_stale_temp_file_is_swept_at_the_start_of_an_execute_run(self):
        stale = self.stale()
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            self.sync(execute=True)

        self.assertFalse(os.path.exists(stale))
        self.assertIn(stale, "\n".join(logs.output))

    def test_a_converged_tree_is_swept_too(self):
        """P2-3: the sweep used to sit inside apply, which a converged tree
        never reaches — so the leftover stayed in the folder the app reads for
        ever, and only a tree that happened to be out of sync got cleaned."""
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        tl.write_record(self.b, sid, mtime_ns=5_000_000_000)
        stale = self.stale()
        self.assertTrue(self.sync().plan.is_empty())

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            result = self.sync(execute=True)

        self.assertTrue(result.plan.is_empty())
        self.assertFalse(os.path.exists(stale))
        self.assertIn(stale, "\n".join(logs.output))

    def test_sweeping_a_converged_tree_still_takes_no_backup(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        tl.write_record(self.b, sid, mtime_ns=5_000_000_000)
        self.stale()

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync(execute=True)

        self.assertIsNone(result.backup)
        self.assertFalse(os.path.exists(
            os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)))

    def test_the_sweep_happens_before_the_backup_is_taken(self):
        """Otherwise every backup carries a half-written record nobody wants."""
        stale = self.stale()
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync(execute=True)

        saved = os.path.join(result.backup, bridge.SESSIONS_DIRNAME,
                             os.path.relpath(stale, self.root))
        self.assertFalse(os.path.exists(saved))

    def test_every_leaf_is_swept_not_only_the_ones_being_written_to(self):
        first = self.stale(leaf=self.a)
        second = self.stale(leaf=self.b)
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        tl.write_record(self.b, sid, mtime_ns=5_000_000_000)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            self.sync(execute=True)

        self.assertFalse(os.path.exists(first))
        self.assertFalse(os.path.exists(second))

    def test_a_dry_run_sweeps_nothing(self):
        stale = self.stale()
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        self.sync(execute=False)

        self.assertTrue(os.path.exists(stale))

    def test_only_temp_prefixed_regular_files_are_swept(self):
        keep = tl.write_other(self.b, ".some-other-dotfile", b"keep me")
        directory = os.path.join(self.b, bridge.TEMP_PREFIX + "a-directory")
        os.makedirs(directory)
        outside = tl.write_other(self.base, "outside.json", b"keep me too")
        link = os.path.join(self.b, bridge.TEMP_PREFIX + "a-link")
        os.symlink(outside, link)
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        self.sync(execute=True)

        self.assertTrue(os.path.exists(keep))
        self.assertTrue(os.path.isdir(directory))
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.exists(outside))

    def test_a_forced_write_failure_leaves_no_temp_file_behind(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        original = os.replace

        def explode(src, dst, *args, **kwargs):
            raise OSError(1, "rename refused")

        os.replace = explode
        self.addCleanup(setattr, os, "replace", original)

        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            result = self.sync(execute=True)

        self.assertEqual(result.applied["errors"], 1)
        self.assertEqual(result.applied["copies_written"], 0)
        self.assertEqual([name for name in os.listdir(self.b)
                          if name.startswith(bridge.TEMP_PREFIX)], [])


class UnwritableLeafTest(TreeCase):
    """P2-1: a 0555/0500 leaf kept the plan non-empty FOR EVER.

    Every run then took a full backup (ten of those and the ring holds nothing
    but this one broken state) and churned a temp file in the leaf, which
    re-fires WatchPaths. The leaf is now blocked for WRITING at discovery: its
    records still take part as sources, nothing is written into it, and the plan
    converges.
    """

    def block(self, leaf=None, mode=0o500):
        leaf = leaf or self.b
        os.chmod(leaf, mode)
        self.addCleanup(os.chmod, leaf, 0o700)
        return leaf

    def backups(self):
        root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)
        return sorted(os.listdir(root)) if os.path.isdir(root) else []

    def test_an_unwritable_leaf_is_recorded_with_a_reason(self):
        tl.write_record(self.b, tl.uid(5), mtime_ns=5_000_000_000)
        leaf = self.block()

        state = bridge.read_leaf(leaf)

        self.assertIsNotNone(state.leaf_blocked)
        self.assertIn("writable", state.leaf_blocked)

    def test_its_records_are_still_read_and_still_win(self):
        """An unwritable leaf can hold the NEWEST copy; it stays a source."""
        sid = tl.uid(5)
        tl.write_record(self.b, sid, mtime_ns=9_000_000_000, title="newest")
        tl.write_record(self.a, sid, mtime_ns=1_000_000_000, title="older")
        self.block()

        self.sync(execute=True)

        with open(os.path.join(self.a, "local_%s.json" % sid), "rb") as handle:
            self.assertIn(b"newest", handle.read())

    def test_no_write_is_planned_into_it_and_the_plan_converges(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        leaf = self.block()

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync(execute=True)

        self.assertTrue(result.plan.is_empty())
        self.assertEqual(result.plan.counts()["blocked_leaves"], 1)
        self.assertEqual([path for path, _ in result.plan.blocked_leaves], [leaf])

    def test_no_backup_is_taken_and_no_temp_file_is_churned(self):
        """The two consequences that made this a HIGH: ring shredded, WatchPaths
        re-fired by a temp file appearing in the leaf on every run."""
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.block()

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            self.sync(execute=True)
            self.sync(execute=True)

        self.assertEqual(self.backups(), [])
        self.assertEqual([name for name in os.listdir(self.a)
                          if name.startswith(bridge.TEMP_PREFIX)], [])

    def test_no_tombstone_copy_and_no_delete_reach_it(self):
        sid = tl.uid(5)
        doomed = tl.write_record(self.b, sid, mtime_ns=1_000_000_000)
        tl.write_tombstone(self.a, sid, mtime_ns=5_000_000_000)
        self.block()

        plan = bridge.plan_sync(bridge.gather(self.root))

        self.assertEqual(plan.deletes, ())
        self.assertEqual(plan.tombstone_copies, ())
        self.assertTrue(os.path.exists(doomed))

    def test_the_planner_never_emits_a_write_into_a_non_writable_directory(self):
        """P2-1: `_apply_write`'s "not a real directory" branch is a backstop.

        Keep the guard, but nothing the planner produces may need it.
        """
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        tl.write_tombstone(self.a, tl.uid(6), mtime_ns=6_000_000_000)
        c = tl.make_leaf(self.base, tl.uid(3), tl.uid(33))
        self.block()

        plan = bridge.plan_sync(bridge.gather(self.root))

        self.assertTrue(plan.writes or plan.tombstone_copies)
        for write in plan.writes + plan.tombstone_copies:
            parent = os.path.dirname(write.dest)
            self.assertTrue(os.path.isdir(parent) and not os.path.islink(parent))
            self.assertTrue(os.access(parent, os.W_OK | os.X_OK))
        self.assertIn(c, [os.path.dirname(w.dest) for w in plan.writes])


class UnreadableLeafTest(TreeCase):
    """P2-2: a leaf we cannot even list was reported as records=0, silently.

    Nothing said so, and the planner then treated every id as ABSENT there and
    planned a write into a directory it cannot read — a plan that can never
    converge, i.e. a full backup on every single run.
    """

    def block(self, leaf=None):
        leaf = leaf or self.b
        os.chmod(leaf, 0o000)
        self.addCleanup(os.chmod, leaf, 0o700)
        return leaf

    def test_an_unreadable_leaf_is_recorded_with_a_reason(self):
        leaf = self.block()

        state = bridge.read_leaf(leaf)

        self.assertIsNotNone(state.leaf_blocked)
        self.assertIn("unreadable", state.leaf_blocked)
        self.assertEqual(state.records, {})

    def test_reading_an_unreadable_leaf_warns_and_is_never_silent(self):
        leaf = self.block()

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            bridge.read_leaf(leaf)

        self.assertIn(leaf, "\n".join(logs.output))

    def test_no_write_is_planned_into_an_unreadable_leaf(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        leaf = self.block()

        plan = bridge.plan_sync(bridge.gather(self.root))

        self.assertEqual(plan.writes, ())
        self.assertTrue(plan.is_empty())
        self.assertEqual(plan.blocked_leaves, ((leaf, plan.blocked_leaves[0][1]),))
        self.assertEqual(plan.counts()["blocked_leaves"], 1)

    def test_an_unreadable_leaf_takes_no_backup_and_converges(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.block()

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync(execute=True)

        self.assertTrue(result.plan.is_empty())
        self.assertIsNone(result.backup)
        self.assertFalse(os.path.isdir(
            os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)))

    def test_the_blocked_leaf_is_reported_by_log_plan_findings(self):
        leaf = self.block()
        plan = bridge.plan_sync(bridge.gather(self.root))

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            bridge.log_plan_findings(plan)

        joined = "\n".join(logs.output)
        self.assertIn(leaf, joined)
        self.assertIn("unreadable", joined)

    def test_a_readable_sibling_is_still_planned_for(self):
        """Only the blocked leaf drops out; the rest of the tree converges.

        (The --execute half of this lives in the backup-skip tests: copying the
        tree is what an unreadable leaf breaks next.)
        """
        sid = tl.uid(5)
        c = tl.make_leaf(self.base, tl.uid(3), tl.uid(33))
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        leaf = self.block()

        plan = bridge.plan_sync(bridge.gather(self.root))

        self.assertEqual([w.dest for w in plan.writes],
                         [os.path.join(c, "local_%s.json" % sid)])
        self.assertEqual([path for path, _ in plan.blocked_leaves], [leaf])


class UnreadableCopyTest(TreeCase):
    """P-F7: an unreadable copy must not be overwritten by an older one."""

    def test_an_unreadable_copy_blocks_every_write_of_that_id(self):
        sid = tl.uid(5)
        newest = tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="newest")
        tl.write_record(self.b, sid, mtime_ns=1_000_000_000, title="older")
        with open(newest, "rb") as handle:
            before = handle.read()
        os.chmod(newest, 0o000)
        self.addCleanup(os.chmod, newest, 0o600)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            result = self.sync(execute=True)

        self.assertEqual(result.plan.writes, ())
        self.assertEqual(result.plan.counts()["blocked"], 1)
        os.chmod(newest, 0o600)
        with open(newest, "rb") as handle:
            self.assertEqual(handle.read(), before)


if __name__ == "__main__":
    unittest.main()
