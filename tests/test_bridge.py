"""Leaf discovery and per-leaf reading."""

import os
import shutil
import tempfile
import unittest

import bridge_testlib as tl
import bridge


class DiscoverLeavesTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_finds_exactly_two_level_uuid_leaves(self):
        a = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        b = tl.make_leaf(self.base, tl.uid(1), tl.uid(12))
        c = tl.make_leaf(self.base, tl.uid(2), tl.uid(13))
        root = tl.make_root(self.base)

        self.assertEqual(bridge.discover_leaves(root), sorted([a, b, c]))

    def test_missing_root_is_a_clean_empty_result(self):
        root = os.path.join(self.base, tl.SESSIONS_DIRNAME)
        self.assertFalse(os.path.exists(root))
        self.assertEqual(bridge.discover_leaves(root), [])

    def test_non_uuid_directories_are_skipped_at_both_levels(self):
        good = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        root = tl.make_root(self.base)
        os.makedirs(os.path.join(root, "not-a-uuid", tl.uid(11)))
        os.makedirs(os.path.join(root, tl.uid(1), "not-a-uuid"))
        # a uuid without dashes is not canonical and must be skipped
        os.makedirs(os.path.join(root, "0" * 32, tl.uid(11)))

        self.assertEqual(bridge.discover_leaves(root), [good])

    def test_files_at_account_and_org_level_are_skipped(self):
        good = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        root = tl.make_root(self.base)
        tl.write_other(root, tl.uid(2), content=b"not a dir")
        tl.write_other(os.path.join(root, tl.uid(1)), tl.uid(12), content=b"not a dir")

        self.assertEqual(bridge.discover_leaves(root), [good])

    def test_symlinked_leaf_is_skipped_and_not_followed(self):
        real = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        root = tl.make_root(self.base)
        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(root, tl.uid(1), tl.uid(12)))

        self.assertEqual(bridge.discover_leaves(root), [real])

    def test_symlinked_account_directory_is_skipped(self):
        real = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        root = tl.make_root(self.base)
        outside = os.path.join(self.base, "outside", tl.uid(11))
        os.makedirs(outside)
        os.symlink(os.path.dirname(outside), os.path.join(root, tl.uid(2)))

        self.assertEqual(bridge.discover_leaves(root), [real])

    def test_deeper_nesting_is_not_descended_into(self):
        leaf = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        os.makedirs(os.path.join(leaf, tl.uid(99)))
        root = tl.make_root(self.base)

        self.assertEqual(bridge.discover_leaves(root), [leaf])


class ReadLeafTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_reads_records_and_tombstones_and_ignores_other_files(self):
        leaf = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        tl.write_record(leaf, tl.uid(5), mtime_ns=5_000_000_000)
        tl.write_tombstone(leaf, tl.uid(6), mtime_ns=6_000_000_000)
        tl.write_other(leaf, "scheduled-tasks.json")
        tl.write_other(leaf, "spaces.json")
        tl.write_other(leaf, "backlog/item.json")

        state = bridge.read_leaf(leaf)

        self.assertEqual(state.path, leaf)
        self.assertEqual(sorted(state.records), [tl.uid(5)])
        self.assertEqual(sorted(state.tombstones), [tl.uid(6)])

    def test_record_entry_carries_bytes_mtime_and_parsed_object(self):
        leaf = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        raw = tl.record_bytes(tl.uid(5), last_activity=1234)
        tl.write_record(leaf, tl.uid(5), mtime_ns=7_000_000_000, raw=raw)

        entry = bridge.read_leaf(leaf).records[tl.uid(5)]

        self.assertEqual(entry.content, raw)
        self.assertEqual(entry.mtime_ns, 7_000_000_000)
        self.assertEqual(entry.parsed["lastActivityAt"], 1234)

    def test_unparsable_and_non_object_json_have_parsed_none(self):
        leaf = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        tl.write_record(leaf, tl.uid(5), mtime_ns=1_000_000_000, raw=b"{not json")
        tl.write_record(leaf, tl.uid(6), mtime_ns=1_000_000_000, raw=b"[1, 2, 3]")

        records = bridge.read_leaf(leaf).records

        self.assertIsNone(records[tl.uid(5)].parsed)
        self.assertIsNone(records[tl.uid(6)].parsed)

    def test_non_uuid_and_symlinked_record_names_are_ignored(self):
        leaf = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))
        tl.write_other(leaf, "local_not-a-uuid.json")
        tl.write_other(leaf, "local_%s.txt" % tl.uid(5))
        tl.write_other(leaf, "deleted_not-a-uuid")
        target = tl.write_other(self.base, "elsewhere.json", content=b"{}")
        os.symlink(target, os.path.join(leaf, "local_%s.json" % tl.uid(7)))

        state = bridge.read_leaf(leaf)

        self.assertEqual(state.records, {})
        self.assertEqual(state.tombstones, {})


