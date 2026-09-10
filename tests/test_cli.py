"""The command line: argparse wiring, lock, backups, status, discovery."""

import contextlib
import io
import os
import shutil
import stat
import tempfile
import unittest

import bridge_testlib as tl
import bridge


class CliCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.user_data = os.path.join(self.base, "userData")
        self.state_dir = os.path.join(self.base, "state")
        # NEVER the real ~/Library/LaunchAgents: status stats the plist now.
        self.agents_dir = os.path.join(self.base, "LaunchAgents")
        self.root = tl.make_root(self.user_data)
        self.a = tl.make_leaf(self.user_data, tl.uid(1), tl.uid(11))
        self.b = tl.make_leaf(self.user_data, tl.uid(2), tl.uid(22))
        self.runner = tl.RecordingRunner()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bridge.main(list(args), runner=self.runner)
        return code, out.getvalue()

    def sync_args(self, *extra):
        return ("sync", "--user-data", self.user_data,
                "--state-dir", self.state_dir) + extra

    def status_args(self, *extra):
        return ("status", "--user-data", self.user_data,
                "--state-dir", self.state_dir,
                "--agents-dir", self.agents_dir) + extra

    def plist_path(self):
        return os.path.join(self.agents_dir, bridge.LABEL + ".plist")

    def write_plist(self):
        os.makedirs(self.agents_dir, exist_ok=True)
        with open(self.plist_path(), "wb") as handle:
            handle.write(bridge.plist_bytes("/synthetic/bridge.py", self.root,
                                            [self.a, self.b]))
        return self.plist_path()

    def backups(self):
        root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)
        return sorted(os.listdir(root)) if os.path.isdir(root) else []


class SyncCommandTest(CliCase):
    def test_default_is_dry_run_and_writes_nothing(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        before = tl.snapshot(self.root)

        code, out = self.run_cli(*self.sync_args())

        self.assertEqual(code, 0)
        self.assertEqual(tl.snapshot(self.root), before)
        self.assertIn("dry-run", out)
        self.assertEqual(self.backups(), [])

    def test_explicit_dry_run_matches_the_default(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        before = tl.snapshot(self.root)

        code, _ = self.run_cli(*self.sync_args("--dry-run"))

        self.assertEqual(code, 0)
        self.assertEqual(tl.snapshot(self.root), before)

    def test_execute_writes_and_reports_counts(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(
            os.path.join(self.b, "local_%s.json" % tl.uid(5))))
        self.assertIn("copies_written=1", out)

    def test_dry_run_and_execute_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                bridge.main(list(self.sync_args("--execute", "--dry-run")),
                            runner=self.runner)
        self.assertNotEqual(caught.exception.code, 0)


class BackupTest(CliCase):
    def test_backup_is_taken_before_the_first_execute(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)

        self.run_cli(*self.sync_args("--execute"))

        names = self.backups()
        self.assertEqual(len(names), 1)
        saved = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME, names[0],
                             bridge.SESSIONS_DIRNAME)
        # the backup holds the PRE-sync tree: leaf B did not have the record yet
        self.assertTrue(os.path.exists(os.path.join(
            saved, tl.uid(1), tl.uid(11), "local_%s.json" % sid)))
        self.assertFalse(os.path.exists(os.path.join(
            saved, tl.uid(2), tl.uid(22), "local_%s.json" % sid)))

    def test_no_backup_in_dry_run(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.run_cli(*self.sync_args("--dry-run"))
        self.assertEqual(self.backups(), [])

    def test_no_backup_when_the_plan_is_empty(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        tl.write_record(self.b, sid, mtime_ns=5_000_000_000)

        self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(self.backups(), [])

    def test_only_the_newest_ten_backups_are_kept(self):
        backups_root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)
        os.makedirs(backups_root)
        for index in range(12):
            os.makedirs(os.path.join(backups_root, "20200101T0000%02d000000Z" % index))
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        self.run_cli(*self.sync_args("--execute"))

        names = self.backups()
        self.assertEqual(len(names), 10)
        self.assertEqual(names[:9], ["20200101T0000%02d000000Z" % i for i in range(3, 12)])
        self.assertTrue(names[-1].endswith("Z"))
        self.assertNotIn("20200101T000000000000Z", names)

    def test_the_backup_preserves_record_bytes_and_exact_mtimes(self):
        """N5: a backup with drifted mtimes would restore a DIFFERENT merge."""
        sid = tl.uid(5)
        source = tl.write_record(self.a, sid, mtime_ns=5_123_456_789)
        other = tl.write_other(self.a, "scheduled-tasks.json",
                               b'{"tasks": []}', 1_222_333_444)

        self.run_cli(*self.sync_args("--execute"))

        saved_root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME,
                                  self.backups()[0], bridge.SESSIONS_DIRNAME)
        for original in (source, other):
            saved = os.path.join(saved_root, os.path.relpath(original, self.root))
            with open(original, "rb") as handle:
                expected = handle.read()
            with open(saved, "rb") as handle:
                self.assertEqual(handle.read(), expected)
            self.assertEqual(os.lstat(saved).st_mtime_ns,
                             os.lstat(original).st_mtime_ns)

    def test_a_symlink_in_the_tree_is_backed_up_as_a_symlink(self):
        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.root, tl.uid(1), tl.uid(12)))
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        self.run_cli(*self.sync_args("--execute"))

        saved = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME,
                             self.backups()[0], bridge.SESSIONS_DIRNAME,
                             tl.uid(1), tl.uid(12))
        self.assertTrue(os.path.islink(saved))


