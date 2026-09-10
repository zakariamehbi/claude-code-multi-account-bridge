"""plan_sync: the pure merge. No filesystem involved in these tests."""

import hashlib
import os
import unittest

import bridge_testlib as tl
import bridge

LEAF_A = "/synthetic/root/%s/%s" % (tl.uid(1), tl.uid(11))
LEAF_B = "/synthetic/root/%s/%s" % (tl.uid(2), tl.uid(22))
LEAF_C = "/synthetic/root/%s/%s" % (tl.uid(3), tl.uid(33))
SID = tl.uid(5)


def entry(last_activity=1000, archived=False, mtime_ns=1000, title="t", raw=None):
    content = raw if raw is not None else tl.record_bytes(
        SID, last_activity=last_activity, archived=archived, title=title)
    return bridge._load_from_bytes(content, mtime_ns)


def tomb(mtime_ns=1000, content=b"1700000000000"):
    return bridge.FileEntry(content, mtime_ns)


def leaf(path, records=None, tombstones=None):
    return bridge.LeafState(path, dict(records or {}), dict(tombstones or {}))


def rec_path(leaf_path, session_id=SID):
    return os.path.join(leaf_path, "local_%s.json" % session_id)


def tomb_path(leaf_path, session_id=SID):
    return os.path.join(leaf_path, "deleted_%s" % session_id)


class UnionTest(unittest.TestCase):
    def test_record_in_one_leaf_is_written_to_the_others(self):
        a = entry(mtime_ns=5_000_000_000)
        plan = bridge.plan_sync([leaf(LEAF_A, {SID: a}), leaf(LEAF_B), leaf(LEAF_C)])

        self.assertEqual(
            sorted(w.dest for w in plan.writes),
            sorted([rec_path(LEAF_B), rec_path(LEAF_C)]))
        for write in plan.writes:
            self.assertEqual(write.content, a.content)
            self.assertEqual(write.mtime_ns, a.mtime_ns)
            self.assertEqual(write.source, rec_path(LEAF_A))
        self.assertEqual(plan.records_seen, 1)
        self.assertEqual(plan.deletes, ())
        self.assertEqual(plan.tombstone_copies, ())

    def test_single_leaf_produces_no_writes(self):
        plan = bridge.plan_sync([leaf(LEAF_A, {SID: entry()})])
        self.assertTrue(plan.is_empty())

    def test_identical_content_and_mtime_everywhere_is_empty(self):
        a = entry(mtime_ns=7_000_000_000)
        b = entry(mtime_ns=7_000_000_000)
        plan = bridge.plan_sync([leaf(LEAF_A, {SID: a}), leaf(LEAF_B, {SID: b})])
        self.assertTrue(plan.is_empty())

    def test_same_bytes_but_different_mtime_is_still_a_write(self):
        a = entry(mtime_ns=9_000_000_000)
        b = entry(mtime_ns=1_000_000_000)
        plan = bridge.plan_sync([leaf(LEAF_A, {SID: a}), leaf(LEAF_B, {SID: b})])

        self.assertEqual([w.dest for w in plan.writes], [rec_path(LEAF_B)])
        self.assertEqual(plan.writes[0].mtime_ns, 9_000_000_000)

    def test_leaves_are_reported_sorted(self):
        plan = bridge.plan_sync([leaf(LEAF_C), leaf(LEAF_A), leaf(LEAF_B)])
        self.assertEqual(plan.leaves, (LEAF_A, LEAF_B, LEAF_C))


