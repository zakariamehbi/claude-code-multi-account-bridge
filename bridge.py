#!/usr/bin/env python3
"""claude-code-multi-account-bridge — converge the per-account Claude Code
session list across every account/org folder of one desktop install.

See README.md for the problem statement and docs/design.md for the spec.

======================================================================
EXTERNAL CONTRACTS THIS TOOL DEPENDS ON
======================================================================
Filesystem (POSIX / APFS / HFS+ / NTFS via os):
  * os.replace(src, dst) is atomic when src and dst are on the SAME
    filesystem. Guaranteed only if both live in the same directory, so the
    temp file is always created INSIDE the destination leaf, never in /tmp.
    A reader therefore sees either the whole old file or the whole new one.
  * os.replace() over an existing symlink replaces the LINK, it does not
    write through it. We still refuse such a destination outright, because
    replacing a link the user created is a surprise either way.
  * st_mtime is a float and loses precision on APFS (nanosecond stored
    resolution). Every comparison and every write-back therefore uses
    st_mtime_ns / os.utime(path, ns=(atime_ns, mtime_ns)), never the float
    form: float mtimes make equal timestamps compare unequal.
  * os.utime(..., ns=...) sets mtime AFTER the rename; the rename itself
    always stamps "now", so the order is write -> replace -> utime.
  * os.scandir() + DirEntry.is_dir(follow_symlinks=False) is the only
    directory test used, so a symlinked account/org directory is never
    descended into. os.path.realpath() is used in exactly ONE place — the
    state-directory-inside-the-sessions-root comparison in _is_inside — and
    never to resolve a write target. shutil.copytree(..., symlinks=True)
    copies links as links; its `ignore` callback is what leaves an
    unreadable record OUT of a backup instead of failing the whole backup.
  * os.link(temp, lock_path) fails with FileExistsError when something is
    already at lock_path, whatever kind of file it is. That one step is the
    whole locking primitive, and it publishes a lock file that already
    contains its owner's pid — a lock file created empty and filled in
    afterwards is readable, by a concurrent run, in its pidless state.
    st_ino identifies the file behind a name: a reclaim unlinks only the
    inode it judged stale, and a teardown only the inode it created.
    Hard links are POSIX (and work on NTFS); a filesystem without them
    makes the state directory unusable, which is reported as such.
  * A lock is reclaimed ONLY when its recorded pid is provably gone:
    os.kill(pid, 0) delivers no signal and raises ProcessLookupError for a
    dead process, while PermissionError means the process exists under
    another user, so the lock stands. A live owner's lock is never stolen.
    Pids are REUSED, so a stale lock whose number now belongs to some
    unrelated process reads as live — see the README's troubleshooting.
  * os.access(dir, W_OK | X_OK) is what finds a leaf that cannot be written
    into BEFORE anything is planned for it. It cannot promise a write will
    succeed (permissions change, ACLs, races), so the apply-time guards and
    the error counting stay exactly as they were.
  * A case-INSENSITIVE filesystem (APFS, HFS+) makes local_<UUID>.json and
    local_<uuid>.json one file; a case-SENSITIVE one makes them two. Ids are
    therefore folded for identity, while every path is built from the
    basename the directory listing gave — see FileEntry.name.
  * fsync() on a directory file descriptor makes the rename itself durable.
    Not every filesystem supports it, so a failure there is ignored: the
    bytes are already in place.
  * shutil.copytree() raises shutil.Error (an OSError subclass) collecting
    per-file failures, with the tree HALF copied; one unreadable file is
    enough. shutil.rmtree()'s onerror callback is deprecated from 3.12 and
    onexc does not exist before it, so neither is used: a tree is made
    writable with an explicit chmod walk before a plain rmtree.
  * Directory mtimes change when entries are added/removed, so tree
    snapshots in tests compare file bytes and mtimes, and treat directory
    mtimes as incidental.

launchd (macOS only):
  * A per-user LaunchAgent lives in ~/Library/LaunchAgents/<label>.plist.
  * Load:   launchctl bootstrap gui/<uid> <plist path>
  * Unload: launchctl bootout   gui/<uid>/<label>
  * Query:  launchctl print     gui/<uid>/<label>   (non-zero => not loaded)
    <uid> is os.getuid(); "gui/<uid>" is the per-login-session domain.
  * WatchPaths fires the job when a watched directory's contents change;
    ThrottleInterval is the minimum seconds between launches (5 here);
    StartInterval additionally starts the job every N seconds (60 here).
  * plist files are XML with the -//Apple//DTD PLIST 1.0//EN doctype;
    plistlib.dumps() emits exactly that, so the XML is correct by
    construction rather than by string templating.
  * launchctl is only ever invoked through an injectable runner, so tests
    never touch the real launchd domain.

Claude desktop app (observed layout, see docs/design.md):
  * <userData>/claude-code-sessions/<accountUuid>/<orgUuid>/ holds
    local_<sessionId>.json records and deleted_<sessionId> tombstones.
  * The app reads that folder when an account loads and does not watch it,
    so a merged list appears on the next account switch or relaunch.
  * Transcripts live outside userData and are already account-independent.
"""

import argparse
import collections
import contextlib
import datetime
import hashlib
import json
import logging
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time

__version__ = "0.1.0"

SESSIONS_DIRNAME = "claude-code-sessions"
# Cowork keeps the same <account>/<org> layout here.
COWORK_DIRNAME = "local-agent-mode-sessions"
LABEL = "com.claude-code-multi-account-bridge"
RECORD_PREFIX = "local_"
RECORD_SUFFIX = ".json"
TOMBSTONE_PREFIX = "deleted_"
THROTTLE_INTERVAL = 5
# A periodic floor: the app reads its folder on account load, which writes
# nothing, so a switch after a quiet stretch needs a run that no WatchPaths
# event triggers. A converged tree costs a run nothing (no writes, no backup).
START_INTERVAL = 60

# Every "we could not read this" reason starts with this word, and nothing else
# does: the backup skips exactly those paths (R2-NB2), so the prefix is a
# contract between read_leaf/_read_entry and unreadable_paths().
UNREADABLE_REASON = "unreadable"

# A session record is a few kB of JSON. Anything past this is not a record we
# are willing to load into memory, and refusing to read it also means refusing
# to judge it: the id is blocked for the whole run rather than overwritten.
MAX_RECORD_BYTES = 8 * 1024 * 1024

LOGGER = logging.getLogger("bridge")

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_uuid(name):
    return bool(UUID_RE.match(name))


# ---------------------------------------------------------------- discovery

def discover_leaves(root):
    """Return sorted absolute paths of every <account>/<org> leaf under root.

    Exactly two levels deep, both names canonical UUIDs, both real
    directories. Symlinks are skipped and never followed. A missing root is
    a clean empty result, not an error.
    """
    leaves = []
    for account in _child_dirs(root):
        for org in _child_dirs(account):
            leaves.append(org)
    return sorted(leaves)


def _child_dirs(parent):
    """Real, UUID-named subdirectories of parent (symlinks excluded)."""
    try:
        with os.scandir(parent) as entries:
            children = list(entries)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []
    out = []
    for entry in children:
        if not is_uuid(entry.name):
            continue
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        out.append(entry.path)
    return sorted(out)


# ------------------------------------------------------------- leaf reading

class FileEntry(object):
    """One on-disk file the bridge cares about.

    `name` is the BASENAME as the directory listing gave it. Session ids are
    case-folded for identity (APFS is case-insensitive, so two cases are one
    file there), but on a case-SENSITIVE filesystem the folded id is not the
    file name, and a path built from it points at nothing (R2-B2). Equality
    still compares bytes and mtime only: `name` says where this copy lives, not
    what it is.
    """

    __slots__ = ("content", "mtime_ns", "parsed", "name")

    def __init__(self, content, mtime_ns, parsed=None, name=None):
        self.content = content
        self.mtime_ns = mtime_ns
        self.parsed = parsed
        self.name = name

    @property
    def digest(self):
        return hashlib.sha256(self.content).hexdigest()

    def __eq__(self, other):
        return (isinstance(other, FileEntry)
                and self.content == other.content
                and self.mtime_ns == other.mtime_ns)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.content, self.mtime_ns))

    def __repr__(self):
        return "FileEntry(len=%d, mtime_ns=%d, parsed=%s, name=%r)" % (
            len(self.content), self.mtime_ns, self.parsed is not None, self.name)


class LeafState(object):
    """Everything the bridge knows about one <account>/<org> leaf.

    `blocked` maps a PATH we can neither read nor write (a directory or a
    symlink wearing a record/tombstone name, an unreadable or oversize file)
    to the reason. The planner emits no write and no delete for such a path,
    which is what makes the plan converge instead of proposing the same
    impossible write on every run.

    `blocked_ids` maps a SESSION ID that must not be written anywhere this run
    to the reason: we hold a copy of that id we could not read, so we cannot
    know it is not the newest one, and writing another leaf's copy over it
    would be data loss.

    `ignored` maps a path whose name only looks like ours (local_/deleted_
    with something that is not a canonical UUID) to the reason.

    `leaf_blocked` is the reason the LEAF DIRECTORY itself may not be written
    into (P2-1/P2-2: it is not readable, or not writable). Its records still
    take part in the merge as SOURCES — an unwritable leaf can still hold the
    newest copy — but the planner emits no write, no delete and no tombstone
    copy INTO it. Without this a 0555 leaf kept the plan non-empty for ever:
    a full backup on every run, shredding the backup ring.
    """

    __slots__ = ("path", "records", "tombstones", "blocked", "blocked_ids",
                 "ignored", "leaf_blocked")

    def __init__(self, path, records=None, tombstones=None, blocked=None,
                 blocked_ids=None, ignored=None, leaf_blocked=None):
        self.path = path
        self.records = records if records is not None else {}
        self.tombstones = tombstones if tombstones is not None else {}
        self.blocked = blocked if blocked is not None else {}
        self.blocked_ids = blocked_ids if blocked_ids is not None else {}
        self.ignored = ignored if ignored is not None else {}
        self.leaf_blocked = leaf_blocked

    def record_path(self, session_id):
        """Where this leaf's copy of local_<id>.json really is.

        The on-disk basename when this leaf holds the id, the canonical
        lower-cased name when it does not (nothing is there to disagree with).
        Every path the planner emits for this leaf goes through here or through
        tombstone_path, so a case-sensitive filesystem cannot be addressed with
        a name that is not on it (R2-B2).
        """
        return os.path.join(
            self.path, _on_disk_name(self.records, session_id, record_name))

    def tombstone_path(self, session_id):
        """Where this leaf's copy of deleted_<id> really is (see record_path)."""
        return os.path.join(
            self.path, _on_disk_name(self.tombstones, session_id, tombstone_name))

    def __repr__(self):
        return ("LeafState(%s, records=%d, tombstones=%d, blocked=%d, "
                "ignored=%d, leaf_blocked=%r)") % (
                    self.path, len(self.records), len(self.tombstones),
                    len(self.blocked), len(self.ignored), self.leaf_blocked)


def _on_disk_name(table, session_id, canonical):
    """The listed basename of this leaf's copy, else the canonical name."""
    entry = table.get(session_id)
    if entry is not None and entry.name:
        return entry.name
    return canonical(session_id)


def record_name(session_id):
    return RECORD_PREFIX + session_id + RECORD_SUFFIX


def tombstone_name(session_id):
    return TOMBSTONE_PREFIX + session_id


def record_id(filename):
    """Session id if filename is exactly local_<uuid>.json, else None.

    The id is lower-cased: APFS and HFS+ are case-insensitive, so
    local_<UUID>.json and local_<uuid>.json are ONE file there, and treating
    them as two session ids would make the plan fight itself.
    """
    if not filename.startswith(RECORD_PREFIX) or not filename.endswith(RECORD_SUFFIX):
        return None
    session_id = filename[len(RECORD_PREFIX):-len(RECORD_SUFFIX)]
    return session_id.lower() if is_uuid(session_id) else None