class BlockedEntryTest(unittest.TestCase):
    """Entries the bridge can neither read nor write are BLOCKED, not ignored."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.leaf = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))

    def test_a_directory_named_like_a_record_is_blocked(self):
        path = os.path.join(self.leaf, "local_%s.json" % tl.uid(5))
        os.makedirs(path)

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(state.records, {})
        self.assertEqual(sorted(state.blocked), [path])
        self.assertNotIn(tl.uid(5), state.blocked_ids)

    def test_a_directory_named_like_a_tombstone_is_blocked(self):
        path = os.path.join(self.leaf, "deleted_%s" % tl.uid(6))
        os.makedirs(path)

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(state.tombstones, {})
        self.assertEqual(sorted(state.blocked), [path])

    def test_a_symlinked_record_is_blocked_and_never_silent(self):
        target = tl.write_other(self.base, "elsewhere.json", content=b"{}")
        link = os.path.join(self.leaf, "local_%s.json" % tl.uid(5))
        os.symlink(target, link)

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(state.records, {})
        self.assertEqual(sorted(state.blocked), [link])

    def test_an_unreadable_record_blocks_its_id_everywhere(self):
        path = tl.write_record(self.leaf, tl.uid(5), mtime_ns=5_000_000_000)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o600)

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(state.records, {})
        self.assertEqual(sorted(state.blocked), [path])
        self.assertIn(tl.uid(5), state.blocked_ids)

    def test_an_unreadable_tombstone_blocks_its_id_too(self):
        """Its bytes are unknown, so the id is not in `tombstones` and the
        record would otherwise be copied around as if nothing had been deleted."""
        path = tl.write_tombstone(self.leaf, tl.uid(5), mtime_ns=5_000_000_000)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o600)

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(state.tombstones, {})
        self.assertEqual(sorted(state.blocked), [path])
        self.assertIn(tl.uid(5), state.blocked_ids)

    def test_a_record_over_the_size_cap_blocks_its_id(self):
        original = bridge.MAX_RECORD_BYTES
        bridge.MAX_RECORD_BYTES = 16
        self.addCleanup(setattr, bridge, "MAX_RECORD_BYTES", original)
        path = tl.write_record(self.leaf, tl.uid(5), mtime_ns=5_000_000_000)

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(state.records, {})
        self.assertEqual(sorted(state.blocked), [path])
        self.assertIn(tl.uid(5), state.blocked_ids)

    def test_a_record_at_the_size_cap_is_still_read(self):
        path = tl.write_record(self.leaf, tl.uid(5), mtime_ns=5_000_000_000)
        original = bridge.MAX_RECORD_BYTES
        bridge.MAX_RECORD_BYTES = os.lstat(path).st_size
        self.addCleanup(setattr, bridge, "MAX_RECORD_BYTES", original)

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(sorted(state.records), [tl.uid(5)])
        self.assertEqual(state.blocked, {})


class IgnoredNameTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.leaf = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))

    def test_names_that_look_like_ours_but_fail_the_uuid_test_are_ignored(self):
        first = tl.write_other(self.leaf, "local_not-a-uuid.json")
        second = tl.write_other(self.leaf, "deleted_not-a-uuid")
        third = tl.write_other(self.leaf, "deleted_%s.json" % tl.uid(5))

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(sorted(state.ignored), sorted([first, second, third]))
        self.assertEqual(state.blocked, {})

    def test_ignored_names_are_logged_at_debug_never_silently_dropped(self):
        """N9: not a warning (they are none of our business), but not silent."""
        path = tl.write_other(self.leaf, "local_not-a-uuid.json")
        plan = bridge.plan_sync([bridge.read_leaf(self.leaf)])

        with self.assertLogs(bridge.LOGGER, level="DEBUG") as logs:
            bridge.log_plan_findings(plan)

        self.assertIn(path, "\n".join(logs.output))
        self.assertTrue(all(record.levelno == bridge.logging.DEBUG
                            for record in logs.records))

    def test_a_file_that_does_not_look_like_ours_is_not_even_ignored(self):
        tl.write_other(self.leaf, "scheduled-tasks.json")
        tl.write_other(self.leaf, "local_%s.txt" % tl.uid(5))

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(state.ignored, {})
        self.assertEqual(state.blocked, {})


class CaseFoldedIdTest(unittest.TestCase):
    """APFS is case-insensitive: one file must not become two session ids."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.leaf = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))

    def test_record_and_tombstone_ids_are_lowercased(self):
        upper = "00000000-0000-4000-8000-0000000000AB"
        self.assertEqual(bridge.record_id("local_%s.json" % upper), upper.lower())
        self.assertEqual(bridge.tombstone_id("deleted_%s" % upper), upper.lower())

    def test_an_uppercase_named_record_is_read_under_the_lowercase_id(self):
        upper = "00000000-0000-4000-8000-0000000000AB"
        tl.write_record(self.leaf, upper, mtime_ns=5_000_000_000)

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(sorted(state.records), [upper.lower()])

    def test_the_on_disk_name_is_kept_on_the_entry(self):
        """R2-B2: the folded id is the identity, the name is where it lives."""
        upper = "00000000-0000-4000-8000-0000000000AB"
        tl.write_record(self.leaf, upper, mtime_ns=5_000_000_000)
        tl.write_tombstone(self.leaf, tl.uid(6), mtime_ns=6_000_000_000)

        state = bridge.read_leaf(self.leaf)

        self.assertEqual(state.records[upper.lower()].name,
                         "local_%s.json" % upper)
        self.assertEqual(state.tombstones[tl.uid(6)].name,
                         "deleted_%s" % tl.uid(6))
        self.assertEqual(state.record_path(upper.lower()),
                         os.path.join(self.leaf, "local_%s.json" % upper))
        self.assertEqual(state.tombstone_path(tl.uid(6)),
                         os.path.join(self.leaf, "deleted_%s" % tl.uid(6)))