class WinnerTest(unittest.TestCase):
    def winner_content(self, states):
        plan = bridge.plan_sync(states)
        self.assertTrue(plan.writes, "expected at least one write")
        contents = {w.content for w in plan.writes}
        self.assertEqual(len(contents), 1)
        return plan.writes[0].content, plan.writes[0].mtime_ns

    def test_newest_mtime_wins(self):
        older = entry(mtime_ns=1_000_000_000, title="older", last_activity=9999)
        newer = entry(mtime_ns=2_000_000_000, title="newer", last_activity=1)
        content, mtime_ns = self.winner_content(
            [leaf(LEAF_A, {SID: older}), leaf(LEAF_B, {SID: newer})])

        self.assertEqual(content, newer.content)
        self.assertEqual(mtime_ns, 2_000_000_000)

    def test_mtime_tie_breaks_on_larger_last_activity(self):
        low = entry(mtime_ns=3_000_000_000, last_activity=10, title="low")
        high = entry(mtime_ns=3_000_000_000, last_activity=20, title="high")
        content, _ = self.winner_content(
            [leaf(LEAF_A, {SID: low}), leaf(LEAF_B, {SID: high})])
        self.assertEqual(content, high.content)

    def test_missing_or_non_numeric_last_activity_loses_to_a_number(self):
        absent = bridge._load_from_bytes(b'{"id": "x"}', 3_000_000_000)
        junk = bridge._load_from_bytes(b'{"lastActivityAt": "oops"}', 3_000_000_000)
        numeric = entry(mtime_ns=3_000_000_000, last_activity=0)

        for other in (absent, junk):
            content, _ = self.winner_content(
                [leaf(LEAF_A, {SID: other}), leaf(LEAF_B, {SID: numeric})])
            self.assertEqual(content, numeric.content)

    def test_full_tie_breaks_deterministically_on_content_hash(self):
        one = entry(mtime_ns=4_000_000_000, last_activity=5, title="aaa")
        two = entry(mtime_ns=4_000_000_000, last_activity=5, title="bbb")
        expected = max([one.content, two.content],
                       key=lambda c: hashlib.sha256(c).hexdigest())

        forward = bridge.plan_sync([leaf(LEAF_A, {SID: one}), leaf(LEAF_B, {SID: two})])
        backward = bridge.plan_sync([leaf(LEAF_B, {SID: two}), leaf(LEAF_A, {SID: one})])

        self.assertEqual(forward.writes[0].content, expected)
        self.assertEqual(backward.writes[0].content, expected)
        self.assertEqual(forward, backward)

    def test_archived_copy_with_newer_mtime_wins_despite_older_activity(self):
        archived = entry(mtime_ns=8_000_000_000, last_activity=100, archived=True)
        active = entry(mtime_ns=7_000_000_000, last_activity=500, archived=False)

        plan = bridge.plan_sync(
            [leaf(LEAF_A, {SID: archived}), leaf(LEAF_B, {SID: active})])

        self.assertEqual([w.dest for w in plan.writes], [rec_path(LEAF_B)])
        self.assertEqual(plan.writes[0].content, archived.content)
        self.assertTrue(bridge.json.loads(plan.writes[0].content)["isArchived"])


class ParseFailureTest(unittest.TestCase):
    def test_unparsable_copy_is_never_chosen_and_is_overwritten(self):
        broken = bridge._load_from_bytes(b"{ truncated", 9_000_000_000)
        good = entry(mtime_ns=1_000_000_000)

        plan = bridge.plan_sync(
            [leaf(LEAF_A, {SID: broken}), leaf(LEAF_B, {SID: good})])

        self.assertEqual([w.dest for w in plan.writes], [rec_path(LEAF_A)])
        self.assertEqual(plan.writes[0].content, good.content)
        self.assertEqual(plan.deletes, ())
        self.assertEqual(plan.parse_failures, (rec_path(LEAF_A),))

    def test_all_copies_unparsable_means_no_writes_and_no_deletes(self):
        broken_a = bridge._load_from_bytes(b"{ truncated", 9_000_000_000)
        broken_b = bridge._load_from_bytes(b"nope", 1_000_000_000)

        plan = bridge.plan_sync(
            [leaf(LEAF_A, {SID: broken_a}), leaf(LEAF_B, {SID: broken_b})])

        self.assertTrue(plan.is_empty())
        self.assertEqual(plan.records_seen, 0)
        self.assertEqual(sorted(plan.parse_failures),
                         sorted([rec_path(LEAF_A), rec_path(LEAF_B)]))