def tombstone_id(filename):
    """Session id if filename is exactly deleted_<uuid>, else None (lowered)."""
    if not filename.startswith(TOMBSTONE_PREFIX):
        return None
    session_id = filename[len(TOMBSTONE_PREFIX):]
    return session_id.lower() if is_uuid(session_id) else None


def looks_like_ours(filename):
    """True for a name that wears our prefixes but need not be a valid id."""
    return ((filename.startswith(RECORD_PREFIX)
             and filename.endswith(RECORD_SUFFIX))
            or filename.startswith(TOMBSTONE_PREFIX))


def read_leaf(leaf):
    """Read one leaf. Only local_<uuid>.json and deleted_<uuid> are read.

    An entry that cannot be read or written is recorded in `blocked` rather
    than silently skipped (see LeafState); a name that only looks like ours
    lands in `ignored`. Records that do not parse as a JSON object are read
    normally and keep parsed=None.
    """
    state = LeafState(leaf)
    try:
        with os.scandir(leaf) as entries:
            children = list(entries)
    except PermissionError as error:
        # P2-2: this used to read as "records=0", i.e. "in sync", with no
        # warning at all — and every id then looked ABSENT here, so the planner
        # proposed writes into a directory it cannot even list, on every run.
        state.leaf_blocked = "%s directory: %s" % (UNREADABLE_REASON, error)
        LOGGER.warning("cannot read the leaf %s: %s; nothing will be read from "
                       "or written into it", leaf, error)
        return state
    except (FileNotFoundError, NotADirectoryError):
        return state
    # P2-1: creating the temp file needs write AND execute on the folder. A
    # 0555 leaf used to fail every single write, on every run, so the plan
    # never converged: a full backup each time (ten runs and the ring holds
    # nothing else) plus a temp file churned in the folder the app reads, which
    # re-fires WatchPaths. Its records are still read: it may hold the newest.
    if not os.access(leaf, os.W_OK | os.X_OK):
        state.leaf_blocked = ("not writable: no write/execute permission on "
                              "the folder")
        LOGGER.warning("cannot write into the leaf %s: %s; its records still "
                       "take part as sources, but nothing will be written "
                       "into it", leaf, state.leaf_blocked)
    return _read_entries(state, sorted(children, key=lambda e: e.name))


AMBIGUOUS_CASE = ("two names in this folder differ only in case for one "
                  "session id: which one the app reads is unknowable")


def _read_entries(state, entries):
    """Classify one leaf's directory entries into the LeafState.

    Split out from read_leaf so the case-sensitive-filesystem behaviour can be
    exercised on a case-insensitive one (R2-B2): the entries are the only input.
    """
    ambiguous = {True: set(), False: set()}
    # id -> the path we accepted it from, per kind, so an ambiguous pair blocks
    # the path we actually read and not one rebuilt from a name.
    accepted = {True: {}, False: {}}
    for entry in entries:
        session_id = record_id(entry.name)
        is_record = session_id is not None
        if not is_record:
            session_id = tombstone_id(entry.name)
        if session_id is None:
            if looks_like_ours(entry.name):
                state.ignored[entry.path] = "name is not a canonical UUID"
            continue
        if entry.is_symlink():
            state.blocked[entry.path] = "symlink: never followed, never written through"
            continue
        if not entry.is_file(follow_symlinks=False):
            state.blocked[entry.path] = _not_a_regular_file_reason(entry.path)
            continue
        table = state.records if is_record else state.tombstones
        # R2-B2: two names for one id, only possible on a case-sensitive
        # filesystem. We cannot tell which one the app reads, so we touch
        # NEITHER: both are blocked, which also keeps the plan convergent.
        if session_id in ambiguous[is_record]:
            state.blocked[entry.path] = AMBIGUOUS_CASE
            LOGGER.warning("%s: %s", AMBIGUOUS_CASE, entry.path)
            continue
        if session_id in table:
            ambiguous[is_record].add(session_id)
            for path in (accepted[is_record][session_id], entry.path):
                state.blocked[path] = AMBIGUOUS_CASE
                LOGGER.warning("%s: %s", AMBIGUOUS_CASE, path)
            del table[session_id]
            continue
        item, reason = _read_entry(entry.path, parse=is_record, name=entry.name)
        if item is None:
            # We hold a copy of this id we cannot read: block the id as well as
            # the path, so no other leaf's copy is written over it.
            state.blocked[entry.path] = reason
            state.blocked_ids[session_id] = reason
            continue
        table[session_id] = item
        accepted[is_record][session_id] = entry.path
    return state


def _not_a_regular_file_reason(path):
    """Why a non-regular entry is blocked — saying so when it is UNREADABLE.

    Measured: a DIRECTORY wearing a record's name with no read permission is
    blocked either way, but the backup could not copy it, so the run failed and
    applied nothing on EVERY run — the permanent no-op R2-NB2 exists to kill.
    What decides whether the backup can step around a blocked path is whether it
    can be read, not whether it is a file or a directory: a readable one is
    copied normally, which keeps the backup complete.
    """
    unreadable = not os.access(path, os.R_OK) or (
        os.path.isdir(path) and not os.access(path, os.X_OK))
    if unreadable:
        return "%s: not a regular file, and it cannot be read" % UNREADABLE_REASON
    return "not a regular file"


def _read_entry(path, parse=True, name=None):
    """(FileEntry, None) on success, (None, reason) when it must be blocked."""
    try:
        st = os.lstat(path)
        if st.st_size > MAX_RECORD_BYTES:
            reason = "larger than the %d byte cap (%d bytes)" % (
                MAX_RECORD_BYTES, st.st_size)
            LOGGER.warning("not reading %s: %s", path, reason)
            return None, reason
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError as error:
        LOGGER.warning("cannot read %s: %s", path, error)
        return None, "%s: %s" % (UNREADABLE_REASON, error)
    if not parse:
        return FileEntry(content, st.st_mtime_ns, None, name), None
    return _load_from_bytes(content, st.st_mtime_ns, name=name), None


def _load_from_bytes(content, mtime_ns, name=None):
    """Build a FileEntry, parsing content as a JSON object if it is one."""
    try:
        candidate = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        candidate = None
    parsed = candidate if isinstance(candidate, dict) else None
    return FileEntry(content, mtime_ns, parsed, name)


class RunResult(object):
    """Minimal stand-in for subprocess.CompletedProcess."""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ------------------------------------------------------------------ planning

class _Absent(object):
    """The sentinel for 'this destination did not exist when we planned'."""

    __slots__ = ()

    def __repr__(self):
        return "ABSENT"


ABSENT = _Absent()

# `expected` is the destination's state when the plan was made: ABSENT, or
# (mtime_ns, sha256 hexdigest). It has NO default on purpose — every writer
# must say what it expects to overwrite, so a hand-built Write cannot silently
# take the "it changed under us" branch.
Write = collections.namedtuple("Write", "dest content mtime_ns source kind expected")
Delete = collections.namedtuple("Delete", "path session_id source")

NO_ACTIVITY = float("-inf")

# The record's own copy of its session id. The filename is authoritative (it is
# what the app reads the folder by), so a disagreement is reported, not acted
# on. Only this one key is checked: `cliSessionId` is the CLI transcript id and
# may legitimately differ from the record's session id.
ID_FIELD = "id"


class Plan(collections.namedtuple(
        "Plan",
        "leaves writes tombstone_copies deletes records_seen parse_failures "
        "blocked blocked_leaves ignored id_mismatches")):
    """An immutable description of what a sync would do. Compares by value."""

    __slots__ = ()

    def is_empty(self):
        return not (self.writes or self.tombstone_copies or self.deletes)

    def counts(self):
        return {
            "leaves": len(self.leaves),
            "records_seen": self.records_seen,
            "copies_written": len(self.writes),
            "records_deleted": len(self.deletes),
            "tombstones_copied": len(self.tombstone_copies),
            "parse_failures": len(self.parse_failures),
            "blocked": len(self.blocked),
            "blocked_leaves": len(self.blocked_leaves),
            "ignored": len(self.ignored),
            "id_mismatches": len(self.id_mismatches),
        }