def chmod_if_present(path, mode):
    """Cleanup helper: prune may legitimately have deleted the path already."""
    if os.path.exists(path):
        os.chmod(path, mode)


class BackupFailureTest(CliCase):
    """B3: an unreadable file in the tree must not become a traceback."""

    def make_unreadable(self):
        path = tl.write_other(self.a, "scheduled-tasks.json", b'{"tasks": []}')
        os.chmod(path, 0o000)
        self.addCleanup(chmod_if_present, path, 0o600)
        return path

    def test_a_failed_backup_stops_the_run_before_anything_is_applied(self):
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        self.make_unreadable()

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(
            os.path.join(self.b, "local_%s.json" % sid)))
        self.assertIn("backup", "\n".join(logs.output).lower())
        self.assertEqual(self.backups(), [],
                         "the half-written backup directory must be removed")
        self.assertIn("errors=1", out)

    def test_a_failed_backup_does_not_prune_a_good_backup_away(self):
        """The corpse sorts NEWEST, so pruning before removing it keeps it."""
        backups_root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)
        os.makedirs(backups_root)
        for index in range(10):
            os.makedirs(os.path.join(backups_root,
                                     "20200101T0000%02d000000Z" % index))
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.make_unreadable()

        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            code, _ = self.run_cli(*self.sync_args("--execute"))

        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.backups()), 10)
        self.assertIn("20200101T000009000000Z", self.backups())
        self.assertIn("20200101T000000000000Z", self.backups())


class BackupSkipsUnreadableTest(CliCase):
    """R2-NB2: ONE unreadable record made the backup — and so the whole run —
    fail every single time, for ever.

    That contradicted B6 outright: an unreadable copy is supposed to be
    reported and stepped around, not to wedge the tool. Exactly the paths
    discovery marked unreadable are skipped, each logged, and counted on the
    backup line. Any OTHER copy failure still aborts the run.
    """

    def saved_root(self):
        return os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME,
                            self.backups()[0], bridge.SESSIONS_DIRNAME)

    def saved(self, path):
        return os.path.join(self.saved_root(), os.path.relpath(path, self.root))

    def unreadable_record(self, leaf, session_id):
        path = tl.write_record(leaf, session_id, mtime_ns=5_000_000_000)
        os.chmod(path, 0o000)
        self.addCleanup(chmod_if_present, path, 0o600)
        return path

    def test_an_unreadable_record_is_skipped_and_the_run_completes(self):
        sid = tl.uid(5)
        tl.write_record(self.a, tl.uid(6), mtime_ns=6_000_000_000)
        blocked = self.unreadable_record(self.a, sid)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertEqual(len(self.backups()), 1)
        self.assertFalse(os.path.exists(self.saved(blocked)))
        self.assertIn("skipped=1", out)
        self.assertIn(blocked, "\n".join(logs.output))
        # ... and the rest of the tree was both backed up and converged
        self.assertTrue(os.path.exists(self.saved(
            os.path.join(self.a, "local_%s.json" % tl.uid(6)))))
        self.assertTrue(os.path.exists(
            os.path.join(self.b, "local_%s.json" % tl.uid(6))))

    def test_the_backup_line_names_the_skip_count(self):
        tl.write_record(self.a, tl.uid(6), mtime_ns=6_000_000_000)
        self.unreadable_record(self.a, tl.uid(5))
        self.unreadable_record(self.b, tl.uid(7))

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            _, out = self.run_cli(*self.sync_args("--execute"))

        backup_line = [line for line in out.splitlines()
                       if line.startswith("backup:")]
        self.assertEqual(len(backup_line), 1)
        self.assertIn("skipped=2", backup_line[0])

    def test_a_clean_backup_reports_no_skips(self):
        tl.write_record(self.a, tl.uid(6), mtime_ns=6_000_000_000)

        _, out = self.run_cli(*self.sync_args("--execute"))

        backup_line = [line for line in out.splitlines()
                       if line.startswith("backup:")][0]
        self.assertIn("skipped=0", backup_line)

    def test_an_unreadable_leaf_directory_is_skipped_whole(self):
        sid = tl.uid(5)
        c = tl.make_leaf(self.user_data, tl.uid(3), tl.uid(33))
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        os.chmod(self.b, 0o000)
        self.addCleanup(chmod_if_present, self.b, 0o700)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.saved(self.b)))
        self.assertIn("skipped=1", out)
        self.assertIn(self.b, "\n".join(logs.output))
        # the readable sibling still converges
        self.assertTrue(os.path.isfile(os.path.join(c, "local_%s.json" % sid)))

    def test_an_unreadable_directory_wearing_a_record_name_is_skipped(self):
        """Measured: this was a permanent no-op too.

        The path is `blocked` (it is not a regular file), but it ALSO cannot be
        read, so copytree failed on it — every run, for ever, applying nothing.
        Whether a blocked path can be READ is what decides if the backup can
        step around it, not whether it is a file or a directory.
        """
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        directory = os.path.join(self.b, "local_%s.json" % tl.uid(7))
        os.makedirs(directory)
        tl.write_other(directory, "inner.txt", b"x")
        os.chmod(directory, 0o000)
        self.addCleanup(chmod_if_present, directory, 0o700)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertIn("errors=0", out)
        self.assertIn("skipped=1", out)
        self.assertEqual(len(self.backups()), 1)
        self.assertIn(directory, "\n".join(logs.output))
        self.assertTrue(os.path.exists(
            os.path.join(self.b, "local_%s.json" % sid)))
        self.assertTrue(os.path.isdir(directory))

    def test_a_readable_directory_wearing_a_record_name_is_still_backed_up(self):
        """It is blocked, but it copies fine, so the backup stays complete."""
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        directory = os.path.join(self.b, "local_%s.json" % tl.uid(7))
        os.makedirs(directory)
        tl.write_other(directory, "inner.txt", b"x")

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertIn("skipped=0", out)
        self.assertTrue(os.path.exists(
            os.path.join(self.saved(directory), "inner.txt")))

    def test_a_readable_but_oversize_record_is_still_backed_up(self):
        """It is `blocked` because it is too big to JUDGE, not unreadable —
        the backup can copy it perfectly well, and must."""
        sid = tl.uid(5)
        big = tl.write_record(self.a, sid, mtime_ns=5_000_000_000,
                              title="t" * 4096)
        small = tl.write_record(self.a, tl.uid(6), mtime_ns=6_000_000_000)
        original = bridge.MAX_RECORD_BYTES
        bridge.MAX_RECORD_BYTES = 1024
        self.addCleanup(setattr, bridge, "MAX_RECORD_BYTES", original)
        self.assertGreater(os.lstat(big).st_size, 1024)
        self.assertLess(os.lstat(small).st_size, 1024)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertIn("skipped=0", out)
        with open(self.saved(big), "rb") as handle:
            with open(big, "rb") as source:
                self.assertEqual(handle.read(), source.read())

    def test_any_other_unreadable_file_still_aborts_the_run(self):
        """A file the bridge never reads is not a path it may skip: it has no
        idea what it is, so the backup is not complete without it."""
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        victim = tl.write_other(self.a, "scheduled-tasks.json", b'{"tasks": []}')
        os.chmod(victim, 0o000)
        self.addCleanup(chmod_if_present, victim, 0o600)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertNotEqual(code, 0)
        self.assertEqual(self.backups(), [])
        self.assertIn("backup", "\n".join(logs.output).lower())
        self.assertFalse(os.path.exists(
            os.path.join(self.b, "local_%s.json" % sid)))

    def test_take_backup_reports_the_paths_it_skipped(self):
        blocked = self.unreadable_record(self.a, tl.uid(5))

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            backup = bridge.take_backup(self.root, self.state_dir,
                                        skip_paths=[blocked])

        self.assertEqual(backup.skipped, 1)
        self.assertTrue(os.path.isdir(backup.path))
        self.assertFalse(os.path.exists(self.saved(blocked)))


