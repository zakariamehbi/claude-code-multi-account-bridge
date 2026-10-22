"""scheduled-tasks.json merge across accounts, against synthetic trees."""

import json
import os
import shutil
import tempfile
import unittest

import bridge_testlib as tl
import bridge


def task(task_id, **fields):
    payload = {"id": task_id, "displayName": task_id, "enabled": True,
               "filePath": "/synthetic/%s/SKILL.md" % task_id}
    payload.update(fields)
    return payload


def write_tasks(leaf, tasks, mtime_ns, skips=None):
    data = {"scheduledTasks": tasks, "recordedSkips": skips or {},
            "sundayAliasBoundaryStamped": True}
    return tl.write_other(leaf, "scheduled-tasks.json",
                          json.dumps(data, indent=2).encode("utf-8"), mtime_ns)


def read_tasks(leaf):
    with open(os.path.join(leaf, "scheduled-tasks.json"), "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def ids(leaf):
    return [t["id"] for t in read_tasks(leaf)["scheduledTasks"]]


class TasksCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.state_dir = os.path.join(self.base, "state")
        self.root = tl.make_root(self.base)
        self.a = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        self.b = tl.make_leaf(self.base, tl.uid(2), tl.uid(22))

    def sync(self, execute=True, app_running=False):
        return bridge.sync_once(self.root, execute=execute,
                                state_dir=self.state_dir, app_running=app_running)

    def set_active(self, account):
        tl.write_other(self.base, "config.json", json.dumps(
            {"lastKnownAccountUuid": account}).encode("utf-8"))


class MergeTest(TasksCase):
    def test_a_task_on_one_account_is_copied_to_the_other(self):
        write_tasks(self.a, [task("daily")], 5_000_000_000)
        write_tasks(self.b, [], 4_000_000_000)

        result = self.sync()

        self.assertEqual(ids(self.b), ["daily"])
        self.assertEqual(result.applied["tasks_written"], 1)
        self.assertEqual(read_tasks(self.b)["sundayAliasBoundaryStamped"], True)

    def test_converged_tree_writes_nothing_on_the_next_run(self):
        write_tasks(self.a, [task("daily")], 5_000_000_000)
        write_tasks(self.b, [task("weekly")], 4_000_000_000)
        self.sync()
        before = tl.snapshot(self.root)

        result = self.sync()

        self.assertEqual(result.tasks.writes, ())
        self.assertEqual(tl.snapshot(self.root), before)
        self.assertEqual(sorted(ids(self.a)), ["daily", "weekly"])

    def test_newest_file_wins_the_body_and_newest_run_fields_win(self):
        write_tasks(self.a, [task("t", enabled=False,
                                  lastRunAt="2026-01-01T00:00:00.000Z")],
                    5_000_000_000)
        write_tasks(self.b, [task("t", enabled=True,
                                  lastRunAt="2026-02-01T00:00:00.000Z")],
                    4_000_000_000)

        self.sync()

        for leaf in (self.a, self.b):
            merged = read_tasks(leaf)["scheduledTasks"][0]
            self.assertFalse(merged["enabled"])
            self.assertEqual(merged["lastRunAt"], "2026-02-01T00:00:00.000Z")

    def test_a_delete_on_one_account_is_a_delete_everywhere(self):
        write_tasks(self.a, [task("keep"), task("gone")], 5_000_000_000)
        write_tasks(self.b, [task("keep"), task("gone")], 5_000_000_000)
        self.sync()
        write_tasks(self.a, [task("keep")], 6_000_000_000,
                    skips={"gone": [{"at": 1, "reason": "x"}]})

        result = self.sync()

        self.assertEqual(result.tasks.deleted, ("gone",))
        self.assertEqual(ids(self.a), ["keep"])
        self.assertEqual(ids(self.b), ["keep"])
        self.assertNotIn("gone", read_tasks(self.b)["recordedSkips"])

    def test_without_a_base_nothing_is_treated_as_deleted(self):
        write_tasks(self.a, [task("x")], 5_000_000_000)
        write_tasks(self.b, [], 6_000_000_000)

        self.sync()

        self.assertEqual(ids(self.a), ["x"])
        self.assertEqual(ids(self.b), ["x"])

    def test_a_dry_run_writes_nothing_and_saves_no_base(self):
        write_tasks(self.a, [task("x")], 5_000_000_000)
        write_tasks(self.b, [], 4_000_000_000)
        before = tl.snapshot(self.root)

        result = self.sync(execute=False)

        self.assertEqual(len(result.tasks.writes), 1)
        self.assertEqual(tl.snapshot(self.root), before)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_a_leaf_without_the_file_or_with_another_shape_is_left_alone(self):
        write_tasks(self.a, [task("x")], 5_000_000_000)
        other = tl.write_other(self.b, "scheduled-tasks.json", b'{"tasks": []}',
                               1_000_000_000)
        c = tl.make_leaf(self.base, tl.uid(3), tl.uid(33))

        self.sync()

        with open(other, "rb") as handle:
            self.assertEqual(handle.read(), b'{"tasks": []}')
        self.assertFalse(os.path.exists(os.path.join(c, "scheduled-tasks.json")))


class RunningAppTest(TasksCase):
    """The running app rewrites its account's file from memory: never write it
    then, and never read the file it rewrote as a delete of what it missed."""

    def test_the_loaded_account_is_held_back_while_the_app_runs(self):
        self.set_active(tl.uid(2))
        write_tasks(self.a, [task("x")], 5_000_000_000)
        held = write_tasks(self.b, [], 4_000_000_000)
        before = os.lstat(held).st_mtime_ns

        result = self.sync(app_running=True)

        self.assertEqual(ids(self.b), [])
        self.assertEqual(os.lstat(held).st_mtime_ns, before)
        self.assertEqual(result.tasks.held_back, (self.b,))

    def test_the_loaded_account_is_written_once_the_app_quits(self):
        self.set_active(tl.uid(2))
        write_tasks(self.a, [task("x")], 5_000_000_000)
        write_tasks(self.b, [], 4_000_000_000)
        self.sync(app_running=True)

        self.sync(app_running=False)

        self.assertEqual(ids(self.b), ["x"])

    def test_the_app_rewriting_its_stale_list_does_not_delete_tasks(self):
        self.set_active(tl.uid(2))
        write_tasks(self.a, [task("x")], 5_000_000_000)
        write_tasks(self.b, [], 4_000_000_000)
        self.sync(app_running=True)
        # The app, still holding its empty list, adds a task and saves.
        write_tasks(self.b, [task("new")], 7_000_000_000)

        self.sync(app_running=True)

        self.assertEqual(sorted(ids(self.a)), ["new", "x"])


if __name__ == "__main__":
    unittest.main()


class PendingDeletionTest(TasksCase):
    """Review #1: a deletion that cannot reach a held-back leaf must survive
    until it can, not be undone by the stale copy on the next run."""

    def test_a_delete_waits_for_the_held_back_leaf_instead_of_being_undone(self):
        write_tasks(self.a, [task("x"), task("keep")], 5_000_000_000)
        write_tasks(self.b, [task("x"), task("keep")], 5_000_000_000)
        self.sync()
        write_tasks(self.b, [task("keep")], 6_000_000_000)
        self.set_active(tl.uid(1))

        self.sync(app_running=True)
        self.sync(app_running=True)

        self.assertEqual(ids(self.b), ["keep"])
        self.assertEqual(sorted(ids(self.a)), ["keep", "x"])

        self.sync(app_running=False)

        self.assertEqual(ids(self.a), ["keep"])
        self.assertEqual(ids(self.b), ["keep"])
        self.assertEqual(bridge.load_pending(self.state_dir, "tasks\n" + self.root),
                         set())