class TombstoneTest(unittest.TestCase):
    def test_tombstone_deletes_the_record_everywhere_and_is_copied_everywhere(self):
        t = tomb(mtime_ns=5_000_000_000, content=b"1700000000001")
        plan = bridge.plan_sync([
            leaf(LEAF_A, {SID: entry(mtime_ns=9_000_000_000)}, {SID: t}),
            leaf(LEAF_B, {SID: entry(mtime_ns=1_000_000_000)}),
            leaf(LEAF_C),
        ])

        self.assertEqual(sorted(d.path for d in plan.deletes),
                         sorted([rec_path(LEAF_A), rec_path(LEAF_B)]))
        self.assertEqual(sorted(w.dest for w in plan.tombstone_copies),
                         sorted([tomb_path(LEAF_B), tomb_path(LEAF_C)]))
        for copy in plan.tombstone_copies:
            self.assertEqual(copy.content, b"1700000000001")
            self.assertEqual(copy.mtime_ns, 5_000_000_000)
            self.assertEqual(copy.source, tomb_path(LEAF_A))
        self.assertEqual(plan.writes, ())

    def test_tombstone_suppresses_the_record_write_in_a_leaf_that_lacks_it(self):
        plan = bridge.plan_sync([
            leaf(LEAF_A, {SID: entry(mtime_ns=9_000_000_000)}),
            leaf(LEAF_B, {}, {SID: tomb()}),
        ])

        self.assertEqual(plan.writes, ())
        self.assertEqual([d.path for d in plan.deletes], [rec_path(LEAF_A)])
        self.assertEqual([w.dest for w in plan.tombstone_copies], [tomb_path(LEAF_A)])

    def test_converged_tombstone_is_an_empty_plan(self):
        t = tomb(mtime_ns=5_000_000_000)
        plan = bridge.plan_sync([leaf(LEAF_A, {}, {SID: t}), leaf(LEAF_B, {}, {SID: t})])
        self.assertTrue(plan.is_empty())

    def test_differing_tombstone_bytes_are_left_alone(self):
        plan = bridge.plan_sync([
            leaf(LEAF_A, {}, {SID: tomb(mtime_ns=5_000_000_000, content=b"1")}),
            leaf(LEAF_B, {}, {SID: tomb(mtime_ns=6_000_000_000, content=b"2")}),
        ])
        self.assertTrue(plan.is_empty())

    def test_an_unparsable_record_is_deleted_when_tombstoned(self):
        broken = bridge._load_from_bytes(b"{ truncated", 9_000_000_000)
        plan = bridge.plan_sync([
            leaf(LEAF_A, {SID: broken}, {SID: tomb()}),
            leaf(LEAF_B, {}, {SID: tomb()}),
        ])
        self.assertEqual([d.path for d in plan.deletes], [rec_path(LEAF_A)])

    def test_deletes_only_ever_target_a_local_record_filename(self):
        plan = bridge.plan_sync([
            leaf(LEAF_A, {SID: entry()}, {SID: tomb()}),
            leaf(LEAF_B, {}, {SID: tomb()}),
        ])
        for delete in plan.deletes:
            name = os.path.basename(delete.path)
            self.assertTrue(name.startswith("local_") and name.endswith(".json"))
            self.assertTrue(bridge.record_id(name))


class IdMismatchTest(unittest.TestCase):
    """N19: the FILENAME is what the app reads the folder by, so it wins."""

    def mismatched(self):
        raw = tl.record_bytes(tl.uid(9))          # says id 9 ...
        return bridge._load_from_bytes(raw, 5_000_000_000)

    def test_a_record_whose_id_field_disagrees_is_counted_but_still_synced(self):
        plan = bridge.plan_sync([
            leaf(LEAF_A, {SID: self.mismatched()}),   # ... but is named 5
            leaf(LEAF_B),
        ])

        self.assertEqual(plan.id_mismatches, (rec_path(LEAF_A),))
        self.assertEqual([w.dest for w in plan.writes], [rec_path(LEAF_B)])
        self.assertEqual(plan.counts()["id_mismatches"], 1)

    def test_the_mismatch_is_reported_at_warning_level(self):
        plan = bridge.plan_sync([leaf(LEAF_A, {SID: self.mismatched()})])

        with self.assertLogs(bridge.LOGGER, level="WARNING") as logs:
            bridge.log_plan_findings(plan)

        joined = "\n".join(logs.output)
        self.assertIn(rec_path(LEAF_A), joined)
        self.assertIn("filename", joined)

    def test_a_case_only_difference_is_not_a_mismatch(self):
        upper = "00000000-0000-4000-8000-0000000000AB"
        entry_ = bridge._load_from_bytes(tl.record_bytes(upper), 5_000_000_000)
        plan = bridge.plan_sync([leaf(LEAF_A, {upper.lower(): entry_})])

        self.assertEqual(plan.id_mismatches, ())


