"""The exclusive lock: mutual exclusion, stale reclaim, and teardown (R2-B1).

The mutual-exclusion test uses a REAL second process taking the lock through
the real code path. It is synchronised by a file-based barrier — the child
signals "I hold it" by creating a file and waits for a release file — so
nothing here depends on a sleep landing in the right order.

Everything runs against a temp --state-dir. No real state directory, no real
launchd, no `launchctl`.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import bridge_testlib as tl
import bridge

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The child stalls INSIDE the lock, at the moment right after the claim, and
# only lets go when the parent says so. sys.path is fixed up here because the
# child is started with a bare script path.
CHILD = '''\
import os
import sys
import time

sys.path.insert(0, %r)
import bridge

state_dir, barrier, release = sys.argv[1], sys.argv[2], sys.argv[3]


def stall(path):
    with open(barrier, "w") as handle:
        handle.write(str(os.getpid()))
    deadline = time.time() + 60
    while not os.path.exists(release):
        if time.time() > deadline:
            raise SystemExit("child timed out waiting for the release file")
        time.sleep(0.01)


bridge.AFTER_LOCK_CLAIM = stall
with bridge.file_lock(state_dir) as held:
    if not os.path.exists(held):
        raise SystemExit("the lock file the child claimed is not there")
raise SystemExit(0)
''' % (REPO_ROOT,)


class LockCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state_dir = os.path.join(self.base, "state")
        # The pause seam belongs to the CHILD process only. Armed in this one it
        # would hang every later file_lock — a stuck suite, not a red test — so
        # it is checked on the way in and restored on the way out regardless.
        self.assertIsNone(bridge.AFTER_LOCK_CLAIM,
                          "AFTER_LOCK_CLAIM was left armed by an earlier test")
        self.addCleanup(setattr, bridge, "AFTER_LOCK_CLAIM", None)

    def lock_path(self):
        return os.path.join(self.state_dir, bridge.LOCK_FILENAME)

    def hold_lock(self, body):
        """Plant a lock file that this run did not claim."""
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.lock_path(), "w") as handle:
            handle.write(body)
        return os.lstat(self.lock_path()).st_ino

    def kill_raises(self, error):
        original = os.kill

        def fake(pid, sig):
            if sig == 0:
                raise error
            return original(pid, sig)

        os.kill = fake
        self.addCleanup(setattr, os, "kill", original)


class TwoProcessRaceTest(LockCase):
    """The B1 defect itself: the lock file used to be created EMPTY.

    A concurrent run read the empty body, decided the lock was stale, unlinked
    the LIVE holder's lock and took it — both runs then held the lock and both
    wrote to the same leaves. The pid is written before the claim now, and the
    claim is the hard link itself.
    """

    def start_child(self):
        script = os.path.join(self.base, "child_holds_the_lock.py")
        with open(script, "w") as handle:
            handle.write(CHILD)
        self.barrier = os.path.join(self.base, "child-has-the-lock")
        self.release = os.path.join(self.base, "child-may-let-go")
        environ = dict(os.environ)
        environ["PYTHONDONTWRITEBYTECODE"] = "1"
        child = subprocess.Popen(
            [sys.executable, "-B", script, self.state_dir, self.barrier,
             self.release],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environ)
        self.addCleanup(self.stop_child, child)
        return child

    def stop_child(self, child):
        if child.poll() is None:
            child.kill()
        child.wait()

    def wait_for(self, path, child, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path):
                return
            if child.poll() is not None:
                self.fail("the child exited (%s) before reaching the barrier: %s"
                          % (child.returncode, child.communicate()[1]))
            time.sleep(0.01)
        self.fail("the child never reached the barrier %s" % path)

    def let_go(self, child):
        with open(self.release, "w") as handle:
            handle.write("go")
        try:
            out, err = child.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("the child did not exit after being released")
        self.assertEqual(child.returncode, 0, err.decode("utf-8", "replace"))

    def test_a_second_run_cannot_take_a_lock_a_live_process_holds(self):
        child = self.start_child()
        self.wait_for(self.barrier, child)

        with self.assertRaises(bridge.LockHeld) as caught:
            with bridge.file_lock(self.state_dir):
                self.fail("two runs held the lock at the same time")

        self.assertEqual(caught.exception.path, self.lock_path())
        self.assertEqual(caught.exception.pid, child.pid)
        self.let_go(child)

    def test_the_pid_is_readable_the_moment_the_lock_exists(self):
        """An empty body is what made a live lock look stale."""
        child = self.start_child()
        self.wait_for(self.barrier, child)

        self.assertEqual(bridge._lock_owner_pid(self.lock_path()), child.pid)
        self.assertTrue(bridge._lock_owner_is_gone(self.lock_path()) is False)
        self.let_go(child)

    def test_the_child_leaves_no_lock_and_no_temp_file_behind(self):
        child = self.start_child()
        self.wait_for(self.barrier, child)
        self.let_go(child)

        self.assertFalse(os.path.exists(self.lock_path()))
        self.assertEqual(os.listdir(self.state_dir), [])

    def test_the_lock_is_available_again_once_the_child_is_done(self):
        child = self.start_child()
        self.wait_for(self.barrier, child)
        self.let_go(child)

        with bridge.file_lock(self.state_dir) as path:
            self.assertEqual(bridge._lock_owner_pid(path), os.getpid())


class ClaimTest(LockCase):
    def test_the_lock_holds_this_run_s_pid_while_it_is_held(self):
        with bridge.file_lock(self.state_dir) as path:
            self.assertEqual(bridge._lock_owner_pid(path), os.getpid())
        self.assertFalse(os.path.exists(path))

    def test_the_temp_file_used_to_claim_it_is_cleaned_up(self):
        with bridge.file_lock(self.state_dir):
            self.assertEqual(os.listdir(self.state_dir),
                             [bridge.LOCK_FILENAME])

    def test_a_leftover_claim_temp_file_does_not_wedge_the_tool(self):
        """Pids are reused, so a temp file from a killed run may carry ours."""
        os.makedirs(self.state_dir)
        stale = os.path.join(self.state_dir, "%s.%d.tmp" % (
            bridge.LOCK_FILENAME, os.getpid()))
        with open(stale, "w") as handle:
            handle.write("999999")

        with bridge.file_lock(self.state_dir) as path:
            self.assertEqual(bridge._lock_owner_pid(path), os.getpid())

    def test_the_claimed_inode_is_the_one_this_run_created(self):
        """R2-B1: recording `lstat(lock_path)` AFTER the link is not safe.

        Another run's reclaim can swap the file in that window, and this run
        would then delete THAT run's live lock on the way out.
        """
        original = os.link

        def link_then_swap(src, dst, *args, **kwargs):
            original(src, dst, *args, **kwargs)
            os.unlink(dst)
            self.hold_lock("4242")

        os.link = link_then_swap
        self.addCleanup(setattr, os, "link", original)

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            with bridge.file_lock(self.state_dir):
                pass

        self.assertTrue(os.path.exists(self.lock_path()))
        self.assertEqual(bridge._lock_owner_pid(self.lock_path()), 4242)
        self.assertIn(self.lock_path(), "\n".join(logs.output))


class TeardownTest(LockCase):
    def test_teardown_never_removes_a_lock_this_run_did_not_claim(self):
        """Run A's teardown used to unlink whatever sat at the path — run B's
        live lock included."""
        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            with bridge.file_lock(self.state_dir) as path:
                os.unlink(path)
                foreign = self.hold_lock(str(os.getpid()))

        self.assertTrue(os.path.exists(self.lock_path()))
        self.assertEqual(os.lstat(self.lock_path()).st_ino, foreign)
        self.assertIn(self.lock_path(), "\n".join(logs.output))

    def test_teardown_is_quiet_when_the_lock_has_simply_gone(self):
        with bridge.file_lock(self.state_dir) as path:
            os.unlink(path)
        self.assertFalse(os.path.exists(path))


class StaleReclaimTest(LockCase):
    def test_a_dead_owner_s_lock_is_reclaimed_once_and_loudly(self):
        self.hold_lock("4242")
        self.kill_raises(ProcessLookupError(3, "No such process"))

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            with bridge.file_lock(self.state_dir) as path:
                self.assertEqual(bridge._lock_owner_pid(path), os.getpid())

        joined = "\n".join(logs.output)
        self.assertIn("reclaiming", joined)
        self.assertIn("4242", joined)
        self.assertFalse(os.path.exists(self.lock_path()))

    def test_a_lock_whose_inode_changed_before_the_unlink_is_left_alone(self):
        """Judged stale, then replaced by a LIVE run's lock in the window.

        Unlinking "whatever is at the path" would throw away a lock that was
        never judged at all.
        """
        planted = self.hold_lock("4242")
        original = bridge._lock_owner_is_gone

        def gone_then_swapped(path):
            os.unlink(path)
            self.hold_lock(str(os.getpid()))
            return True

        bridge._lock_owner_is_gone = gone_then_swapped
        self.addCleanup(setattr, bridge, "_lock_owner_is_gone", original)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            with self.assertRaises(bridge.LockHeld) as caught:
                with bridge.file_lock(self.state_dir):
                    self.fail("the swapped-in lock was stolen")

        self.assertEqual(caught.exception.pid, os.getpid())
        self.assertTrue(os.path.exists(self.lock_path()))
        self.assertNotEqual(os.lstat(self.lock_path()).st_ino, planted)

    def test_losing_the_reclaim_race_is_lock_held_not_a_second_attempt(self):
        """R2 gap 14: another run claims it between our unlink and our claim."""
        self.hold_lock("4242")
        self.kill_raises(ProcessLookupError(3, "No such process"))
        original = bridge._unlink_lock_if_inode

        def unlink_then_lose(path, inode):
            removed = original(path, inode)
            self.hold_lock(str(os.getpid()))
            return removed

        bridge._unlink_lock_if_inode = unlink_then_lose
        self.addCleanup(setattr, bridge, "_unlink_lock_if_inode", original)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            with self.assertRaises(bridge.LockHeld) as caught:
                with bridge.file_lock(self.state_dir):
                    self.fail("the winner's lock was stolen")

        self.assertEqual(caught.exception.pid, os.getpid())
        self.assertTrue(os.path.exists(self.lock_path()))

    def test_a_lock_released_during_the_judgement_is_claimed_cleanly(self):
        """The benign direction: the holder let go while we were looking."""
        self.hold_lock(str(os.getpid()))
        original = bridge._lock_owner_is_gone

        def released(path):
            os.unlink(path)
            return True

        bridge._lock_owner_is_gone = released
        self.addCleanup(setattr, bridge, "_lock_owner_is_gone", original)

        with self.assertLogs(bridge.LOGGER, level="WARNING"):
            with bridge.file_lock(self.state_dir) as path:
                self.assertEqual(bridge._lock_owner_pid(path), os.getpid())


class OddLockPathTest(LockCase):
    """R2-NB7: something that is not a regular file sits at the lock path."""

    def test_a_directory_at_the_lock_path_is_a_state_dir_error(self):
        os.makedirs(self.lock_path())

        with self.assertRaises(bridge.StateDirUnusable) as caught:
            with bridge.file_lock(self.state_dir):
                self.fail("a directory was treated as a lock file")

        self.assertEqual(caught.exception.path, self.state_dir)
        self.assertTrue(os.path.isdir(self.lock_path()))

    def test_a_symlink_at_the_lock_path_is_a_state_dir_error_too(self):
        os.makedirs(self.state_dir)
        target = os.path.join(self.base, "somewhere-else")
        with open(target, "w") as handle:
            handle.write(str(os.getpid()))
        os.symlink(target, self.lock_path())

        with self.assertRaises(bridge.StateDirUnusable):
            with bridge.file_lock(self.state_dir):
                self.fail("a symlink was treated as a lock file")

        self.assertTrue(os.path.islink(self.lock_path()))
        self.assertTrue(os.path.exists(target))

    def test_the_cli_reports_an_odd_lock_path_as_a_clean_exit_two(self):
        user_data = os.path.join(self.base, "userData")
        root = tl.make_root(user_data)
        leaf = tl.make_leaf(user_data, tl.uid(1), tl.uid(11))
        tl.write_record(leaf, tl.uid(5), mtime_ns=5_000_000_000)
        os.makedirs(self.lock_path())
        before = tl.snapshot(root)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            # `sync` never asks the runner for anything; passed for safety, so
            # no test can reach the real launchctl even by accident.
            code = bridge.main(["sync", "--user-data", user_data,
                                "--state-dir", self.state_dir, "--execute"],
                               runner=tl.RecordingRunner())

        self.assertEqual(code, 2)
        self.assertIn(self.state_dir, "\n".join(logs.output))
        self.assertEqual(tl.snapshot(root), before)


class LockFileModeTest(LockCase):
    def test_the_lock_file_is_owner_only(self):
        with bridge.file_lock(self.state_dir) as path:
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode) & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