class PruneRobustnessTest(CliCase):
    def test_a_read_only_directory_inside_a_backup_cannot_strand_it(self):
        """B3: `ignore_errors` used to leave zombies, so the ring grew forever."""
        backups_root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)
        os.makedirs(backups_root)
        for index in range(12):
            os.makedirs(os.path.join(backups_root,
                                     "20200101T0000%02d000000Z" % index))
        locked = os.path.join(backups_root, "20200101T000000000000Z", "locked")
        os.makedirs(locked)
        tl.write_other(locked, "record.json", b"{}")
        os.chmod(locked, 0o555)
        self.addCleanup(chmod_if_present, locked, 0o700)
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        code, _ = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        names = self.backups()
        self.assertEqual(len(names), 10, "exactly ten kept, no zombies")
        self.assertNotIn("20200101T000000000000Z", names)
        self.assertNotIn("20200101T000001000000Z", names)


class PruneSymlinkTest(CliCase):
    """R2-NB1: a symlink in the backups directory reached OUTSIDE the tree.

    `prune_backups` counted it as a backup (os.path.isdir follows links), and
    `_make_writable` then chmod'd every directory and file it pointed at — 0700
    and 0600, on paths this tool has no business touching — before rmtree.
    """

    def outside_tree(self):
        outside = os.path.join(self.base, "outside")
        os.makedirs(os.path.join(outside, "inner"))
        kept = tl.write_other(os.path.join(outside, "inner"), "precious.txt",
                              b"do not touch")
        os.chmod(kept, 0o400)
        os.chmod(os.path.join(outside, "inner"), 0o500)
        self.addCleanup(chmod_if_present, os.path.join(outside, "inner"), 0o700)
        self.addCleanup(chmod_if_present, kept, 0o600)
        return outside, kept

    def modes(self, outside, kept):
        return (stat.S_IMODE(os.lstat(kept).st_mode),
                stat.S_IMODE(os.lstat(os.path.join(outside, "inner")).st_mode))

    def backups_root(self):
        root = os.path.join(self.state_dir, bridge.BACKUPS_DIRNAME)
        os.makedirs(root, exist_ok=True)
        return root

    def test_prune_skips_a_symlink_named_like_a_backup_and_says_so(self):
        outside, kept = self.outside_tree()
        root = self.backups_root()
        for index in range(12):
            os.makedirs(os.path.join(root, "20200101T0000%02d000000Z" % index))
        link = os.path.join(root, "20190101T000000000000Z")
        os.symlink(outside, link)
        before = self.modes(outside, kept)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            bridge.prune_backups(root)

        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isdir(os.path.join(outside, "inner")))
        self.assertEqual(self.modes(outside, kept), before)
        self.assertIn(link, "\n".join(logs.output))

    def test_the_symlink_does_not_occupy_a_slot_in_the_ring(self):
        """It is not a backup, so it must not push a real one out either, and
        the ring must not be one short for ever because of it."""
        outside, kept = self.outside_tree()
        root = self.backups_root()
        for index in range(10):
            os.makedirs(os.path.join(root, "20200101T0000%02d000000Z" % index))
        os.symlink(outside, os.path.join(root, "20190101T000000000000Z"))
        before = self.modes(outside, kept)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            removed = bridge.prune_backups(root)

        self.assertEqual(removed, [])
        self.assertEqual(self.modes(outside, kept), before)
        self.assertEqual(sorted(name for name in os.listdir(root)
                                if not os.path.islink(os.path.join(root, name))),
                         ["20200101T0000%02d000000Z" % i for i in range(10)])
        self.assertIn("symlink", "\n".join(logs.output))

    def test_make_writable_stops_at_a_symlink(self):
        outside, kept = self.outside_tree()
        link = os.path.join(self.base, "link-to-outside")
        os.symlink(outside, link)
        before = self.modes(outside, kept)

        bridge._make_writable(link)

        self.assertEqual(self.modes(outside, kept), before)

    def test_force_rmtree_refuses_a_symlink_outright(self):
        outside, kept = self.outside_tree()
        link = os.path.join(self.base, "link-to-outside")
        os.symlink(outside, link)

        before = self.modes(outside, kept)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            self.assertFalse(bridge._force_rmtree(link))

        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.exists(kept))
        self.assertEqual(self.modes(outside, kept), before)
        self.assertIn(link, "\n".join(logs.output))
        self.assertIn("symlink", "\n".join(logs.output))