class CaseSensitiveNameTest(unittest.TestCase):
    """R2-B2: the id is case-folded, the FILENAME is whatever is on disk.

    On a case-INSENSITIVE filesystem (APFS by default, HFS+) the two are
    interchangeable, which is why this went unnoticed. On Linux, or a
    case-sensitive APFS volume, `local_<UPPER>.json` and `local_<lower>.json`
    are different files: building the path from the folded id addresses a file
    that DOES NOT EXIST. The write then lands beside the real record, the plan
    never converges (full backup every run), a tombstone deletes nothing, and
    the run still exits 0. These tests build the leaf states directly, since
    the dev machine cannot hold both names at once.
    """

    UPPER = "00000000-0000-4000-8000-0000000000AB"
    LOWER = UPPER.lower()

    def upper_record(self, mtime_ns=5_000_000_000, title="t", raw=None):
        content = raw if raw is not None else tl.record_bytes(
            self.UPPER, title=title)
        return bridge._load_from_bytes(content, mtime_ns,
                                       name="local_%s.json" % self.UPPER)

    def lower_record(self, mtime_ns=5_000_000_000, title="t"):
        return bridge._load_from_bytes(
            tl.record_bytes(self.LOWER, title=title), mtime_ns,
            name="local_%s.json" % self.LOWER)

    def upper_tombstone(self, mtime_ns=5_000_000_000, content=b"1700000000000"):
        return bridge.FileEntry(content, mtime_ns,
                                name="deleted_%s" % self.UPPER)

    def upper_path(self, leaf_path):
        return os.path.join(leaf_path, "local_%s.json" % self.UPPER)

    def lower_path(self, leaf_path):
        return os.path.join(leaf_path, "local_%s.json" % self.LOWER)

    def test_the_winner_is_written_over_the_file_that_is_really_there(self):
        newer = self.lower_record(mtime_ns=9_000_000_000, title="newer")
        older = self.upper_record(mtime_ns=1_000_000_000, title="older")

        plan = bridge.plan_sync([leaf(LEAF_A, {self.LOWER: newer}),
                                 leaf(LEAF_B, {self.LOWER: older})])

        self.assertEqual([w.dest for w in plan.writes], [self.upper_path(LEAF_B)])
        self.assertEqual(plan.writes[0].expected,
                         (older.mtime_ns, older.digest))

    def test_the_source_path_is_the_on_disk_name_too(self):
        winner = self.upper_record(mtime_ns=9_000_000_000, title="winner")

        plan = bridge.plan_sync([leaf(LEAF_A, {self.LOWER: winner}),
                                 leaf(LEAF_B)])

        self.assertEqual(plan.writes[0].source, self.upper_path(LEAF_A))
        # the leaf that has nothing keeps the canonical lower-cased name
        self.assertEqual(plan.writes[0].dest, self.lower_path(LEAF_B))

    def test_two_differently_cased_copies_of_one_id_converge(self):
        """The bug's headline symptom: this plan was never empty."""
        content = tl.record_bytes(self.LOWER)
        lower = bridge._load_from_bytes(content, 7_000_000_000,
                                        name="local_%s.json" % self.LOWER)
        upper = bridge._load_from_bytes(content, 7_000_000_000,
                                        name="local_%s.json" % self.UPPER)

        plan = bridge.plan_sync([leaf(LEAF_A, {self.LOWER: lower}),
                                 leaf(LEAF_B, {self.LOWER: upper})])

        self.assertTrue(plan.is_empty())

    def test_a_tombstone_deletes_the_file_that_is_really_there(self):
        plan = bridge.plan_sync([
            leaf(LEAF_A, {self.LOWER: self.upper_record()},
                 {self.LOWER: self.upper_tombstone()}),
            leaf(LEAF_B),
        ])

        self.assertEqual([d.path for d in plan.deletes],
                         [self.upper_path(LEAF_A)])
        self.assertEqual([w.dest for w in plan.tombstone_copies],
                         [os.path.join(LEAF_B, "deleted_%s" % self.LOWER)])
        self.assertEqual(plan.tombstone_copies[0].source,
                         os.path.join(LEAF_A, "deleted_%s" % self.UPPER))

    def test_a_leaf_that_already_holds_an_upper_cased_tombstone_gets_no_copy(self):
        tombstone = self.upper_tombstone(mtime_ns=5_000_000_000)
        plan = bridge.plan_sync([
            leaf(LEAF_A, {}, {self.LOWER: tombstone}),
            leaf(LEAF_B, {}, {self.LOWER: bridge.FileEntry(
                tombstone.content, tombstone.mtime_ns,
                name="deleted_%s" % self.LOWER)}),
        ])

        self.assertTrue(plan.is_empty())

    def test_a_blocked_on_disk_path_still_suppresses_its_delete(self):
        """R2 gap 15: `doomed not in blocked`, with the real name.

        The blocked table is keyed on the path ON DISK, so if the doomed path
        were built from the folded id the two would never match and a delete
        would be planned for a path the tool has already refused to touch.
        """
        state = bridge.LeafState(LEAF_A, {self.LOWER: self.upper_record()},
                                 {self.LOWER: self.upper_tombstone()})
        state.blocked[self.upper_path(LEAF_A)] = "not a regular file"

        plan = bridge.plan_sync([state])

        self.assertEqual(plan.deletes, ())
        self.assertEqual(plan.blocked, ((self.upper_path(LEAF_A),
                                         "not a regular file"),))

    def test_a_blocked_on_disk_path_still_suppresses_its_write(self):
        state = bridge.LeafState(LEAF_B, {self.LOWER: self.upper_record(
            mtime_ns=1_000_000_000)})
        state.blocked[self.upper_path(LEAF_B)] = "not a regular file"

        plan = bridge.plan_sync([
            leaf(LEAF_A, {self.LOWER: self.lower_record(mtime_ns=9_000_000_000)}),
            state,
        ])

        self.assertEqual(plan.writes, ())

    def test_the_parse_failure_and_mismatch_reports_name_the_real_file(self):
        broken = self.upper_record(raw=b"{ truncated")
        mismatched = bridge._load_from_bytes(
            tl.record_bytes(tl.uid(9)), 5_000_000_000,
            name="local_%s.json" % self.UPPER)

        broken_plan = bridge.plan_sync([leaf(LEAF_A, {self.LOWER: broken})])
        mismatch_plan = bridge.plan_sync([leaf(LEAF_A, {self.LOWER: mismatched})])

        self.assertEqual(broken_plan.parse_failures, (self.upper_path(LEAF_A),))
        self.assertEqual(mismatch_plan.id_mismatches, (self.upper_path(LEAF_A),))


