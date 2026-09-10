"""Shared helpers for the bridge test-suite.

Every helper builds a SYNTHETIC tree under a caller-supplied temp directory.
Nothing here ever touches a real Claude userData directory.
"""

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import bridge  # noqa: E402  (import after sys.path fix-up)

# Keep the library's warnings out of the test output; unittest's assertLogs
# swaps handlers itself, so this does not hide anything a test asserts on.
import logging  # noqa: E402
bridge.LOGGER.addHandler(logging.NullHandler())

SESSIONS_DIRNAME = "claude-code-sessions"


def uid(n):
    """A deterministic, canonical v4-shaped UUID string for tests."""
    return "00000000-0000-4000-8000-%012d" % n


def make_root(base):
    """Create <base>/claude-code-sessions and return its path."""
    root = os.path.join(base, SESSIONS_DIRNAME)
    os.makedirs(root, exist_ok=True)
    return root


def make_leaf(base, account, org):
    """Create <base>/claude-code-sessions/<account>/<org> and return its path."""
    leaf = os.path.join(make_root(base), account, org)
    os.makedirs(leaf, exist_ok=True)
    return leaf


def set_mtime_ns(path, mtime_ns):
    os.utime(path, ns=(mtime_ns, mtime_ns))


def record_bytes(session_id, last_activity=1000, archived=False, title="t", extra=None):
    payload = {
        "id": session_id,
        "title": title,
        "cwd": "/synthetic/project",
        "isArchived": archived,
        "lastActivityAt": last_activity,
        "cliSessionId": session_id,
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def write_record(leaf, session_id, mtime_ns, last_activity=1000, archived=False,
                 title="t", extra=None, raw=None):
    """Write local_<session_id>.json into leaf with an exact mtime."""
    path = os.path.join(leaf, "local_%s.json" % session_id)
    data = raw if raw is not None else record_bytes(
        session_id, last_activity=last_activity, archived=archived, title=title, extra=extra)
    with open(path, "wb") as fh:
        fh.write(data)
    set_mtime_ns(path, mtime_ns)
    return path


def write_tombstone(leaf, session_id, mtime_ns, content=b"1700000000000"):
    path = os.path.join(leaf, "deleted_%s" % session_id)
    with open(path, "wb") as fh:
        fh.write(content)
    set_mtime_ns(path, mtime_ns)
    return path


def write_other(leaf_or_dir, name, content=b"{}", mtime_ns=None):
    """Write a file the bridge must never touch."""
    path = os.path.join(leaf_or_dir, name)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "wb") as fh:
        fh.write(content)
    if mtime_ns is not None:
        set_mtime_ns(path, mtime_ns)
    return path


def snapshot(path):
    """Map relpath -> (kind, payload, mtime_ns) for everything under path.

    Uses lstat only: a symlink is recorded as its link target string, never
    followed, so a snapshot cannot be fooled by a write through a symlink.
    """
    out = {}

    def walk(current, prefix):
        with os.scandir(current) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            rel = prefix + entry.name
            st = os.lstat(entry.path)
            if entry.is_symlink():
                out[rel] = ("symlink", os.readlink(entry.path), st.st_mtime_ns)
            elif entry.is_dir(follow_symlinks=False):
                out[rel] = ("dir", None, st.st_mtime_ns)
                walk(entry.path, rel + "/")
            else:
                with open(entry.path, "rb") as fh:
                    out[rel] = ("file", fh.read(), st.st_mtime_ns)

    if os.path.isdir(path):
        walk(path, "")
    return out


class RecordingRunner(object):
    """Injectable stand-in for subprocess.run: records argv, never executes."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, argv, check=False):
        self.calls.append(list(argv))
        key = " ".join(argv)
        for prefix, result in self.results.items():
            if key.startswith(prefix):
                return result
        return bridge.RunResult(returncode=0, stdout="", stderr="")