class BackupNamingTest(CliCase):
    def test_backup_names_are_utc_stamped_and_sort_chronologically(self):
        import datetime
        earlier = datetime.datetime(2026, 1, 2, 3, 4, 5, 60000,
                                    tzinfo=datetime.timezone.utc)
        later = earlier + datetime.timedelta(seconds=1)

        first = bridge.take_backup(self.root, self.state_dir, now=earlier)
        second = bridge.take_backup(self.root, self.state_dir, now=later)

        self.assertEqual(os.path.basename(first.path), "20260102T030405060000Z")
        self.assertEqual(first.skipped, 0)
        self.assertLess(os.path.basename(first.path),
                        os.path.basename(second.path))

    def test_taking_a_backup_does_not_emit_deprecation_warnings(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            bridge.take_backup(self.root, self.state_dir)


class LockTest(CliCase):
    def lock_path(self):
        return os.path.join(self.state_dir, bridge.LOCK_FILENAME)

    def hold_lock(self, body):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.lock_path(), "w") as handle:
            handle.write(body)

    def kill_raises(self, error):
        original = os.kill

        def fake(pid, sig):
            if sig == 0:
                raise error
            return original(pid, sig)

        os.kill = fake
        self.addCleanup(setattr, os, "kill", original)

    def test_a_lock_held_by_a_live_process_stops_the_run(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        # our own pid: guaranteed alive, unlike an arbitrary number
        self.hold_lock(str(os.getpid()))
        before = tl.snapshot(self.root)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, _ = self.run_cli(*self.sync_args("--execute"))

        self.assertNotEqual(code, 0)
        self.assertEqual(tl.snapshot(self.root), before)
        joined = "\n".join(logs.output)
        self.assertIn("already running", joined)
        self.assertIn(self.lock_path(), joined)

    def test_the_lock_is_released_after_a_normal_run(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        code, _ = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_the_lock_is_released_even_when_the_run_fails(self):
        def explode(*_args, **_kwargs):
            raise RuntimeError("boom")

        original = bridge.sync_once
        bridge.sync_once = explode
        self.addCleanup(setattr, bridge, "sync_once", original)

        with self.assertRaises(RuntimeError):
            self.run_cli(*self.sync_args("--execute"))

        self.assertFalse(os.path.exists(self.lock_path()))

    def test_the_lock_is_held_while_the_sync_itself_runs(self):
        """Pins the ORDERING: the lock must cover the backup+write window."""
        observed = {}
        original = bridge.sync_once

        def spy(root, execute=False, state_dir=None):
            observed["locked"] = os.path.exists(self.lock_path())
            return original(root, execute=execute, state_dir=state_dir)

        bridge.sync_once = spy
        self.addCleanup(setattr, bridge, "sync_once", original)
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        code, _ = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertTrue(observed.get("locked"),
                        "sync ran without the lock file in place")

    def test_a_dry_run_needs_no_lock_at_all(self):
        """B2: a dry run writes nothing, so a held lock must not wedge it.

        Refusing to even LOOK while a lock exists is what made a stale lock
        unrecoverable: the one command that could diagnose it was blocked too.
        """
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.hold_lock(str(os.getpid()))
        before = tl.snapshot(self.root)

        code, out = self.run_cli(*self.sync_args("--dry-run"))

        self.assertEqual(code, 0)
        self.assertIn("copies_written=1", out)
        self.assertEqual(tl.snapshot(self.root), before)

    def test_a_dry_run_creates_no_state_directory(self):
        """N13/P-F9b: nothing is written, so nothing is created either."""
        shutil.rmtree(self.state_dir, ignore_errors=True)
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        code, _ = self.run_cli(*self.sync_args("--dry-run"))

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_a_lock_left_by_a_dead_process_is_reclaimed(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.hold_lock("4242")
        self.kill_raises(ProcessLookupError(3, "No such process"))

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertIn("copies_written=1", out)
        self.assertFalse(os.path.exists(self.lock_path()))
        joined = "\n".join(logs.output)
        self.assertIn(self.lock_path(), joined)
        self.assertIn("4242", joined)

    def test_a_lock_file_with_no_usable_pid_is_reclaimed(self):
        for body in ("", "   ", "not-a-pid", "0", "-1"):
            with self.subTest(body=body):
                shutil.rmtree(self.state_dir, ignore_errors=True)
                self.hold_lock(body)

                with self.assertLogs(bridge.LOGGER, level="WARNING"):
                    code, _ = self.run_cli(*self.sync_args("--execute"))

                self.assertEqual(code, 0)
                self.assertFalse(os.path.exists(self.lock_path()))

    def test_a_lock_held_by_another_users_process_is_never_stolen(self):
        """PermissionError from os.kill means the process EXISTS."""
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        self.hold_lock("4242")
        self.kill_raises(PermissionError(1, "Operation not permitted"))
        before = tl.snapshot(self.root)

        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            code, _ = self.run_cli(*self.sync_args("--execute"))

        self.assertNotEqual(code, 0)
        self.assertEqual(tl.snapshot(self.root), before)
        self.assertTrue(os.path.exists(self.lock_path()))


class UnusableStateDirTest(CliCase):
    def test_an_unwritable_state_directory_is_a_clean_error(self):
        """N17: a clean message and a non-zero exit, never a traceback."""
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        cage = os.path.join(self.base, "cage")
        os.makedirs(cage)
        os.chmod(cage, 0o500)
        self.addCleanup(os.chmod, cage, 0o700)
        state_dir = os.path.join(cage, "state")
        before = tl.snapshot(self.root)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, _ = self.run_cli("sync", "--user-data", self.user_data,
                                   "--state-dir", state_dir, "--execute")

        self.assertNotEqual(code, 0)
        self.assertIn(state_dir, "\n".join(logs.output))
        self.assertEqual(tl.snapshot(self.root), before)

    def test_an_existing_state_directory_we_cannot_write_to_is_a_clean_error(self):
        """The other raise site: makedirs succeeds, creating the lock does not."""
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        os.makedirs(self.state_dir)
        os.chmod(self.state_dir, 0o500)
        self.addCleanup(os.chmod, self.state_dir, 0o700)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, _ = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 2)
        self.assertIn(self.state_dir, "\n".join(logs.output))
        self.assertFalse(os.path.exists(
            os.path.join(self.b, "local_%s.json" % tl.uid(5))))


class RefusedWriteReportingTest(CliCase):
    def test_a_destination_refused_for_safety_is_visible_in_the_output(self):
        """B4: the refusal happens at DISCOVERY, so it shows up as `blocked`.

        The planner emits no write for a symlinked destination at all, which is
        what makes the plan converge; there is therefore nothing left to refuse
        at apply time and nothing to count in `skipped`.
        """
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000)
        target = tl.write_other(self.base, "target.json", content=b"untouched")
        os.symlink(target, os.path.join(self.b, "local_%s.json" % sid))

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertIn("blocked=1", out)
        self.assertIn("copies_written=0", out.splitlines()[-1])
        self.assertEqual(self.backups(), [])
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"untouched")

    def test_an_apply_time_skip_is_visible_in_the_applied_line(self):
        """`skipped` still has to reach the output — via the B1 re-check now."""
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="winner")
        tl.write_record(self.b, sid, mtime_ns=1_000_000_000, title="stale")
        original = bridge.apply_plan

        def racing_apply(plan):
            # the app writes a newer copy after the plan, before the writes
            tl.write_record(self.b, sid, mtime_ns=9_500_000_000, title="fresh")
            return original(plan)

        bridge.apply_plan = racing_apply
        self.addCleanup(setattr, bridge, "apply_plan", original)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertIn("skipped=1", out)
        self.assertIn("copies_written=1", out)      # the plan said one
        self.assertIn("copies_written=0", out)      # the apply wrote none

    def test_a_skipped_only_run_stays_at_exit_zero_and_says_why(self):
        """R2-NB12: nothing failed — the app wrote a newer copy and we kept it,
        and the next trigger converges — so exit 0. But it may not be quiet:
        the count goes to stdout AND the reason to the log."""
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=9_000_000_000, title="winner")
        dest = tl.write_record(self.b, sid, mtime_ns=1_000_000_000, title="stale")
        original = bridge.apply_plan

        def racing_apply(plan):
            tl.write_record(self.b, sid, mtime_ns=9_500_000_000, title="fresh")
            return original(plan)

        bridge.apply_plan = racing_apply
        self.addCleanup(setattr, bridge, "apply_plan", original)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertIn("skipped=1", out)
        self.assertIn("errors=0", out)
        joined = "\n".join(logs.output)
        self.assertIn(dest, joined)
        self.assertIn("changed since the plan was made", joined)
        self.assertIn("the copy on disk is kept", joined)
        self.assertTrue(any(record.levelno == bridge.logging.WARNING
                            and "changed since the plan" in record.getMessage()
                            for record in logs.records))

    def test_a_write_error_makes_the_exit_code_non_zero(self):
        """N6: a failed write must not look like a clean run.

        The failure is injected at the rename: an unwritable leaf is blocked at
        discovery now (P2-1), so it produces no write to fail.
        """
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        original = os.replace

        def explode(src, dst, *args, **kwargs):
            raise OSError(1, "rename refused")

        os.replace = explode
        self.addCleanup(setattr, os, "replace", original)

        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 1)
        self.assertIn("errors=1", out)

    def test_an_unwritable_leaf_is_a_blocked_leaf_not_an_error(self):
        """P2-1: it used to be errors=1 on every run, for ever, with a full
        backup each time. Now it is reported, counted, and the run is clean."""
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        os.chmod(self.b, 0o500)
        self.addCleanup(os.chmod, self.b, 0o700)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertEqual(code, 0)
        self.assertIn("blocked_leaves=1", out)
        self.assertIn("copies_written=0", out)
        self.assertIn(self.b, "\n".join(logs.output))
        self.assertEqual(self.backups(), [])

    def test_a_clean_execute_reports_what_was_applied(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        _, out = self.run_cli(*self.sync_args("--execute"))

        self.assertIn("applied:", out)
        self.assertIn("skipped=0", out)
        self.assertIn("errors=0", out)


class SessionsRootTest(CliCase):
    """B5: a missing or mistyped root reported "in sync" and exited 0."""

    def test_sync_refuses_a_missing_sessions_root(self):
        shutil.rmtree(self.root)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, out = self.run_cli(*self.sync_args("--execute"))

        self.assertNotEqual(code, 0)
        self.assertIn(self.root, "\n".join(logs.output))
        self.assertNotIn("in sync", out)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_status_refuses_a_missing_sessions_root(self):
        shutil.rmtree(self.root)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, out = self.run_cli(*self.status_args())

        self.assertNotEqual(code, 0)
        self.assertIn(self.root, "\n".join(logs.output))
        self.assertNotIn("in sync", out)

    def test_a_mistyped_user_data_directory_is_named_in_the_error(self):
        typo = os.path.join(self.base, "userDta")

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, _ = self.run_cli("sync", "--user-data", typo,
                                   "--state-dir", self.state_dir)

        self.assertNotEqual(code, 0)
        self.assertIn(typo, "\n".join(logs.output))

    def test_a_sessions_root_that_is_a_file_is_refused(self):
        shutil.rmtree(self.root)
        tl.write_other(self.user_data, bridge.SESSIONS_DIRNAME, b"not a directory")

        for args in (self.sync_args("--execute"), self.status_args()):
            with self.subTest(args=args[0]):
                with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
                    code, _ = self.run_cli(*args)
                self.assertNotEqual(code, 0)
                self.assertIn(self.root, "\n".join(logs.output))


class PathProblemMessageTest(CliCase):
    """Gap 17: the two `exists` arms say DIFFERENT things, and must keep to it.

    "does not exist" sends you to check --user-data; "is not a directory" says
    the path is there but is the wrong kind of thing. Swapping them, or
    collapsing them into one, makes the tool lie about what it found.
    """

    def test_a_missing_root_says_it_does_not_exist_and_names_the_flag(self):
        shutil.rmtree(self.root)

        problem = bridge.path_problem(self.root, self.state_dir)

        self.assertIn("does not exist", problem)
        self.assertNotIn("is not a directory", problem)
        self.assertIn(self.root, problem)
        self.assertIn("--user-data", problem)
        self.assertIn(bridge.SESSIONS_DIRNAME, problem)

    def test_a_root_that_is_a_file_says_it_is_not_a_directory(self):
        shutil.rmtree(self.root)
        tl.write_other(self.user_data, bridge.SESSIONS_DIRNAME, b"not a dir")

        problem = bridge.path_problem(self.root, self.state_dir)

        self.assertIn("is not a directory", problem)
        self.assertNotIn("does not exist", problem)
        self.assertIn(self.root, problem)

    def test_usable_paths_are_no_problem_at_all(self):
        self.assertIsNone(bridge.path_problem(self.root, self.state_dir))


class StateDirInsideRootTest(CliCase):
    """N12: backups inside the tree being backed up grow without bound."""

    def inside(self):
        return os.path.join(self.root, tl.uid(1), "state")

    def test_sync_refuses_a_state_dir_inside_the_sessions_root(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, _ = self.run_cli("sync", "--user-data", self.user_data,
                                   "--state-dir", self.inside(), "--execute")

        self.assertNotEqual(code, 0)
        self.assertIn(self.inside(), "\n".join(logs.output))
        self.assertFalse(os.path.exists(self.inside()))

    def test_the_sessions_root_itself_is_refused_as_a_state_dir(self):
        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            code, _ = self.run_cli("sync", "--user-data", self.user_data,
                                   "--state-dir", self.root, "--execute")
        self.assertNotEqual(code, 0)

    def test_install_agent_refuses_to_bake_in_such_a_state_dir(self):
        agents_dir = os.path.join(self.base, "LaunchAgents")

        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            code, _ = self.run_cli("install-agent", "--user-data", self.user_data,
                                   "--state-dir", self.inside(),
                                   "--agents-dir", agents_dir)

        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(
            os.path.join(agents_dir, bridge.LABEL + ".plist")))
        self.assertEqual(self.runner.calls, [])

    def test_a_symlinked_state_dir_inside_the_root_is_still_refused(self):
        """R2-NB4: abspath does not resolve links, so a --state-dir reached
        through one walked straight past the refusal — and every backup then
        copied the previous backups, without bound."""
        link = os.path.join(self.base, "looks-outside")
        os.symlink(os.path.join(self.root, tl.uid(1)), link)
        state_dir = os.path.join(link, "state")
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code, _ = self.run_cli("sync", "--user-data", self.user_data,
                                   "--state-dir", state_dir, "--execute")

        self.assertEqual(code, 2)
        self.assertIn(state_dir, "\n".join(logs.output))
        self.assertFalse(os.path.exists(state_dir))

    def test_a_symlinked_sessions_root_does_not_hide_the_state_dir_either(self):
        """The other side of the comparison: the ROOT reached through a link."""
        link = os.path.join(self.base, "userData-link")
        os.symlink(self.user_data, link)

        problem = bridge.path_problem(bridge.sessions_root(link), self.inside())

        self.assertIsNotNone(problem)
        self.assertIn(self.inside(), problem)

    def test_realpath_is_only_used_for_the_comparison_never_to_write(self):
        """A symlinked state directory OUTSIDE the tree is perfectly fine, and
        the backup must land under the path we were given, not its target."""
        outside = os.path.join(self.base, "elsewhere")
        os.makedirs(outside)
        link = os.path.join(self.base, "state-link")
        os.symlink(outside, link)
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        code, out = self.run_cli("sync", "--user-data", self.user_data,
                                 "--state-dir", link, "--execute")

        self.assertEqual(code, 0)
        self.assertIn(link, out)
        self.assertTrue(os.path.isdir(os.path.join(link, bridge.BACKUPS_DIRNAME)))

    def test_a_state_dir_beside_the_root_is_fine(self):
        beside = os.path.join(self.user_data, "state")
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)

        code, _ = self.run_cli("sync", "--user-data", self.user_data,
                               "--state-dir", beside, "--execute")

        self.assertEqual(code, 0)