class FakeEntry(object):
    """A directory entry with a name of our choosing, pointing at a real file.

    The dev machine's filesystem is case-INSENSITIVE, so it cannot hold both
    `local_<UUID>.json` and `local_<uuid>.json` at once. This presents two real
    files under the two names, which is exactly what a case-sensitive volume
    would hand `os.scandir`.
    """

    def __init__(self, leaf, name, path):
        self.name = name
        self.path = path
        self.leaf = leaf

    def is_symlink(self):
        return os.path.islink(self.path)

    def is_file(self, follow_symlinks=True):
        return os.path.isfile(self.path)


class AmbiguousCaseVariantTest(unittest.TestCase):
    """R2-B2: one id under two names in ONE leaf is ambiguous, so hands off.

    Only reachable on a case-sensitive filesystem. There is no way to tell
    which of the two the app reads, so neither is written to and neither is
    deleted: both are `blocked` and reported.
    """

    UPPER = "00000000-0000-4000-8000-0000000000AB"
    LOWER = UPPER.lower()

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.leaf = tl.make_leaf(self.base, tl.uid(1), tl.uid(11))

    def stand_in(self, real_name, content):
        path = tl.write_other(self.leaf, real_name, content=content)
        return path

    def entries(self, *pairs):
        return [FakeEntry(self.leaf, name, path) for name, path in pairs]

    def read(self, entries):
        return bridge._read_entries(bridge.LeafState(self.leaf), entries)

    def two_records(self):
        lower_path = self.stand_in("stand-in-lower.json",
                                   tl.record_bytes(self.LOWER))
        upper_path = self.stand_in("stand-in-upper.json",
                                   tl.record_bytes(self.UPPER))
        return lower_path, upper_path

    def test_both_copies_are_blocked_and_neither_is_a_record(self):
        lower_path, upper_path = self.two_records()

        state = self.read(self.entries(
            ("local_%s.json" % self.LOWER, lower_path),
            ("local_%s.json" % self.UPPER, upper_path)))

        self.assertEqual(state.records, {})
        self.assertEqual(sorted(state.blocked), sorted([lower_path, upper_path]))
        for reason in state.blocked.values():
            self.assertIn("case", reason)

    def test_the_ambiguity_is_logged_at_warning_level(self):
        lower_path, upper_path = self.two_records()

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            self.read(self.entries(
                ("local_%s.json" % self.LOWER, lower_path),
                ("local_%s.json" % self.UPPER, upper_path)))

        joined = "\n".join(logs.output)
        self.assertIn(upper_path, joined)
        self.assertIn("case", joined)

    def test_a_third_variant_is_blocked_as_well(self):
        lower_path, upper_path = self.two_records()
        mixed_path = self.stand_in("stand-in-mixed.json",
                                   tl.record_bytes(self.LOWER))
        mixed = "local_%s.json" % (self.LOWER[:-1] + "B")

        state = self.read(self.entries(
            ("local_%s.json" % self.LOWER, lower_path),
            (mixed, mixed_path),
            ("local_%s.json" % self.UPPER, upper_path)))

        self.assertEqual(state.records, {})
        self.assertEqual(sorted(state.blocked),
                         sorted([lower_path, mixed_path, upper_path]))

    def test_two_tombstone_variants_are_blocked_too(self):
        lower_path = self.stand_in("stand-in-lower", b"1700000000000")
        upper_path = self.stand_in("stand-in-upper", b"1700000000000")

        state = self.read(self.entries(
            ("deleted_%s" % self.LOWER, lower_path),
            ("deleted_%s" % self.UPPER, upper_path)))

        self.assertEqual(state.tombstones, {})
        self.assertEqual(sorted(state.blocked), sorted([lower_path, upper_path]))

    def test_a_record_and_a_tombstone_for_one_id_are_not_ambiguous(self):
        """The normal, everyday case: it must keep working."""
        record_path = self.stand_in("stand-in-record.json",
                                    tl.record_bytes(self.LOWER))
        tombstone_path = self.stand_in("stand-in-tombstone", b"1700000000000")

        state = self.read(self.entries(
            ("deleted_%s" % self.LOWER, tombstone_path),
            ("local_%s.json" % self.LOWER, record_path)))

        self.assertEqual(sorted(state.records), [self.LOWER])
        self.assertEqual(sorted(state.tombstones), [self.LOWER])
        self.assertEqual(state.blocked, {})

    def test_neither_copy_is_written_to_or_deleted(self):
        """The planner consequence: blocked paths get no write and no delete."""
        lower_path, upper_path = self.two_records()
        other = bridge.LeafState(
            "/synthetic/other-leaf",
            {self.LOWER: bridge._load_from_bytes(
                tl.record_bytes(self.LOWER), 9_000_000_000,
                name="local_%s.json" % self.LOWER)},
            {self.LOWER: bridge.FileEntry(b"1700000000000", 9_000_000_000,
                                          name="deleted_%s" % self.LOWER)})
        state = self.read(self.entries(
            ("local_%s.json" % self.LOWER, lower_path),
            ("local_%s.json" % self.UPPER, upper_path)))

        plan = bridge.plan_sync([state, other])

        # nothing touches either ambiguous copy ...
        for write in plan.writes + plan.tombstone_copies:
            self.assertNotIn(write.dest, (lower_path, upper_path))
        self.assertEqual([d.path for d in plan.deletes if d.path.startswith(
            self.leaf)], [])
        self.assertEqual(plan.counts()["blocked"], 2)
        # ... while the other leaf still gets its own tombstoned record removed
        self.assertEqual([d.path for d in plan.deletes],
                         [os.path.join("/synthetic/other-leaf",
                                       "local_%s.json" % self.LOWER)])


if __name__ == "__main__":
    unittest.main()