def _activity(parsed):
    """lastActivityAt as a comparable number; absent or junk sorts lowest."""
    value = parsed.get("lastActivityAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return NO_ACTIVITY
    return float(value)


def plan_sync(leaf_states):
    """Pure merge: leaf states in, a Plan out. Touches no filesystem.

    Rules (docs/design.md):
      * union by session id; winner = newest mtime_ns, then larger
        lastActivityAt, then larger sha256 of the bytes (determinism only);
      * records that do not parse as a JSON object are never candidates and
        are never deleted, but are counted and may be overwritten by a
        winner from another leaf;
      * a tombstone anywhere deletes local_<id>.json in every leaf, is
        copied byte-for-byte into every leaf that lacks it, and suppresses
        every write of that id;
      * a blocked path gets neither a write nor a delete, and a blocked id
        gets no record write in any leaf, so the plan converges instead of
        proposing an impossible action on every run;
      * a blocked LEAF (unreadable or unwritable directory) is written into by
        nothing at all, while the records it did yield still take part as
        sources.
    """
    states = sorted(leaf_states, key=lambda state: state.path)
    leaves = tuple(state.path for state in states)
    # The leaves that may actually be written into. Everything else still reads.
    writable = [state for state in states if state.leaf_blocked is None]

    blocked = {}
    blocked_leaves = {}
    ignored = {}
    blocked_ids = {}
    for state in states:
        blocked.update(state.blocked)
        ignored.update(state.ignored)
        blocked_ids.update(state.blocked_ids)
        if state.leaf_blocked is not None:
            blocked_leaves[state.path] = state.leaf_blocked

    parse_failures = []
    id_mismatches = []
    for state in states:
        for session_id, item in sorted(state.records.items()):
            path = state.record_path(session_id)
            if item.parsed is None:
                parse_failures.append(path)
                continue
            claimed = item.parsed.get(ID_FIELD)
            if isinstance(claimed, str) and claimed.lower() != session_id:
                id_mismatches.append(path)

    tombstoned = sorted({sid for state in states for sid in state.tombstones})
    deletes = []
    tombstone_copies = []
    for session_id in tombstoned:
        holders = [(state, state.tombstones[session_id])
                   for state in states if session_id in state.tombstones]
        source_state, winner = max(
            holders, key=lambda pair: (pair[1].mtime_ns, pair[1].digest))
        source = source_state.tombstone_path(session_id)
        for state in writable:
            doomed = state.record_path(session_id)
            if session_id in state.records and doomed not in blocked:
                deletes.append(Delete(doomed, session_id, source))
            copy_dest = state.tombstone_path(session_id)
            if session_id not in state.tombstones and copy_dest not in blocked:
                tombstone_copies.append(Write(
                    copy_dest, winner.content, winner.mtime_ns, source,
                    "tombstone", ABSENT))

    writes = []
    records_seen = 0
    live_ids = {sid for state in states for sid in state.records} - set(tombstoned)
    for session_id in sorted(live_ids):
        candidates = [(state, state.records[session_id]) for state in states
                      if session_id in state.records
                      and state.records[session_id].parsed is not None]
        if not candidates:
            continue
        records_seen += 1
        if session_id in blocked_ids:
            continue
        source_state, winner = max(candidates, key=lambda pair: (
            pair[1].mtime_ns, _activity(pair[1].parsed), pair[1].digest))
        source = source_state.record_path(session_id)
        for state in writable:
            dest = state.record_path(session_id)
            if dest == source or dest in blocked:
                continue
            current = state.records.get(session_id)
            if (current is not None
                    and current.content == winner.content
                    and current.mtime_ns == winner.mtime_ns):
                continue
            expected = (ABSENT if current is None
                        else (current.mtime_ns, current.digest))
            writes.append(Write(dest, winner.content, winner.mtime_ns,
                                source, "record", expected))

    return Plan(
        leaves=leaves,
        writes=tuple(sorted(writes, key=lambda w: w.dest)),
        tombstone_copies=tuple(sorted(tombstone_copies, key=lambda w: w.dest)),
        deletes=tuple(sorted(deletes, key=lambda d: d.path)),
        records_seen=records_seen,
        parse_failures=tuple(sorted(parse_failures)),
        blocked=tuple(sorted(blocked.items())),
        blocked_leaves=tuple(sorted(blocked_leaves.items())),
        ignored=tuple(sorted(ignored.items())),
        id_mismatches=tuple(sorted(id_mismatches)),
    )


# ------------------------------------------------------------------- applying

TEMP_PREFIX = ".bridge-tmp-"
BACKUPS_DIRNAME = "backups"
LOCK_FILENAME = "bridge.lock"
BACKUPS_KEPT = 10

SyncResult = collections.namedtuple(
    "SyncResult", "plan backup applied backup_skipped tasks dirs")

# What take_backup made: the directory, and how many paths it deliberately left
# out because discovery had already reported them as unreadable (R2-NB2).
Backup = collections.namedtuple("Backup", "path skipped")


def empty_applied(errors=0):
    """The applied-counts dict with nothing done (and optionally one failure)."""
    return {"copies_written": 0, "tombstones_copied": 0, "records_deleted": 0,
            "tasks_written": 0, "dir_files_written": 0, "dir_files_deleted": 0,
            "app_files_written": 0, "skipped": 0, "errors": errors}


def default_state_dir():
    return os.path.join(os.path.expanduser("~"), ".claude-code-multi-account-bridge")


def gather(root):
    """Read every leaf under root into LeafState objects."""
    return [read_leaf(leaf) for leaf in discover_leaves(root)]


def apply_plan(plan):
    """Perform the plan's IO. Returns the counts actually applied.

    Every write is temp-file + atomic rename inside the destination leaf,
    followed by os.utime to the winner's mtime. A destination that is a
    symlink is refused, never written through. A delete is refused unless
    the basename is exactly local_<uuid>.json.

    The stale-temp-file sweep is NOT done here: it belongs to the whole
    --execute run, including one with an empty plan (see sync_once).
    """
    applied = empty_applied()
    for write in plan.tombstone_copies:
        _apply_write(write, applied, "tombstones_copied")
    for delete in plan.deletes:
        _apply_delete(delete, applied)
    for write in plan.writes:
        _apply_write(write, applied, "copies_written")
    return applied


def sweep_stale_temp_files(leaves):
    """Remove leftover `.bridge-tmp-*` files from EVERY leaf that was read.

    A run killed mid-write leaves one behind, in the very folder the desktop app
    reads, and nothing else would ever clean it up.

    Called ONLY from sync_once's --execute path, which runs under the lock —
    that is what makes removing a file another process created safe — and there
    it runs BEFORE the backup and whether or not there is anything to apply
    (P2-3: living inside apply meant a converged tree, which is the normal
    state, never swept at all). Do not call it from `status` or the dry-run
    path: those hold no lock, so a temp file there may belong to a live run.
    Only regular files whose name starts with the prefix are touched: never a
    directory, never a symlink.
    """
    removed = []
    for leaf in leaves:
        try:
            with os.scandir(leaf) as entries:
                children = list(entries)
        except OSError:
            continue
        for entry in children:
            if not entry.name.startswith(TEMP_PREFIX):
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            LOGGER.warning("removing a stale temp file left by an interrupted "
                           "run: %s", entry.path)
            if _unlink_quietly(entry.path):
                removed.append(entry.path)
    return removed


def _dest_pre_state(path):
    """The destination's state RIGHT NOW, in the shape of Write.expected.

    ABSENT when it does not exist, (mtime_ns, sha256) for a readable regular
    file within the size cap, and None for anything else (unreadable, a
    directory, a symlink, oversize). None never compares equal to an
    `expected`, so an unexpected destination is always refused rather than
    guessed at.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ABSENT
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RECORD_BYTES:
        return None
    try:
        with open(path, "rb") as handle:
            content = handle.read()
    except OSError:
        return None
    return (st.st_mtime_ns, hashlib.sha256(content).hexdigest())


def _describe_state(state):
    if state is ABSENT:
        return "absent"
    if state is None:
        return "unreadable, oversize or not a regular file"
    return "mtime_ns=%d sha256=%s" % (state[0], state[1][:12])


def _apply_write(write, applied, counter):
    parent = os.path.dirname(write.dest)
    if os.path.islink(parent) or not os.path.isdir(parent):
        LOGGER.warning("skipping %s: destination directory is not a real directory",
                       write.dest)
        applied["skipped"] += 1
        return
    if os.path.islink(write.dest):
        LOGGER.warning("skipping %s: destination is a symlink, refusing to write "
                       "through it", write.dest)
        applied["skipped"] += 1
        return
    # B1: the app may have written a NEWER copy between the plan and now.
    # Overwriting it would lose it AND stamp the older winner's mtime, hiding
    # the loss from every later run. Deletes are deliberately NOT re-checked: a
    # tombstone is final and session ids are never reused.
    current = _dest_pre_state(write.dest)
    if current != write.expected:
        LOGGER.warning("skipping %s: it changed since the plan was made "
                       "(expected %s, found %s); the copy on disk is kept",
                       write.dest, _describe_state(write.expected),
                       _describe_state(current))
        applied["skipped"] += 1
        return
    temp = os.path.join(parent, TEMP_PREFIX + "%d-%s" % (
        os.getpid(), os.path.basename(write.dest)))
    try:
        with open(temp, "wb") as handle:
            handle.write(write.content)
            handle.flush()
            os.fsync(handle.fileno())
        # N11: an overwrite must not change the destination's permissions. The
        # temp file was created with the process umask, which is usually wider.
        if write.expected is not ABSENT:
            _copy_mode(write.dest, temp)
        os.replace(temp, write.dest)
    except OSError as error:
        LOGGER.error("failed to write %s: %s", write.dest, error)
        applied["errors"] += 1
        _unlink_quietly(temp)
        return
    # The bytes are in place from here on: the copy counts even if the mtime
    # or the directory sync do not work out.
    applied[counter] += 1
    LOGGER.debug("wrote %s (%s) from %s", write.dest, write.kind, write.source)
    try:
        os.utime(write.dest, ns=(write.mtime_ns, write.mtime_ns))
    except OSError as error:
        # N2: the copy landed but kept the rename's "now" mtime, which makes it
        # look like the NEWEST copy of that id and win the next merge. Loud, and
        # non-zero exit, precisely because the bytes did land.
        LOGGER.error("wrote %s but could not restore its mtime (%s); it will "
                     "look newer than every other copy until this is fixed",
                     write.dest, error)
        applied["errors"] += 1
    _fsync_directory(parent)


def _copy_mode(source, target):
    """Give target source's permission bits, if source still exists."""
    try:
        os.chmod(target, stat.S_IMODE(os.lstat(source).st_mode))
    except OSError:
        pass


def _fsync_directory(path):
    """Best effort: make the rename itself durable. Never fails a write."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _apply_delete(delete, applied):
    name = os.path.basename(delete.path)
    if record_id(name) is None:
        LOGGER.error("refusing to delete %s: not a local_<uuid>.json record",
                     delete.path)
        applied["skipped"] += 1
        return
    if os.path.islink(delete.path):
        LOGGER.warning("skipping %s: it is a symlink", delete.path)
        applied["skipped"] += 1
        return
    try:
        os.unlink(delete.path)
    except FileNotFoundError:
        applied["skipped"] += 1
        return
    except OSError as error:
        LOGGER.error("failed to delete %s: %s", delete.path, error)
        applied["errors"] += 1
        return
    LOGGER.debug("deleted %s (tombstoned by %s)", delete.path, delete.source)
    applied["records_deleted"] += 1


def _unlink_quietly(path):
    """Delete a path, reporting success, never raising."""
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def unreadable_paths(plan):
    """The paths discovery could not read: blocked entries and blocked leaves.

    These are the only paths a backup is allowed to leave out (R2-NB2). The
    reasons are written by _read_entry and read_leaf and all start with
    UNREADABLE_REASON — an oversize record, a directory wearing a record's name
    or a symlink is blocked for other reasons and copies perfectly well.
    """
    return tuple(sorted(
        path for path, reason in tuple(plan.blocked) + tuple(plan.blocked_leaves)
        if reason.startswith(UNREADABLE_REASON)))


def _skip_paths_ignore(skip_paths, skipped):
    """A shutil.copytree `ignore` callback that omits exactly `skip_paths`."""
    wanted = set(skip_paths)

    def ignore(directory, names):
        omitted = set()
        for name in names:
            path = os.path.join(directory, name)
            if path in wanted:
                LOGGER.warning("backup: leaving out %s, it cannot be read; the "
                               "backup will not contain it", path)
                skipped.append(path)
                omitted.add(name)
        return omitted

    return ignore


def take_backup(root, state_dir, now=None, skip_paths=()):
    """Copy the whole sessions tree aside, then keep only the newest N.

    Returns a Backup (directory + how many paths were left out), or None when
    the copy failed. A single unreadable file anywhere in the tree makes
    shutil.copytree raise shutil.Error (an OSError subclass) with the tree half
    copied; that must not become a traceback, and it must not let the run apply
    anything, because the backup is the only way back from the writes that would
    follow.

    `skip_paths` are the paths discovery already reported as unreadable
    (R2-NB2). Without them one unreadable record failed the backup, and so the
    whole run, on EVERY run — a permanent no-op, which is the opposite of the
    "report it and step around it" rule those paths exist for. Anything else
    that cannot be copied still aborts.
    """
    backups_root = os.path.join(state_dir, BACKUPS_DIRNAME)
    try:
        os.makedirs(backups_root, exist_ok=True)
    except OSError as error:
        LOGGER.error("cannot create the backup directory %s: %s",
                     backups_root, error)
        return None
    # timezone-aware UTC: datetime.utcnow() is deprecated from 3.12 on, and
    # the fixed-width stamp makes plain name sorting chronological.
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime(
        "%Y%m%dT%H%M%S%fZ")
    destination = os.path.join(backups_root, stamp)
    skipped = []
    try:
        try:
            shutil.copytree(root, os.path.join(destination, os.path.basename(root)),
                            symlinks=True,
                            ignore=_skip_paths_ignore(skip_paths, skipped))
        except OSError as error:
            LOGGER.error("backup of %s failed, nothing will be applied: %s",
                         root, error)
            # Remove the half-written directory BEFORE pruning: its name sorts
            # newest, so a prune that ran first would keep the corpse and
            # delete a good backup to make room for it.
            _force_rmtree(destination)
            return None
    finally:
        prune_backups(backups_root)
    LOGGER.info("backup written to %s (%d path(s) left out)",
                destination, len(skipped))
    return Backup(destination, len(skipped))


def prune_backups(backups_root, keep=BACKUPS_KEPT):
    """Delete all but the newest `keep` backups (names sort chronologically).

    R2-NB1: a SYMLINK named like a timestamp is not a backup. os.path.isdir
    follows links, so one used to be counted as a backup and then handed to
    _force_rmtree, whose chmod walk went straight through it and changed the
    modes of a tree outside the state directory entirely. Links are skipped and
    reported, and they do not take up a slot in the ring either.
    """
    try:
        names = []
        for name in sorted(os.listdir(backups_root)):
            path = os.path.join(backups_root, name)
            if os.path.islink(path):
                LOGGER.warning("not touching %s: it is a symlink, not a backup "
                               "this tool wrote", path)
                continue
            if os.path.isdir(path):
                names.append(name)
    except OSError:
        return []
    doomed = names[:-keep] if keep > 0 else names
    removed = []
    for name in doomed:
        if _force_rmtree(os.path.join(backups_root, name)):
            removed.append(name)
            LOGGER.debug("pruned old backup %s", name)
    return removed


def _force_rmtree(path):
    """Delete a tree we own, making it writable first. True when it is gone.

    A directory inside a backup whose write bit is cleared (555) defeats a
    plain rmtree, and `ignore_errors=True` would leave that backup in place for
    good — so the ring would grow past its cap with nothing able to trim it.
    No onerror/onexc callback is used on purpose: `onerror` is deprecated from
    3.12 (and this suite turns DeprecationWarning into an error) while `onexc`
    does not exist before it.

    Never called on a symlink (R2-NB1): both the chmod walk and the rmtree
    would act on whatever it points at, which may be nowhere near the state
    directory. Refused loudly instead.
    """
    if os.path.islink(path):
        LOGGER.error("refusing to remove %s: it is a symlink, so it is not a "
                     "tree this tool created", path)
        return False
    _make_writable(path)
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    except OSError as error:
        LOGGER.error("could not remove %s: %s", path, error)
        return False
    return True


def _make_writable(tree):
    """Owner rwx on every directory, rw on every file. Best effort.

    Only ever called on a path that is about to be deleted — and never through
    a symlink: os.walk would descend into the link's target and the chmods would
    land outside the state directory (R2-NB1).
    """
    if os.path.islink(tree):
        return
    _chmod_quietly(tree, 0o700)
    for current, dirnames, filenames in os.walk(tree):
        for name in dirnames:
            _chmod_quietly(os.path.join(current, name), 0o700)
        for name in filenames:
            _chmod_quietly(os.path.join(current, name), 0o600)


def _chmod_quietly(path, mode):
    # os.chmod follows symlinks, so a link inside a backup would have us
    # changing the mode of whatever it points at, outside the tree.
    if os.path.islink(path):
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def log_plan_findings(plan):
    """Report everything the merge could not act on. Never silent.

    `status` calls this too, so a read-only look at the tree surfaces exactly
    the same warnings a sync would.
    """
    for path in plan.parse_failures:
        LOGGER.warning("unparsable record: never chosen as a winner, and only "
                       "deleted by a tombstone: %s", path)
    for path, reason in plan.blocked:
        LOGGER.warning("blocked, nothing will be written to or deleted at this "
                       "path (%s): %s", reason, path)
    for path, reason in plan.blocked_leaves:
        LOGGER.warning("blocked leaf, nothing will be written into this folder "
                       "(%s): %s", reason, path)
    for path in plan.id_mismatches:
        LOGGER.warning("record's %r disagrees with its filename; syncing by "
                       "filename: %s", ID_FIELD, path)
    for path, reason in plan.ignored:
        LOGGER.debug("ignored (%s): %s", reason, path)


# ----------------------------------------------------------- scheduled tasks
#
# <leaf>/scheduled-tasks.json is the list the desktop app's task scheduler runs
# from: {"scheduledTasks": [{"id", "filePath", "cronExpression", "enabled",
# "lastRunAt", ...}], "recordedSkips": {<task id>: [...]}, ...}. The prompt each
# task points at (filePath, ~/.claude/scheduled-tasks/<id>/SKILL.md) is already
# shared across accounts; only the list is per account, so a task created on one
# account never runs, or even shows, while signed into the other.
#
# The app holds that list in memory once an account is loaded and rewrites the
# whole file from memory, so a file written under a running app is overwritten
# with the list it loaded — and a missing task then reads like a delete. The
# signed-in account's leaves are therefore only written while the app is not
# running; every other leaf is written whenever it drifts, so it is current the
# next time that account loads.

SCHEDULED_TASKS_FILENAME = "scheduled-tasks.json"
TASKS_KEY = "scheduledTasks"
SKIPS_KEY = "recordedSkips"
TASK_ID_FIELD = "id"
# Run bookkeeping (ISO-8601 UTC strings, so they order as strings). The newest
# value across copies always wins, so a run on one account is not "missed"
# again on the other.
TASK_RUN_FIELDS = ("lastRunAt", "lastScheduledFor")
# Per leaf, the task ids it held after the last --execute run: a task that was
# there and is gone now was deleted in the app, and is deleted everywhere.
TASKS_BASE_FILENAME = "scheduled-tasks-base.json"
APP_CONFIG_FILENAME = "config.json"
ACTIVE_ACCOUNT_FIELD = "lastKnownAccountUuid"
APP_PROCESS_NAME = "Claude"

TasksPlan = collections.namedtuple(
    "TasksPlan", "files writes deleted held_back tasks")


def tasks_path(leaf):
    return os.path.join(leaf, SCHEDULED_TASKS_FILENAME)


def read_scheduled_tasks(leaf):
    """The leaf's scheduled-tasks.json as a parsed FileEntry, or None.

    None for anything that is not a regular file holding the shape described
    above: a file this tool does not understand is neither a source nor a
    destination. The app creates the file when an account loads, so a leaf
    without one is never given one either.
    """
    path = tasks_path(leaf)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
    except OSError:
        return None
    entry, _ = _read_entry(path, name=SCHEDULED_TASKS_FILENAME)
    if entry is None or entry.parsed is None:
        return None
    tasks = entry.parsed.get(TASKS_KEY)
    if not isinstance(tasks, list) or not all(
            isinstance(task, dict) and isinstance(task.get(TASK_ID_FIELD), str)
            for task in tasks):
        LOGGER.debug("not a scheduled-tasks list this tool understands: %s", path)
        return None
    return entry


def _task_ids(entry):
    return {task[TASK_ID_FIELD] for task in entry.parsed[TASKS_KEY]}


def _newer_run_fields(merged, task):
    for field in TASK_RUN_FIELDS:
        value = task.get(field)
        if isinstance(value, str) and (not isinstance(merged.get(field), str)
                                       or value > merged[field]):
            merged[field] = value


def plan_scheduled_tasks(files, base, writable, held_back=()):
    """Pure merge of every leaf's scheduled-tasks.json. Touches no filesystem.

    files: {leaf: FileEntry} as read_scheduled_tasks returned them; base: {leaf:
    [task ids]} from the last --execute run; writable: leaves that may be written
    into; held_back: leaves of the account the running app has loaded.

      * a task id in a leaf's base and missing from that leaf now was deleted
        there, and is dropped everywhere;
      * every other task id is kept; its body comes from the leaf whose file is
        newest (then the larger path, for determinism), and its run fields are
        the newest across all copies;
      * recordedSkips and every other top-level key come from the newest file,
        with skip lists for tasks only another leaf has added in.
    """
    ordered = sorted(files.items(), key=lambda pair: (pair[1].mtime_ns, pair[0]),
                     reverse=True)
    deleted = set()
    for leaf, entry in files.items():
        if leaf in base:
            deleted |= set(base[leaf]) - _task_ids(entry)
    tasks = []
    index = {}
    skips = {}
    for _, entry in ordered:
        for task in entry.parsed[TASKS_KEY]:
            task_id = task[TASK_ID_FIELD]
            if task_id in deleted:
                continue
            if task_id in index:
                _newer_run_fields(tasks[index[task_id]], task)
            else:
                index[task_id] = len(tasks)
                tasks.append(dict(task))
        leaf_skips = entry.parsed.get(SKIPS_KEY)
        if isinstance(leaf_skips, dict):
            for task_id, value in leaf_skips.items():
                if task_id not in deleted and task_id not in skips:
                    skips[task_id] = value
    if not ordered:
        return TasksPlan(files={}, writes=(), deleted=(), held_back=(), tasks=())
    newest_leaf, newest = ordered[0]
    merged = dict(newest.parsed)
    merged[TASKS_KEY] = tasks
    if SKIPS_KEY in merged or skips:
        merged[SKIPS_KEY] = skips
    content = (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    writes = []
    held = []
    for leaf in sorted(files):
        entry = files[leaf]
        if leaf not in writable or entry.parsed == merged:
            continue
        if leaf in held_back:
            held.append(leaf)
            continue
        writes.append(Write(tasks_path(leaf), content, newest.mtime_ns,
                            tasks_path(newest_leaf), "scheduled-tasks",
                            (entry.mtime_ns, entry.digest)))
    return TasksPlan(files=dict(files), writes=tuple(writes),
                     deleted=tuple(sorted(deleted)), held_back=tuple(held),
                     tasks=tuple(task[TASK_ID_FIELD] for task in tasks))


def load_state_json(state_dir, name):
    """A JSON object this tool saved in the state directory; {} when absent."""
    try:
        with open(os.path.join(state_dir, name), "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state_json(state_dir, name, data):
    path = os.path.join(state_dir, name)
    if not data and not os.path.exists(path):
        return
    temp = path + ".tmp"
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(temp, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(temp, path)
    except OSError as error:
        LOGGER.error("could not save %s: %s", path, error)
        _unlink_quietly(temp)


def load_tasks_base(state_dir):
    """{leaf: [task ids]} from the last --execute run; {} when there is none."""
    base = load_state_json(state_dir, TASKS_BASE_FILENAME)
    return {leaf: ids for leaf, ids in base.items() if isinstance(ids, list)}


def save_tasks_base(state_dir, leaves):
    """Record what these leaves hold NOW, re-read after the writes landed.

    Entries for leaves of another root (the Cowork tree shares this file) are
    kept as they are.
    """
    base = load_tasks_base(state_dir)
    for leaf in leaves:
        entry = read_scheduled_tasks(leaf)
        if entry is not None:
            base[leaf] = sorted(_task_ids(entry))
        else:
            base.pop(leaf, None)
    save_state_json(state_dir, TASKS_BASE_FILENAME, base)


def active_account(user_data):
    """The account the desktop app last loaded, from its config.json, or None."""
    try:
        with open(os.path.join(user_data, APP_CONFIG_FILENAME), "rb") as handle:
            config = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    account = config.get(ACTIVE_ACCOUNT_FIELD) if isinstance(config, dict) else None
    return account.lower() if isinstance(account, str) and is_uuid(account) else None


def app_is_running(platform=None):
    """Whether the desktop app is running. Unknown counts as running: holding a
    write back costs a delay, writing under a running app can cost data."""
    platform = sys.platform if platform is None else platform
    if platform != "darwin":
        return True
    # `ps`, not `pgrep -x`: measured, pgrep finds nothing from inside a
    # sandboxed shell while ps still lists the app.
    try:
        listing = subprocess.run(["ps", "-axo", "comm="], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, check=True)
    except (OSError, subprocess.CalledProcessError):
        return True
    return any(os.path.basename(line.strip()) == APP_PROCESS_NAME
               for line in listing.stdout.decode("utf-8", "replace").splitlines())


def held_back_leaves(root, leaves, app_running=None):
    """Leaves of the account the running app has loaded: written only once it quits."""
    account = active_account(os.path.dirname(root))
    if account is None:
        return ()
    if app_running is None:
        app_running = app_is_running()
    if not app_running:
        return ()
    return tuple(leaf for leaf in leaves
                 if os.path.basename(os.path.dirname(leaf)).lower() == account)


def gather_scheduled_tasks(plan, state_dir, held_back):
    """Read, then plan, the scheduled-tasks merge for the leaves of `plan`."""
    files = {}
    for leaf in plan.leaves:
        entry = read_scheduled_tasks(leaf)
        if entry is not None:
            files[leaf] = entry
    blocked = {path for path, _ in plan.blocked_leaves}
    writable = [leaf for leaf in plan.leaves if leaf not in blocked]
    return plan_scheduled_tasks(files, load_tasks_base(state_dir), writable,
                                held_back)


def log_tasks_findings(tasks_plan):
    for task_id in tasks_plan.deleted:
        LOGGER.info("scheduled task deleted on one account, removing it "
                    "everywhere: %s", task_id)
    for leaf in tasks_plan.held_back:
        LOGGER.debug("scheduled tasks held back while the app has this account "
                     "loaded; written once it quits: %s", tasks_path(leaf))


def format_tasks_counts(tasks_plan):
    return "tasks=%d files=%d writes=%d deleted=%d held_back=%d" % (
        len(tasks_plan.tasks), len(tasks_plan.files), len(tasks_plan.writes),
        len(tasks_plan.deleted), len(tasks_plan.held_back))


# ---------------------------------------------------------------- leaf folders
#
# Folders inside a leaf whose files are per-account state the app keeps beside
# the records: waiting-input/ holds messages queued for a session but not yet
# sent (one local_<id>.json per session), memory/ is Cowork's memory. Every
# regular file in them is mirrored by relative path: newest mtime wins, and a
# file a leaf held after the last --execute run and no longer holds was
# consumed or deleted there, so it is removed everywhere. Without that, a
# queued message the app already sent would be restored on the other account
# and sent again. The loaded account's leaves wait for the app to quit, like
# scheduled-tasks.json: the app owns those folders while it runs.

LEAF_DIRS = ("waiting-input", "memory")
DIRS_BASE_FILENAME = "leaf-dirs-base.json"

DirsPlan = collections.namedtuple(
    "DirsPlan", "files writes deletes deleted held_back")


def _dir_key(leaf, sub):
    return leaf + "\n" + sub


def read_leaf_dir(leaf, sub):
    """{relative path: FileEntry} for every regular file under leaf/sub.

    None when leaf/sub is not a real directory (absent, or a symlink): such a
    folder is a destination only if some other leaf has files for it. Symlinks
    and this tool's own temp files are never read; an unreadable file is left
    out and logged, and its path is returned in the second slot so nothing is
    planned for it anywhere.
    """
    top = os.path.join(leaf, sub)
    if os.path.islink(top) or not os.path.isdir(top):
        return None, ()
    files = {}
    blocked = []
    pending = [""]
    while pending:
        rel_dir = pending.pop()
        try:
            with os.scandir(os.path.join(top, rel_dir)) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as error:
            LOGGER.warning("cannot list %s: %s", os.path.join(top, rel_dir), error)
            blocked.append(rel_dir)
            continue
        for entry in children:
            rel = os.path.join(rel_dir, entry.name) if rel_dir else entry.name
            if entry.name.startswith(TEMP_PREFIX) or entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(rel)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            item, _ = _read_entry(entry.path, parse=False, name=entry.name)
            if item is None:
                blocked.append(rel)
            else:
                files[rel] = item
    return files, tuple(blocked)


def plan_leaf_dirs(dirs, base, writable, held_back=()):
    """Pure merge of the LEAF_DIRS folders. Touches no filesystem.

    dirs: {(leaf, sub): ({rel: FileEntry} or None, blocked rels)}; base:
    {_dir_key(leaf, sub): [rels]} from the last --execute run.
    """
    writes = []
    deletes = []
    held = set()
    deleted_all = []
    file_count = 0
    for sub in LEAF_DIRS:
        present = {leaf: files for (leaf, s), (files, _) in dirs.items()
                   if s == sub and files is not None}
        blocked = {rel for (leaf, s), (_, rels) in dirs.items() if s == sub
                   for rel in rels}
        deleted = set()
        for leaf, files in present.items():
            known = base.get(_dir_key(leaf, sub))
            if isinstance(known, list):
                deleted |= set(known) - set(files)
        deleted -= blocked
        rels = {rel for files in present.values() for rel in files} - deleted - blocked
        if not rels and not deleted:
            continue
        file_count += len(rels)
        deleted_all.extend(os.path.join(sub, rel) for rel in sorted(deleted))
        for rel in sorted(rels):
            holders = [(leaf, files[rel]) for leaf, files in present.items()
                       if rel in files]
            source_leaf, winner = max(holders, key=lambda pair: (
                pair[1].mtime_ns, pair[1].digest, pair[0]))
            for leaf in writable:
                current = present.get(leaf, {}).get(rel)
                if (current is not None and current.content == winner.content
                        and current.mtime_ns == winner.mtime_ns):
                    continue
                if leaf in held_back:
                    held.add(leaf)
                    continue
                expected = (ABSENT if current is None
                            else (current.mtime_ns, current.digest))
                writes.append(Write(os.path.join(leaf, sub, rel), winner.content,
                                    winner.mtime_ns,
                                    os.path.join(source_leaf, sub, rel),
                                    "leaf-dir", expected))
        for rel in sorted(deleted):
            for leaf in writable:
                if rel not in present.get(leaf, {}):
                    continue
                if leaf in held_back:
                    held.add(leaf)
                    continue
                deletes.append(Delete(os.path.join(leaf, sub, rel), None, sub))
    return DirsPlan(files=file_count, writes=tuple(sorted(writes, key=lambda w: w.dest)),
                    deletes=tuple(sorted(deletes, key=lambda d: d.path)),
                    deleted=tuple(deleted_all), held_back=tuple(sorted(held)))


def gather_leaf_dirs(plan, state_dir, held_back):
    dirs = {}
    for leaf in plan.leaves:
        for sub in LEAF_DIRS:
            dirs[(leaf, sub)] = read_leaf_dir(leaf, sub)
    blocked = {path for path, _ in plan.blocked_leaves}
    writable = [leaf for leaf in plan.leaves if leaf not in blocked]
    return plan_leaf_dirs(dirs, load_state_json(state_dir, DIRS_BASE_FILENAME),
                          writable, held_back)


def save_dirs_base(state_dir, leaves):
    base = load_state_json(state_dir, DIRS_BASE_FILENAME)
    for leaf in leaves:
        for sub in LEAF_DIRS:
            files, _ = read_leaf_dir(leaf, sub)
            if files is None:
                base.pop(_dir_key(leaf, sub), None)
            else:
                base[_dir_key(leaf, sub)] = sorted(files)
    save_state_json(state_dir, DIRS_BASE_FILENAME, base)


def _apply_dir_write(write, applied):
    parent = os.path.dirname(write.dest)
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as error:
        LOGGER.error("cannot create %s: %s", parent, error)
        applied["errors"] += 1
        return
    _apply_write(write, applied, "dir_files_written")


def _apply_dir_delete(delete, applied):
    """Remove a mirrored file, refusing anything outside LEAF_DIRS or not a
    regular file: the planner cannot produce one, this is the last guard."""
    parts = os.path.normpath(delete.path).split(os.sep)
    inside = any(sub in parts[:-1] for sub in LEAF_DIRS) and ".." not in parts
    try:
        regular = stat.S_ISREG(os.lstat(delete.path).st_mode)
    except FileNotFoundError:
        return
    except OSError:
        regular = False
    if not inside or not regular:
        LOGGER.error("refusing to delete %s: not a regular file inside %s",
                     delete.path, "/".join(LEAF_DIRS))
        applied["skipped"] += 1
        return
    if _unlink_quietly(delete.path):
        applied["dir_files_deleted"] += 1
        LOGGER.debug("deleted %s (removed on another account)", delete.path)
    else:
        applied["errors"] += 1


def format_dirs_counts(dirs_plan):
    return "files=%d writes=%d deletes=%d held_back=%d" % (
        dirs_plan.files, len(dirs_plan.writes), len(dirs_plan.deletes),
        len(dirs_plan.held_back))


def sync_once(root, execute=False, state_dir=None, app_running=None):
    """Plan a sync and, when execute is set, apply it.

    A backup is taken only when the plan is non-empty and execute is set, so
    a dry run and a converged tree both leave the state directory alone. The
    stale-temp-file sweep is the one thing an --execute run does even with
    nothing to apply: it holds the lock, so it is the only safe moment, and a
    converged tree is exactly where a leftover would otherwise sit for ever.
    The scheduled-tasks and leaf-folder bases are saved on every --execute run,
    writes or not: they are what tells a later run that something missing was
    deleted.
    """
    state_dir = state_dir or default_state_dir()
    plan = plan_sync(gather(root))
    log_plan_findings(plan)
    held = held_back_leaves(root, plan.leaves, app_running)
    tasks_plan = gather_scheduled_tasks(plan, state_dir, held)
    log_tasks_findings(tasks_plan)
    dirs_plan = gather_leaf_dirs(plan, state_dir, held)
    result = SyncResult(plan=plan, backup=None, applied=None, backup_skipped=0,
                        tasks=tasks_plan, dirs=dirs_plan)
    if not execute:
        return result
    sweep_stale_temp_files(plan.leaves)
    if plan.is_empty() and not tasks_plan.writes and not (
            dirs_plan.writes or dirs_plan.deletes):
        save_tasks_base(state_dir, plan.leaves)
        save_dirs_base(state_dir, plan.leaves)
        return result
    backup = take_backup(root, state_dir, skip_paths=unreadable_paths(plan))
    if backup is None:
        # No backup, no writes: the whole point of the backup is to be able to
        # undo what comes after it. take_backup has already said why.
        return result._replace(applied=empty_applied(errors=1))
    applied = apply_plan(plan)
    for write in tasks_plan.writes:
        _apply_write(write, applied, "tasks_written")
    for write in dirs_plan.writes:
        _apply_dir_write(write, applied)
    for delete in dirs_plan.deletes:
        _apply_dir_delete(delete, applied)
    save_tasks_base(state_dir, plan.leaves)
    save_dirs_base(state_dir, plan.leaves)
    return result._replace(backup=backup.path, applied=applied,
                           backup_skipped=backup.skipped)


# ------------------------------------------------------------ app-wide files
#
# A few files at the top of userData hold per-account state under keys named
# after the account (or account/org). Everything listed in APP_SCOPES is
# merged across accounts; everything else in those files is never touched.
#
# Deliberately NOT here: sign-in material (config.json's device tokens,
# ant-device-registry.json, remote-control-state.json identities), whatever
# the app re-downloads from the server (plugin and skill manifests, plan usage
# history), the bypass-permissions settings (a safety consent and an org gate,
# not a preference), and caches the app rebuilds from the records
# (archived-sessions.idx).
#
# The app keeps these files in memory and rewrites them whole, so they are only
# ever written while it is not running. With no timestamps inside, "which copy
# is newer" is decided against the value saved after the last --execute run:
# the account whose value changed since then wins; with no change (or no saved
# run yet), the loaded account's value wins, then the largest.

APP_FILES_BASE_FILENAME = "app-files-base.json"
APP_FILES_BACKUP_DIRNAME = "app-files"

# (scope id, file, path to the map inside it, key kind, merge kind)
#   key kind "account": the map's keys are account uuids;
#            "account/org": "<account>/<org>", one per leaf of either tree;
#            "suffix": "<name>.<account>" keys, one family per <name>.
#   merge kind "value": the whole value is one unit;
#              "presets": a dict whose "presets" list merges by "id";
#              "set": a list of strings, merged as a set.
APP_SCOPES = (
    ("code-sections", "claude_desktop_config.json",
     ("preferences", "epitaxyPrefs", "dframe-code-sections"), "account/org", "value"),
    ("epitaxy-flags", "claude_desktop_config.json",
     ("preferences", "epitaxyPrefs"), "suffix", "value"),
    ("cowork-model-fallback", "claude_desktop_config.json",
     ("preferences", "coworkModelAutoFallbackByAccount"), "account", "value"),
    ("session-presets", "ccd-session-presets.json", ("byAccount",), "account",
     "presets"),
    ("mcp-tool-toggles", "mcp-user-tool-toggles.json", ("owners",), "account",
     "set"),
)

AppFilesPlan = collections.namedtuple(
    "AppFilesPlan", "files writes changed held_back")


def _digest_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _json_indent(content):
    """The indent the app wrote the file with, so a rewrite diffs minimally."""
    for line in content.decode("utf-8", "replace").splitlines()[1:2]:
        stripped = line.lstrip(" \t")
        prefix = line[:len(line) - len(stripped)]
        if prefix.startswith("\t"):
            return "\t"
        if prefix:
            return len(prefix)
    return None


def _dig(data, path, create=False):
    node = data
    for key in path:
        if not isinstance(node, dict):
            return None
        if key not in node:
            if not create:
                return None
            node[key] = {}
        node = node[key]
    return node if isinstance(node, dict) else None


def _scope_families(node, key_kind, accounts, account_orgs):
    """{family: {key: account}}: the keys that must all hold one merged value."""
    if key_kind == "account":
        return {"": {account: account for account in accounts}}
    if key_kind == "account/org":
        return {"": {"%s/%s" % pair: pair[0] for pair in account_orgs}}
    families = {}
    for key in node:
        name, _, account = key.rpartition(".")
        if name and is_uuid(account) and account.lower() in accounts:
            families.setdefault(name, {})
    for name in families:
        families[name] = {"%s.%s" % (name, account): account for account in accounts}
    return families


def _merge_values(values, base, merge_kind, active):
    """One merged value for a family. values: {key: (account, value)}."""
    changed = sorted(key for key, (_, value) in values.items()
                     if base.get(key) != _digest_value(value))
    if base and changed:
        pool = changed
    else:
        pool = sorted(values)

    # With nothing to say which copy is newer, the account the app last loaded
    # is the one the user has been looking at; then the copy holding more.
    def rank(key):
        account, value = values[key]
        return (account == active, len(json.dumps(value, sort_keys=True)), key)

    if merge_kind == "value":
        return values[max(pool, key=rank)][1]
    winner_key = max(pool, key=rank)
    if merge_kind == "set":
        deleted = set()
        for key, (_, value) in values.items():
            known = base.get(key + "\nitems")
            if isinstance(known, list):
                deleted |= set(known) - set(value)
        merged = []
        for key in [winner_key] + sorted(values):
            for item in values[key][1]:
                if item not in merged and item not in deleted:
                    merged.append(item)
        return merged
    # presets: union the list by id, other keys from the winning value.
    deleted = set()
    for key, (_, value) in values.items():
        known = base.get(key + "\nitems")
        if isinstance(known, list):
            deleted |= set(known) - {p.get("id") for p in value.get("presets", [])
                                     if isinstance(p, dict)}
    merged = dict(values[winner_key][1])
    by_id = {}
    order = []
    for key in [winner_key] + sorted(values):
        for preset in values[key][1].get("presets", []):
            if not isinstance(preset, dict) or not isinstance(preset.get("id"), str):
                continue
            preset_id = preset["id"]
            if preset_id in deleted:
                continue
            if preset_id not in by_id:
                order.append(preset_id)
                by_id[preset_id] = preset
            elif (preset.get("lastUsedAt") or 0) > (by_id[preset_id].get("lastUsedAt") or 0):
                by_id[preset_id] = preset
    merged["presets"] = [by_id[preset_id] for preset_id in order]
    return merged


def _value_ok(value, merge_kind):
    if merge_kind == "set":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if merge_kind == "presets":
        return isinstance(value, dict) and isinstance(value.get("presets", []), list)
    return True


def _items(value, merge_kind):
    if merge_kind == "set":
        return sorted(value)
    if merge_kind == "presets":
        return sorted(p["id"] for p in value.get("presets", [])
                      if isinstance(p, dict) and isinstance(p.get("id"), str))
    return None


def read_app_file(user_data, name):
    path = os.path.join(user_data, name)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
    except OSError:
        return None
    entry, _ = _read_entry(path, name=name)
    if entry is None or entry.parsed is None:
        return None
    return entry


def plan_app_files(files, accounts, account_orgs, base, active=None,
                   app_running=False, now_ns=None):
    """Pure merge of APP_SCOPES. files: {name: FileEntry}; accounts: account
    uuids that have at least one leaf; account_orgs: (account, org) pairs.

    Returns the new bytes for every file that changes; while the app runs they
    are counted as held back instead.
    """
    new_data = {name: json.loads(entry.content.decode("utf-8"))
                for name, entry in files.items()}
    changed = []
    for scope, name, path, key_kind, merge_kind in APP_SCOPES:
        if name not in new_data:
            continue
        node = _dig(new_data[name], path)
        if node is None:
            continue
        scope_base = base.get(scope, {})
        for family, keys in sorted(_scope_families(node, key_kind, accounts,
                                                   account_orgs).items()):
            values = {key: (account, node[key]) for key, account in keys.items()
                      if key in node and _value_ok(node[key], merge_kind)}
            if not values or any(key in node and key not in values for key in keys):
                continue
            merged = _merge_values(values, scope_base, merge_kind, active)
            for key in sorted(keys):
                if key not in node or node[key] != merged:
                    node[key] = merged
                    changed.append((scope, key))
    writes = []
    held = []
    for name, entry in sorted(files.items()):
        original = json.loads(entry.content.decode("utf-8"))
        if new_data[name] == original:
            continue
        if app_running:
            held.append(name)
            continue
        text = json.dumps(new_data[name], indent=_json_indent(entry.content),
                          ensure_ascii=False)
        if entry.content.endswith(b"\n"):
            text += "\n"
        writes.append(Write(entry.name, text.encode("utf-8"), now_ns or 0,
                            "merged", "app-file", (entry.mtime_ns, entry.digest)))
    return AppFilesPlan(files=tuple(sorted(files)), writes=tuple(writes),
                        changed=tuple(changed), held_back=tuple(held))


def app_files_base(files, accounts, account_orgs):
    """What every scoped key holds now, for the next run to compare against."""
    base = {}
    for scope, name, path, key_kind, merge_kind in APP_SCOPES:
        if name not in files:
            continue
        node = _dig(files[name].parsed, path)
        if node is None:
            continue
        entries = base.setdefault(scope, {})
        for keys in _scope_families(node, key_kind, accounts, account_orgs).values():
            for key in keys:
                if key in node:
                    entries[key] = _digest_value(node[key])
                    items = (_items(node[key], merge_kind)
                             if _value_ok(node[key], merge_kind) else None)
                    if items is not None:
                        entries[key + "\nitems"] = items
    return base


def _accounts_and_orgs(leaves):
    pairs = sorted({(os.path.basename(os.path.dirname(leaf)).lower(),
                     os.path.basename(leaf).lower()) for leaf in leaves})
    return sorted({account for account, _ in pairs}), pairs


def gather_app_files(user_data, leaves, state_dir, app_running=None):
    files = {}
    for name in sorted({scope[1] for scope in APP_SCOPES}):
        entry = read_app_file(user_data, name)
        if entry is not None:
            files[name] = entry
    accounts, pairs = _accounts_and_orgs(leaves)
    if app_running is None and files:
        app_running = app_is_running()
    plan = plan_app_files(files, accounts, pairs,
                          load_state_json(state_dir, APP_FILES_BASE_FILENAME),
                          active=active_account(user_data),
                          app_running=bool(app_running), now_ns=time.time_ns())
    return plan._replace(writes=tuple(
        write._replace(dest=os.path.join(user_data, write.dest))
        for write in plan.writes))


def save_app_files_base(user_data, leaves, state_dir):
    files = {}
    for name in sorted({scope[1] for scope in APP_SCOPES}):
        entry = read_app_file(user_data, name)
        if entry is not None:
            files[name] = entry
    accounts, pairs = _accounts_and_orgs(leaves)
    save_state_json(state_dir, APP_FILES_BASE_FILENAME,
                    app_files_base(files, accounts, pairs))


def backup_app_files(state_dir, writes):
    """Copy every app file about to be rewritten aside, bytes and mtime."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backups_root = os.path.join(state_dir, BACKUPS_DIRNAME)
    destination = os.path.join(backups_root, stamp, APP_FILES_BACKUP_DIRNAME)
    try:
        os.makedirs(destination)
        for write in writes:
            shutil.copy2(write.dest, destination)
    except OSError as error:
        LOGGER.error("backup of the app files failed, nothing will be applied: %s",
                     error)
        _force_rmtree(os.path.dirname(destination))
        return None
    finally:
        prune_backups(backups_root)
    return os.path.dirname(destination)


def format_app_counts(app_plan):
    return "files=%d keys_changed=%d writes=%d held_back=%d" % (
        len(app_plan.files), len(app_plan.changed), len(app_plan.writes),
        len(app_plan.held_back))


def cowork_root(user_data):
    return os.path.join(user_data, COWORK_DIRNAME)


def extra_roots(user_data):
    """Trees with the same <account>/<org> layout, synced when present."""
    root = cowork_root(user_data)
    return [root] if os.path.isdir(root) and not os.path.islink(root) else []


# --------------------------------------------------------------- environment

def default_user_data(platform=None, home=None, environ=None):
    """The Claude desktop userData directory for a platform.

    macOS   ~/Library/Application Support/Claude
    Windows %APPDATA%\\Claude
    Linux   ~/.config/Claude
    """
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = os.path.expanduser("~") if home is None else home
    if platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Claude")
    if platform.startswith("win"):
        appdata = environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(appdata, "Claude")
    return os.path.join(home, ".config", "Claude")


def sessions_root(user_data):
    return os.path.join(user_data, SESSIONS_DIRNAME)


def log_level(verbosity):
    return logging.DEBUG if verbosity else logging.INFO


class LockHeld(Exception):
    """Raised when a LIVE run already holds the lock file."""

    def __init__(self, path, pid=None):
        Exception.__init__(self, path)
        self.path = path
        self.pid = pid


class StateDirUnusable(Exception):
    """Raised when the state directory cannot be created or written to."""

    def __init__(self, path, error):
        Exception.__init__(self, path)
        self.path = path
        self.error = error


def _lock_owner_pid(path):
    """The pid written inside the lock file, or None if there is not a real one.

    A missing, empty, truncated or non-numeric body means the owning run died
    between creating the file and writing its pid — there is no owner to ask
    about.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(64)
    except OSError:
        return None
    try:
        pid = int(raw.decode("ascii", "replace").strip())
    except ValueError:
        return None
    # pid 0 would mean "this process group" to os.kill, and a negative pid a
    # whole group: neither is an owner, so both count as no pid at all.
    return pid if pid > 0 else None


def _lock_owner_is_gone(path):
    """True only when the lock's owner is definitely not running any more.

    os.kill(pid, 0) delivers no signal, it only asks. ProcessLookupError is the
    single answer that means "gone"; PermissionError means the process exists
    and belongs to another user, which is still ALIVE, so the lock stands.
    """
    pid = _lock_owner_pid(path)
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


# Test seam: called with the lock path immediately after a successful claim, so
# a test can hold a REAL lock in another process at a known moment instead of
# guessing with a sleep. Never set outside the test-suite.
AFTER_LOCK_CLAIM = None


def _lock_temp_path(state_dir):
    return os.path.join(state_dir, "%s.%d.tmp" % (LOCK_FILENAME, os.getpid()))


def _claim_lock(path):
    """Claim the lock atomically. The claimed inode, or None if it is held.

    R2-B1: the lock file used to be created EMPTY (O_CREAT | O_EXCL) and the
    pid written into it afterwards. A concurrent run that read it in between
    saw no pid, called the LIVE lock stale, unlinked it and took it — so both
    runs held the lock. So the pid goes into `bridge.lock.<pid>.tmp` FIRST and
    the claim is `os.link(tmp, lock_path)`: one atomic step that fails with
    EEXIST when somebody holds it, and publishes a lock that already has its
    pid in it. The inode comes from the fd we wrote — a later lstat of the lock
    path could hand us the inode of somebody ELSE's lock, which the teardown
    would then delete.
    """
    state_dir = os.path.dirname(path)
    temp = _lock_temp_path(state_dir)
    try:
        # O_TRUNC rather than O_EXCL: pids are reused, so a temp file left by a
        # killed run that happened to have our pid must not wedge the tool.
        handle = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    except OSError as error:
        raise StateDirUnusable(state_dir, error)
    try:
        os.write(handle, str(os.getpid()).encode("ascii"))
        claimed = os.fstat(handle).st_ino
    except OSError as error:
        _unlink_quietly(temp)
        raise StateDirUnusable(state_dir, error)
    finally:
        os.close(handle)
    try:
        try:
            os.link(temp, path)
        except FileExistsError:
            return None
        except OSError as error:
            # No hard links on this filesystem, or the directory turned
            # unwritable: either way this state directory cannot be used.
            raise StateDirUnusable(state_dir, error)
    finally:
        _unlink_quietly(temp)
    return claimed


def _unlink_lock_if_inode(path, inode):
    """Unlink the lock only while it is still the inode we are talking about.

    True when nothing of that inode is at the path any more (we removed it, or
    it had already gone), False when a DIFFERENT file is there now — which
    means another run's lock, and it is not ours to delete.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if st.st_ino != inode:
        return False
    return _unlink_quietly(path)


def _lock_file_stat(path):
    """lstat of the lock file, None when it is absent.

    Anything that is not a regular file at that path (a directory, a symlink)
    is a broken state directory, not a lock to reason about (R2-NB7): reading a
    pid out of it cannot work, and unlinking it is not this tool's business.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StateDirUnusable(os.path.dirname(path), error)
    if not stat.S_ISREG(st.st_mode):
        raise StateDirUnusable(os.path.dirname(path), (
            "%s exists and is not a regular file, so it cannot be a lock file; "
            "move it out of the way" % path))
    return st


def _claim_or_held(path):
    claimed = _claim_lock(path)
    if claimed is None:
        # Somebody else took it in the window; that one is alive by definition,
        # so do not go round again.
        raise LockHeld(path, _lock_owner_pid(path))
    return claimed


def _reclaim_lock(path):
    """Take over a lock left behind by a DEAD owner. The claimed inode.

    Raises LockHeld when the owner is alive, when the file stopped being the
    one that was judged stale, or when another run won the retry.
    """
    st = _lock_file_stat(path)
    if st is None:
        # Released while we were looking: nothing to reclaim, just claim it.
        return _claim_or_held(path)
    pid = _lock_owner_pid(path)
    if not _lock_owner_is_gone(path):
        raise LockHeld(path, pid)
    LOGGER.warning("reclaiming the lock file %s: its owner (pid %s) is no "
                   "longer running", path, "unknown" if pid is None else pid)
    if not _unlink_lock_if_inode(path, st.st_ino):
        # A different file is at the path now, so it was never judged: a live
        # run claimed it in the window. Unlinking "whatever is there" is
        # exactly how two runs used to end up holding the lock at once.
        LOGGER.warning("not reclaiming %s after all: it is no longer the lock "
                       "file that was judged stale", path)
        raise LockHeld(path, _lock_owner_pid(path))
    return _claim_or_held(path)


def _release_lock(path, claimed):
    """Remove OUR lock file. A lock we did not claim is left strictly alone."""
    if _unlink_lock_if_inode(path, claimed):
        return
    LOGGER.warning("leaving the lock file %s in place: it is no longer the one "
                   "this run claimed, so it belongs to another run", path)


@contextlib.contextmanager
def file_lock(state_dir):
    """Exclusive, non-blocking lock, reclaimed when its owner is DEAD.

    A lock whose owning process is still alive is reported and never stolen. A
    lock left behind by a killed run (or a crashed one that never got as far as
    writing its pid) used to wedge the tool forever — including the dry run that
    would have diagnosed it — so it is reclaimed once, loudly. Both the reclaim
    and the release act on the INODE they judged or created, never on whatever
    happens to sit at the path (R2-B1).
    """
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError as error:
        raise StateDirUnusable(state_dir, error)
    path = os.path.join(state_dir, LOCK_FILENAME)
    claimed = _claim_lock(path)
    if claimed is None:
        claimed = _reclaim_lock(path)
    try:
        if AFTER_LOCK_CLAIM is not None:
            AFTER_LOCK_CLAIM(path)
        yield path
    finally:
        _release_lock(path, claimed)


def default_runner(argv, check=False):
    """The only place a subprocess is ever started."""
    return subprocess.run(argv, capture_output=True, text=True, check=check)


def agent_is_loaded(runner=None, platform=None):
    """True / False / None (cannot tell — not macOS, or launchctl unavailable).

    R2-B3: os.getuid() does not exist on Windows and AttributeError is not an
    OSError, so asking launchd anywhere but macOS used to traceback out of
    `status` instead of answering "cannot tell". launchd is macOS-only, so off
    darwin there is nothing to ask and the uid is never looked up.
    """
    platform = sys.platform if platform is None else platform
    if platform != "darwin":
        return None
    runner = runner or default_runner
    try:
        result = runner(["launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)])
    except (OSError, ValueError):
        return None
    return result.returncode == 0


# ----------------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(
        prog="bridge.py",
        description="Converge the Claude Code session list across the accounts "
                    "of one desktop install.")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser(
        "sync", help="merge every account/org leaf (dry-run by default)")
    _add_common(sync)
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="report what would change and write nothing (default)")
    mode.add_argument("--execute", action="store_true",
                      help="actually write, after taking a backup")

    status = subparsers.add_parser(
        "status", help="list the leaves, their counts and the current drift")
    _add_common(status)
    status.add_argument("--agents-dir", default=None, metavar="DIR",
                        help="override ~/Library/LaunchAgents (where the "
                             "LaunchAgent plist is looked for)")

    install = subparsers.add_parser(
        "install-agent",
        help="install the macOS LaunchAgent that syncs on every change")
    _add_common(install)
    install.add_argument("--agents-dir", default=None, metavar="DIR",
                         help="override ~/Library/LaunchAgents")

    uninstall = subparsers.add_parser(
        "uninstall-agent", help="boot the LaunchAgent out and delete its plist")
    uninstall.add_argument("-v", "--verbose", action="count", default=0,
                           help="debug logging")
    uninstall.add_argument("--agents-dir", default=None, metavar="DIR",
                           help="override ~/Library/LaunchAgents")
    return parser


def _add_common(subparser):
    subparser.add_argument("-v", "--verbose", action="count", default=0,
                           help="debug logging")
    subparser.add_argument("--user-data", default=None, metavar="DIR",
                           help="override the platform userData directory")
    subparser.add_argument("--state-dir", default=None, metavar="DIR",
                           help="override ~/.claude-code-multi-account-bridge "
                                "(backups and lock file)")


def main(argv=None, runner=None, configure_logging=False):
    parser = build_parser()
    args = parser.parse_args(argv)
    if configure_logging:
        logging.basicConfig(level=log_level(getattr(args, "verbose", 0)),
                            stream=sys.stderr,
                            format="%(levelname)s %(message)s")
    runner = runner or default_runner
    if args.command == "sync":
        return cmd_sync(args)
    if args.command == "status":
        return cmd_status(args, runner)
    if args.command == "install-agent":
        return cmd_install_agent(args, runner)
    if args.command == "uninstall-agent":
        return cmd_uninstall_agent(args, runner)
    parser.error("unknown command %r" % args.command)


def _paths(args):
    user_data = args.user_data or default_user_data()
    state_dir = args.state_dir or default_state_dir()
    return sessions_root(user_data), state_dir


def path_problem(root, state_dir):
    """The reason these paths cannot be used, or None when they can.

    A missing or mistyped root used to read as "no leaves", i.e. "in sync",
    exit 0 (B5) — the single most misleading answer the tool could give. A
    state directory inside the sessions root would have every backup copy the
    previous ones, so the tree grows without bound and the ring is worthless
    (N12).
    """
    if not os.path.exists(root):
        return ("the sessions root %s does not exist; check --user-data (it "
                "must point at the Claude userData directory, which contains "
                "%s)" % (root, SESSIONS_DIRNAME))
    if not os.path.isdir(root):
        return "the sessions root %s is not a directory" % root
    if _is_inside(state_dir, root):
        return ("the state directory %s is inside the sessions root %s, so "
                "every backup would copy the earlier backups; choose a path "
                "outside the tree" % (state_dir, root))
    return None


def _is_inside(inner, outer):
    """True when inner IS outer or lives underneath it.

    R2-NB4: BOTH sides are realpath'd, because abspath does not resolve links
    and a --state-dir reached through a symlink then walked straight past this
    refusal — every backup copying the previous ones, without bound. realpath is
    used HERE ONLY, for the comparison: no write target is ever resolved (a
    symlinked state directory outside the tree stays perfectly usable, and its
    backups land under the path the user gave).
    """
    try:
        inner = os.path.realpath(os.path.abspath(inner))
        outer = os.path.realpath(os.path.abspath(outer))
        return os.path.commonpath([inner, outer]) == outer
    except (ValueError, OSError):
        # different drives, or a path this platform cannot compare
        return False


def _refuse_bad_paths(root, state_dir):
    """Log and return the exit code when the paths are unusable, else None."""
    problem = path_problem(root, state_dir)
    if problem is None:
        return None
    LOGGER.error("%s", problem)
    return 2


def _format_counts(counts):
    order = ("leaves", "records_seen", "copies_written", "records_deleted",
             "tombstones_copied", "parse_failures", "blocked", "blocked_leaves",
             "ignored", "id_mismatches")
    return " ".join("%s=%d" % (key, counts[key]) for key in order)


def sync_app_files(user_data, leaves, state_dir, execute=False, app_running=None):
    """Plan, and when executing back up and apply, the APP_SCOPES merge.

    Returns (plan, backup directory or None, applied counts or None). The base
    is saved on every --execute run, held back or not: it is what tells the
    next run which account changed a value.
    """
    plan = gather_app_files(user_data, leaves, state_dir, app_running)
    if not execute:
        return plan, None, None
    if not plan.writes:
        if plan.files:
            save_app_files_base(user_data, leaves, state_dir)
        return plan, None, None
    backup = backup_app_files(state_dir, plan.writes)
    if backup is None:
        return plan, None, empty_applied(errors=1)
    applied = empty_applied()
    for write in plan.writes:
        _apply_write(write, applied, "app_files_written")
    save_app_files_base(user_data, leaves, state_dir)
    return plan, backup, applied


def _root_label(root, first):
    return "" if first else " " + os.path.basename(root)


def cmd_sync(args):
    root, state_dir = _paths(args)
    refused = _refuse_bad_paths(root, state_dir)
    if refused is not None:
        return refused
    execute = bool(args.execute)
    user_data = os.path.dirname(root)
    roots = [root] + extra_roots(user_data)
    # A dry run writes nothing at all, so it needs neither the lock nor the
    # state directory: it is the one command that must keep working while a
    # lock is in place, because it is how you find out what is going on.
    guard = file_lock(state_dir) if execute else contextlib.nullcontext()
    try:
        with guard:
            results = [sync_once(each, execute=execute, state_dir=state_dir)
                       for each in roots]
            leaves = [leaf for result in results for leaf in result.plan.leaves]
            app_plan, app_backup, app_applied = sync_app_files(
                user_data, leaves, state_dir, execute=execute)
    except LockHeld as held:
        LOGGER.error("another run is already running (pid %s, lock file %s); "
                     "delete it if you are sure no run is in progress",
                     "unknown" if held.pid is None else held.pid, held.path)
        return 2
    except StateDirUnusable as unusable:
        LOGGER.error("cannot use the state directory %s (%s); pass --state-dir "
                     "to put the backups and lock file somewhere writable",
                     unusable.path, unusable.error)
        return 2
    mode = "execute" if execute else "dry-run"
    pending = False
    failed = False
    for index, result in enumerate(results):
        label = _root_label(roots[index], index == 0)
        print("sync%s (%s): %s" % (label, mode, _format_counts(result.plan.counts())))
        if result.tasks.files:
            print("scheduled tasks%s: %s" % (label, format_tasks_counts(result.tasks)))
        if result.dirs.files or result.dirs.deletes or result.dirs.held_back:
            print("leaf folders%s: %s" % (label, format_dirs_counts(result.dirs)))
        if result.backup:
            # `skipped` here counts paths left OUT OF THE BACKUP because they
            # could not be read (R2-NB2) — not the `skipped` on the applied
            # line below, which counts writes refused at the last moment.
            print("backup: %s skipped=%d" % (result.backup, result.backup_skipped))
        pending = pending or not (result.plan.is_empty() and not result.tasks.writes
                                  and not result.dirs.writes
                                  and not result.dirs.deletes)
        if result.applied:
            # The plan says what SHOULD happen; this says what did. They differ
            # when a write was refused at the last moment (the destination
            # changed under us), when a delete was refused by the filename
            # guard, or when something failed outright — none of which may
            # ever be invisible.
            print("applied: %s" % _format_applied(result.applied))
            failed = failed or bool(result.applied["errors"])
    if app_plan.files:
        print("app files: %s" % format_app_counts(app_plan))
        pending = pending or bool(app_plan.writes)
    if app_backup:
        print("backup: %s skipped=0" % app_backup)
    if app_applied:
        print("applied: %s" % _format_applied(app_applied))
        failed = failed or bool(app_applied["errors"])
    if not execute and pending:
        print("nothing was written; re-run with --execute to apply this plan")
    return 1 if failed else 0


def _format_applied(applied):
    order = ("copies_written", "tombstones_copied", "records_deleted",
             "tasks_written", "dir_files_written", "dir_files_deleted",
             "app_files_written", "skipped", "errors")
    return " ".join("%s=%d" % (key, applied[key]) for key in order)


def cmd_status(args, runner):
    root, state_dir = _paths(args)
    refused = _refuse_bad_paths(root, state_dir)
    if refused is not None:
        return refused
    states = gather(root)
    plan = plan_sync(states)
    # A read-only look must surface exactly what a sync would complain about.
    log_plan_findings(plan)
    print("sessions root: %s" % root)
    print("leaves: %d" % len(states))
    for state in states:
        print("  %s  records=%d tombstones=%d" % (
            state.path, len(state.records), len(state.tombstones)))
    # P2-4: unconditionally. The counts are where a blocked path, a blocked
    # leaf, a parse failure or an id mismatch becomes visible, and every one of
    # those can sit in a tree with NO drift at all — printing them only when
    # something needed doing made "every leaf is in sync" mean two very
    # different things.
    print("counts: %s" % _format_counts(plan.counts()))
    user_data = os.path.dirname(root)
    leaves = list(plan.leaves)
    for index, each in enumerate([root] + extra_roots(user_data)):
        result = sync_once(each, execute=False, state_dir=state_dir)
        label = _root_label(each, index == 0)
        if index:
            leaves.extend(result.plan.leaves)
            print("sync%s: %s" % (label, _format_counts(result.plan.counts())))
            for write in result.plan.writes + result.plan.tombstone_copies:
                print("  would write  %s" % write.dest)
            for delete in result.plan.deletes:
                print("  would delete %s" % delete.path)
        print("scheduled tasks%s: %s" % (label, format_tasks_counts(result.tasks)))
        print("leaf folders%s: %s" % (label, format_dirs_counts(result.dirs)))
        for write in result.tasks.writes + result.dirs.writes:
            print("  would write  %s" % write.dest)
        for delete in result.dirs.deletes:
            print("  would delete %s" % delete.path)
        for leaf in sorted(set(result.tasks.held_back + result.dirs.held_back)):
            print("  held back    %s (app has this account loaded)" % leaf)
    app_plan, _, _ = sync_app_files(user_data, leaves, state_dir)
    print("app files: %s" % format_app_counts(app_plan))
    for scope, key in app_plan.changed:
        print("  would merge  %s: %s" % (scope, key))
    for name in app_plan.held_back:
        print("  held back    %s (app is running)" % os.path.join(user_data, name))
    if plan.is_empty():
        print("drift: none - every leaf is in sync")
    else:
        print("drift: %d to write, %d to delete" % (
            len(plan.writes) + len(plan.tombstone_copies), len(plan.deletes)))
        for write in plan.writes + plan.tombstone_copies:
            print("  would write  %s" % write.dest)
        for delete in plan.deletes:
            print("  would delete %s" % delete.path)
    print("agent: %s" % describe_agent_state(
        plist_path(args.agents_dir or default_agents_dir()),
        agent_is_loaded(runner)))
    return 0


def describe_agent_state(target, loaded):
    """One line covering BOTH halves of "is the agent installed".

    launchctl's answer alone is not the whole story: a loaded job whose plist
    has gone will not come back after a logout, and a plist that is present but
    not loaded does nothing at all while looking installed. Every negative
    variant keeps the words "not loaded" so the line reads the same way whatever
    the cause.
    """
    exists = os.path.exists(target)
    plist = ("%s" % target) if exists else ("no plist at %s" % target)
    if loaded is None:
        return "unknown (launchctl unavailable) - %s" % plist
    if loaded and not exists:
        # P2-5: "loaded - no plist at ..." read as a positive verdict on a
        # broken install. The job is running now but will not come back after a
        # logout, and there is nothing on disk to bring it back.
        return ("not loaded properly - launchd has the job but there is no "
                "plist at %s; run `install-agent`" % target)
    if loaded:
        return "loaded - %s" % plist
    if not exists:
        return "not loaded - %s; run `install-agent` to set it up" % plist
    return ("not loaded - %s exists but launchd does not have it; run "
            "`install-agent` again" % plist)


# ------------------------------------------------------------- launchd (macOS)

SYSTEM_PYTHON = "/usr/bin/python3"
DEFAULT_AGENTS_DIRNAME = os.path.join("Library", "LaunchAgents")
LOGS_DIRNAME = os.path.join("Library", "Logs")


def default_agents_dir(home=None):
    home = os.path.expanduser("~") if home is None else home
    return os.path.join(home, DEFAULT_AGENTS_DIRNAME)


def log_paths(home=None):
    home = os.path.expanduser("~") if home is None else home
    logs = os.path.join(home, LOGS_DIRNAME)
    return (os.path.join(logs, LABEL + ".out.log"),
            os.path.join(logs, LABEL + ".err.log"))


def watch_paths(root, leaves):
    """The root, every account directory, then every leaf — each once, in order.

    launchd WatchPaths fires on a change to a watched directory's own contents.
    A directory's mtime does NOT change when something appears in a
    grandchild, so watching only the root plus the leaves misses the one case
    that matters most in between: a NEW ORG folder created under an account
    that already exists. Watching the account directories closes that gap.
    """
    ordered = [root]
    for candidate in [os.path.dirname(leaf) for leaf in leaves] + list(leaves):
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def plist_dict(script_path, root, leaves, user_data=None, state_dir=None, home=None):
    """The LaunchAgent definition as a plain dict.

    WatchPaths covers the sessions root, every account directory and every
    current leaf, so a brand-new account or org folder also fires the job (sync
    re-discovers leaves on every run regardless).
    """
    argv = [SYSTEM_PYTHON, script_path, "sync", "--execute"]
    if user_data:
        argv += ["--user-data", user_data]
    if state_dir:
        argv += ["--state-dir", state_dir]
    out_path, err_path = log_paths(home)
    return {
        "Label": LABEL,
        "ProgramArguments": argv,
        "WatchPaths": watch_paths(root, leaves),
        "ThrottleInterval": THROTTLE_INTERVAL,
        "StartInterval": START_INTERVAL,
        "StandardOutPath": out_path,
        "StandardErrorPath": err_path,
    }


def plist_bytes(script_path, root, leaves, user_data=None, state_dir=None, home=None):
    return plistlib.dumps(plist_dict(
        script_path, root, leaves, user_data=user_data, state_dir=state_dir,
        home=home))


def plist_path(agents_dir):
    return os.path.join(agents_dir, LABEL + ".plist")


AGENT_SCRIPT_NAME = "bridge.py"


def copy_agent_script(source, state_dir):
    """Copy the script the agent will run into the state directory.

    Returns the copy's path, or None (after logging) when it cannot be written.
    Temp file + rename, so a running agent never sees a half-copied script.
    """
    target = os.path.join(state_dir, AGENT_SCRIPT_NAME)
    temp = os.path.join(state_dir, ".%s.tmp-%d" % (AGENT_SCRIPT_NAME, os.getpid()))
    try:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        with open(source, "rb") as handle:
            payload = handle.read()
        with open(temp, "wb") as handle:
            handle.write(payload)
        os.chmod(temp, 0o700)
        os.replace(temp, target)
    except OSError as error:
        _unlink_quietly(temp)
        LOGGER.error("cannot place the agent's copy of %s at %s (%s); nothing "
                     "was installed and nothing was unloaded", source, target,
                     error)
        return None
    return target


def install_agent(agents_dir, root, leaves, runner=None, platform=None,
                  user_data=None, state_dir=None, script_path=None, echo=None):
    """Write the plist and (re-)bootstrap it. Returns an exit code."""
    platform = sys.platform if platform is None else platform
    echo = echo or (lambda line: None)
    if platform != "darwin":
        LOGGER.error("install-agent is macOS only (launchd); this platform is %r. "
                     "Run `sync --execute` from your own scheduler instead.",
                     platform)
        return 3
    runner = runner or default_runner
    target = plist_path(agents_dir)
    try:
        os.makedirs(agents_dir, exist_ok=True)
    except OSError as error:
        # R2-NB5: before the bootout, so nothing has been disturbed yet.
        LOGGER.error("cannot create the LaunchAgents directory %s (%s); "
                     "nothing was installed and nothing was unloaded",
                     agents_dir, error)
        return 2
    # launchd runs /usr/bin/python3 WITHOUT the user's privacy grants: a script
    # under ~/Documents, ~/Desktop or ~/Downloads fails with "Operation not
    # permitted" on every run (measured on macOS 26). So the agent runs a copy
    # placed in the state directory, which is never under a protected folder.
    # Done BEFORE the bootout, so a failure here disturbs nothing.
    agent_script = copy_agent_script(
        script_path or os.path.abspath(__file__),
        state_dir or default_state_dir())
    if agent_script is None:
        return 2
    payload = plist_bytes(
        agent_script, root, leaves, user_data=user_data, state_dir=state_dir)
    domain = "gui/%d" % os.getuid()
    # ALWAYS boot out first, plist or no plist: the label can be loaded from a
    # copy that has since been moved or deleted, and bootstrap then fails with
    # "already loaded". A bootout with nothing loaded simply returns non-zero,
    # which is why the result is not checked.
    runner(["launchctl", "bootout", "%s/%s" % (domain, LABEL)])
    try:
        with open(target, "wb") as handle:
            handle.write(payload)
    except OSError as error:
        # R2-NB5: the previous job is already gone at this point, so saying only
        # "cannot write" would leave the user thinking the old one still runs.
        LOGGER.error("cannot write the LaunchAgent plist %s (%s); the "
                     "previously loaded job (if there was one) has been booted "
                     "out, so NOTHING is loaded now — fix the permissions on "
                     "%s and run `install-agent` again",
                     target, error, agents_dir)
        _unlink_quietly(target)
        return 2
    result = runner(["launchctl", "bootstrap", domain, target])
    if result.returncode != 0:
        # Leaving the plist behind would be the worst outcome: the previous copy
        # has already been booted out, so nothing is running, while the file on
        # disk says the agent is installed.
        removed = _unlink_quietly(target)
        LOGGER.error("launchctl bootstrap failed (%d): %s; the plist has %sbeen "
                     "removed (%s), so nothing is installed or loaded",
                     result.returncode, (result.stderr or "").strip(),
                     "" if removed else "NOT ", target)
        LOGGER.error("if launchctl says the job is already loaded, run "
                     "`uninstall-agent` first, then `install-agent` again")
        return 4
    echo("agent installed: %s" % target)
    echo("watching: %s" % ", ".join(watch_paths(root, leaves)))
    return 0


def uninstall_agent(agents_dir, runner=None, platform=None, echo=None):
    """Boot the agent out and delete its plist. Returns an exit code."""
    platform = sys.platform if platform is None else platform
    echo = echo or (lambda line: None)
    if platform != "darwin":
        LOGGER.error("uninstall-agent is macOS only (launchd); this platform "
                     "is %r.", platform)
        return 3
    runner = runner or default_runner
    runner(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)])
    target = plist_path(agents_dir)
    if os.path.exists(target):
        try:
            os.unlink(target)
        except OSError as error:
            # R2-NB5: the job HAS been booted out; only the file is left.
            LOGGER.error("cannot delete the LaunchAgent plist %s (%s); the job "
                         "has been booted out, so it is not running, but the "
                         "plist is still there and will load again at your next "
                         "login — remove it by hand", target, error)
            return 2
        echo("agent removed: %s" % target)
    else:
        echo("no plist at %s; nothing to remove" % target)
    return 0


def cmd_install_agent(args, runner):
    root, state_dir = _paths(args)
    refused = _refuse_bad_paths(root, state_dir)
    if refused is not None:
        return refused
    agents_dir = args.agents_dir or default_agents_dir()
    return install_agent(
        agents_dir=agents_dir, root=root, leaves=discover_leaves(root),
        runner=runner, user_data=args.user_data, state_dir=args.state_dir,
        echo=print)


def cmd_uninstall_agent(args, runner):
    agents_dir = args.agents_dir or default_agents_dir()
    return uninstall_agent(agents_dir=agents_dir, runner=runner, echo=print)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], configure_logging=True))