class StatusCommandTest(CliCase):
    def test_status_lists_every_leaf_with_its_counts(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        tl.write_tombstone(self.a, tl.uid(6), mtime_ns=6_000_000_000)

        code, out = self.run_cli(*self.status_args())

        self.assertEqual(code, 0)
        self.assertIn(self.a, out)
        self.assertIn(self.b, out)
        self.assertIn("records=1", out)
        self.assertIn("tombstones=1", out)

    def test_status_reports_drift_and_writes_nothing(self):
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        before = tl.snapshot(self.root)

        code, out = self.run_cli(*self.status_args())

        self.assertEqual(code, 0)
        self.assertIn("copies_written=1", out)
        self.assertEqual(tl.snapshot(self.root), before)
        self.assertEqual(self.backups(), [])

    def test_status_says_in_sync_when_there_is_no_drift(self):
        code, out = self.run_cli(*self.status_args())
        self.assertEqual(code, 0)
        self.assertIn("in sync", out)

    def test_the_counts_line_is_printed_even_with_no_drift(self):
        """P2-4: the counts were only shown when something needed doing, so a
        converged tree hid every finding behind "every leaf is in sync"."""
        tl.write_record(self.a, tl.uid(5), mtime_ns=5_000_000_000)
        tl.write_record(self.b, tl.uid(5), mtime_ns=5_000_000_000)

        code, out = self.run_cli(*self.status_args())

        self.assertEqual(code, 0)
        self.assertIn("in sync", out)
        self.assertIn("records_seen=1", out)
        self.assertIn("blocked=0", out)
        self.assertIn("id_mismatches=0", out)

    def test_a_blocked_record_under_a_tombstone_is_visible_with_no_drift(self):
        """R2-NB11: nothing can be done about it, so there is no drift — but it
        must not read as a clean, fully-converged tree either."""
        sid = tl.uid(5)
        blocked = os.path.join(self.a, "local_%s.json" % sid)
        os.makedirs(blocked)
        tl.write_tombstone(self.a, sid, mtime_ns=5_000_000_000)
        tl.write_tombstone(self.b, sid, mtime_ns=5_000_000_000)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            code, out = self.run_cli(*self.status_args())

        self.assertEqual(code, 0)
        self.assertIn("in sync", out)
        self.assertIn("blocked=1", out)
        self.assertIn(blocked, "\n".join(logs.output))

    def test_a_blocked_leaf_is_visible_in_the_status_counts(self):
        os.chmod(self.b, 0o500)
        self.addCleanup(os.chmod, self.b, 0o700)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            code, out = self.run_cli(*self.status_args())

        self.assertEqual(code, 0)
        self.assertIn("blocked_leaves=1", out)

    def test_status_reports_whether_the_agent_is_loaded(self):
        self.write_plist()          # ... otherwise this is the P2-5 broken state
        self.runner.results["launchctl print"] = bridge.RunResult(returncode=0)
        _, out = self.run_cli(*self.status_args())
        self.assertIn("agent: loaded", out)
        self.assertTrue(any("print" in call[1] for call in self.runner.calls))

    def test_status_says_not_loaded_when_launchctl_fails(self):
        self.runner.results["launchctl print"] = bridge.RunResult(returncode=113)
        _, out = self.run_cli(*self.status_args())
        self.assertIn("not loaded", out)


class StatusAgentStateTest(CliCase):
    """P-F8: "loaded" was launchctl's word alone; the plist was never checked."""

    def print_returns(self, returncode):
        self.runner.results["launchctl print"] = bridge.RunResult(
            returncode=returncode)

    def test_a_plist_that_exists_and_is_loaded_is_reported_as_installed(self):
        self.write_plist()
        self.print_returns(0)

        _, out = self.run_cli(*self.status_args())

        self.assertIn("agent: loaded", out)
        self.assertIn(self.plist_path(), out)

    def test_a_plist_that_exists_but_is_not_loaded_says_so_and_names_it(self):
        self.write_plist()
        self.print_returns(113)

        _, out = self.run_cli(*self.status_args())

        self.assertIn("not loaded", out)
        self.assertIn(self.plist_path(), out)
        self.assertIn("install-agent", out)

    def test_no_plist_and_nothing_loaded_reads_as_not_installed(self):
        self.print_returns(113)

        _, out = self.run_cli(*self.status_args())

        self.assertIn("not loaded", out)
        self.assertIn("no plist", out)

    def test_a_job_loaded_with_no_plist_on_disk_is_called_out(self):
        """The state a failed re-install used to leave behind, inverted.

        P2-5: this used to print `agent: loaded - no plist at ...`, i.e. a
        POSITIVE verdict on a broken install — the job will not come back after
        a logout, and there is nothing on disk to bring it back.
        """
        self.print_returns(0)

        _, out = self.run_cli(*self.status_args())

        self.assertIn("agent: not loaded properly", out)
        self.assertNotIn("agent: loaded", out)
        self.assertIn("launchd has the job", out)
        self.assertIn("there is no plist at %s" % self.plist_path(), out)
        self.assertIn("install-agent", out)

    def test_launchctl_being_unavailable_is_unknown_not_a_verdict(self):
        def missing(argv, check=False):
            raise OSError(2, "No such file or directory: 'launchctl'")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bridge.main(list(self.status_args()), runner=missing)

        self.assertEqual(code, 0)
        self.assertIn("unknown", out.getvalue())

    def test_status_logs_the_same_findings_a_sync_would(self):
        """A read-only look must surface every warning, not stay quiet."""
        sid = tl.uid(5)
        tl.write_record(self.a, sid, mtime_ns=5_000_000_000, raw=b"{ broken")
        os.makedirs(os.path.join(self.b, "local_%s.json" % tl.uid(6)))
        tl.write_other(self.b, "local_not-a-uuid.json", b"{}")
        mismatched = tl.record_bytes(tl.uid(9))
        tl.write_record(self.a, tl.uid(7), mtime_ns=5_000_000_000, raw=mismatched)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            code, out = self.run_cli(*self.status_args())

        self.assertEqual(code, 0)
        joined = "\n".join(logs.output)
        self.assertIn("local_%s.json" % sid, joined)                # unparsable
        self.assertIn("local_%s.json" % tl.uid(6), joined)           # blocked
        self.assertIn("local_%s.json" % tl.uid(7), joined)           # id mismatch
        self.assertIn("blocked=1", out)
        self.assertIn("ignored=1", out)
        self.assertIn("id_mismatches=1", out)


class UserDataDiscoveryTest(unittest.TestCase):
    def test_platform_defaults(self):
        home = "/synthetic/home"
        self.assertEqual(
            bridge.default_user_data(platform="darwin", home=home,
                                    environ={}),
            "/synthetic/home/Library/Application Support/Claude")
        self.assertEqual(
            bridge.default_user_data(platform="linux", home=home, environ={}),
            "/synthetic/home/.config/Claude")

    def test_windows_uses_appdata_when_present(self):
        got = bridge.default_user_data(
            platform="win32", home="C:\\Users\\someone",
            environ={"APPDATA": "C:\\Users\\someone\\AppData\\Roaming"})
        self.assertTrue(got.endswith("Claude"))
        self.assertIn("AppData", got)

    def test_sessions_root_hangs_off_user_data(self):
        self.assertEqual(
            bridge.sessions_root("/synthetic/userData"),
            os.path.join("/synthetic/userData", bridge.SESSIONS_DIRNAME))


class VerbosityTest(CliCase):
    def test_verbose_flag_selects_debug_level(self):
        self.assertEqual(bridge.log_level(0), bridge.logging.INFO)
        self.assertEqual(bridge.log_level(1), bridge.logging.DEBUG)


if __name__ == "__main__":
    unittest.main()