class CountsTest(unittest.TestCase):
    def test_counts_cover_every_reported_quantity(self):
        other = tl.uid(6)
        plan = bridge.plan_sync([
            leaf(LEAF_A, {SID: entry(mtime_ns=9_000_000_000)}),
            leaf(LEAF_B, {other: bridge._load_from_bytes(
                tl.record_bytes(other), 2_000_000_000)}),
        ])
        counts = plan.counts()

        self.assertEqual(counts["leaves"], 2)
        self.assertEqual(counts["records_seen"], 2)
        self.assertEqual(counts["copies_written"], 2)
        self.assertEqual(counts["records_deleted"], 0)
        self.assertEqual(counts["tombstones_copied"], 0)
        self.assertEqual(counts["parse_failures"], 0)
        self.assertEqual(counts["blocked"], 0)
        self.assertEqual(counts["ignored"], 0)
        self.assertEqual(counts["id_mismatches"], 0)

    def test_records_seen_counts_unique_session_ids_not_copies(self):
        """N20: three leaves holding the same two ids is still two records."""
        other = tl.uid(6)
        first = entry(mtime_ns=9_000_000_000)
        second = bridge._load_from_bytes(tl.record_bytes(other), 2_000_000_000)
        plan = bridge.plan_sync([
            leaf(LEAF_A, {SID: first, other: second}),
            leaf(LEAF_B, {SID: first, other: second}),
            leaf(LEAF_C, {SID: first}),
        ])

        self.assertEqual(plan.records_seen, 2)

    def test_a_blocked_id_is_still_counted_as_a_record_seen(self):
        state = bridge.LeafState(LEAF_A, {SID: entry(mtime_ns=9_000_000_000)})
        state.blocked_ids[SID] = "unreadable"
        plan = bridge.plan_sync([state, leaf(LEAF_B)])

        self.assertEqual(plan.records_seen, 1)
        self.assertEqual(plan.writes, ())


if __name__ == "__main__":
    unittest.main()
