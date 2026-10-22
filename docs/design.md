# claude-code-multi-account-bridge — design

## Problem

The Claude desktop app keeps the Code-tab session list per signed-in account:

```
<userData>/claude-code-sessions/<accountUuid>/<orgUuid>/
    local_<sessionId>.json     one record per session (title, cwd, isArchived, cliSessionId, ...)
    deleted_<sessionId>        tombstone written when a session is deleted (content: epoch ms)
    scheduled-tasks.json       per-account scheduled-task list (merged, see README)
    backlog/, spaces.json, ... per-account, NOT ours
```

`<userData>` is `~/Library/Application Support/Claude` on macOS, `%APPDATA%\Claude` on
Windows, `~/.config/Claude` on Linux.

Transcripts (`~/.claude/projects/<escaped-cwd>/<cliSessionId>.jsonl`) are already
shared across accounts, so a record copied into another account's folder is fully
resumable. Only the list is per account. Someone who alternates between two
accounts (e.g. when one hits its weekly usage limit) sees a stale sidebar on the
other account.

The app reads its folder once when the account loads. It has no file watcher on
it, and it never rewrites files it did not itself change. Records for other
sessions are left untouched.

## What the tool does

`sync` converges every `<account>/<org>` leaf folder to the same set of records:

1. **Discover** leaf folders: exactly two levels under `claude-code-sessions`,
   both directory names valid UUIDs, real directories (symlinks are skipped, never
   followed, never written through). A root that is missing or is not a directory
   is an ERROR naming the path, for `sync` and `status` alike — never an empty,
   "in sync" answer.
2. **Union by session id.** For each `local_<id>.json` present in any leaf, the
   winner is the copy with the newest mtime; ties broken by larger
   `lastActivityAt`, then by content hash for determinism. Archiving rewrites the
   file (mtime bumps) without necessarily bumping `lastActivityAt`, which is why
   mtime is the primary key. Ids are case-folded for IDENTITY (APFS is
   case-insensitive, so `local_<UUID>.json` and `local_<uuid>.json` are one file,
   not two ids) — but every path is built from the basename the directory
   listing actually gave, because on a case-SENSITIVE filesystem (Linux, a
   case-sensitive APFS volume) the folded id is not the file name and a path
   built from it points at nothing that exists. One id under TWO names in one
   leaf is only possible there, and it is unknowable which one the app reads:
   both copies are `blocked`, so neither is written to and neither is deleted.
   Files that fail to parse as JSON objects are logged and are never candidates;
   they are only ever deleted by a tombstone. A record whose own `id` field
   disagrees with its filename is counted and reported; the filename wins,
   because that is what the app reads the folder by.
3. **Tombstones win.** A `deleted_<id>` in any leaf removes `local_<id>.json` from
   every leaf and copies the tombstone to every leaf. Session ids are UUIDs and
   never reused, so a tombstone is final.
4. **Write** the winner into every leaf that lacks it or holds a different copy:
   temp file inside that leaf + atomic rename, then `os.utime` to the winner's
   mtime so ordering and idempotence hold, then an fsync of the leaf directory.
   An existing destination keeps its permission bits. Immediately before the
   rename the destination's (mtime, sha256) is compared with what the plan
   expected — `ABSENT` included — and a destination that changed underneath is
   left alone and counted in `skipped`; a lost newer copy would otherwise be
   invisible, since the winner's older mtime would be stamped on it. That check
   NARROWS the window between reading the destination and renaming over it; it
   cannot close it, because there is no atomic "replace only if unchanged" on
   these filesystems. A copy written in that last sliver is still lost. Nothing
   else in the leaf is touched. Deletes are deliberately NOT re-checked this
   way: a tombstone is final.
5. **Nothing impossible is ever planned.** A path the tool can neither read nor
   write — a directory or symlink wearing a record/tombstone name, an unreadable
   file, one over the 8 MB record cap, one of two names that differ only in case
   — is recorded as `blocked`, gets no write and no delete, and is reported. An
   unreadable copy also blocks every write of that session id anywhere, because a
   copy that cannot be read might be the newest one. A whole LEAF that cannot be
   listed (no read permission) or cannot be written into (no write/execute
   permission) is recorded as a `blocked_leaves` entry with its reason: nothing
   is written into it, no record in it is deleted and no tombstone is copied
   there, while the records it did yield still take part as SOURCES — an
   unwritable leaf can hold the newest copy. A name that only looks like ours
   (`local_`/`deleted_` plus something that is not a canonical UUID) is counted
   as `ignored`. This is what makes the plan CONVERGE: an unconvergeable plan
   means a full backup on every run and a temp file churned in the leaf every
   run, which re-fires WatchPaths. A blocked record under a tombstone therefore
   stays on disk until it is unblocked; it shows up in the `blocked` count on
   every run, including one that reports no drift at all.
6. **Idempotent.** A converged tree yields zero writes. Every run reports counts:
   leaves, records seen (distinct live session ids with at least one readable,
   parsable copy), copies written, records deleted, tombstones copied, parse
   failures, blocked, blocked leaves, ignored, id mismatches — plus, when
   executing, what was actually applied: copies written, tombstones copied,
   records deleted, skipped, errors. `status` prints that counts line
   unconditionally, drift or no drift, because a blocked path, a blocked leaf, a
   parse failure or an id mismatch can all sit in a tree with nothing to do.

`--dry-run` is the default; `--execute` writes. Before the first write of a run,
a backup of the entire `claude-code-sessions` tree is taken to
`~/.claude-code-multi-account-bridge/backups/<UTC timestamp>/` (keep the newest
10, made writable before removal so a read-only directory inside one cannot
strand it; a symlink named like a timestamp is not a backup, is never chmod'd or
deleted, and does not occupy a slot in the ring). The backup leaves OUT exactly
the paths discovery already reported as UNREADABLE — those records, those leaf
directories, and a blocked non-regular path that cannot be read either (a
mode-000 directory wearing a record's name) — each logged, counted on the
`backup:` line as `skipped` — since
otherwise one unreadable record failed the backup, and so every run, for ever,
which is the opposite of "report it and step around it". Any OTHER copy failure
still removes the half-written directory, applies nothing and exits non-zero.

An `--execute` run also sweeps stale `.bridge-tmp-*` files first, under the lock,
before the backup and whether or not there is anything to apply: a converged tree
is the normal state, and a leftover there would otherwise never be cleaned up.

A lock file prevents overlapping `--execute` runs. The pid is written into
`bridge.lock.<pid>.tmp` FIRST and the claim is a hard link of that file onto
`bridge.lock`, so the lock is atomic and is never visible without its owner's
pid; a lock created empty and filled in afterwards could be read in between and
mistaken for a stale one. A lock whose owner is still alive is reported, never
stolen; one left by a dead owner (or with no usable pid) is reclaimed once,
loudly, because otherwise a killed run wedges the tool for good. The reclaim
unlinks only the inode it judged stale, and the teardown only the inode it
created — so a run never removes another run's lock, and losing either race is
reported as "already running" rather than retried. Anything at the lock path
that is not a regular file (a directory, a symlink) makes the state directory
unusable. A dry run takes no lock and creates no state directory at all — it
writes nothing, and it must keep working while a lock is in place, since it is
the command that diagnoses one. The state directory may not live inside the
sessions root: every backup would copy the previous ones. That comparison
resolves symlinks on both sides, so a `--state-dir` reached through a link
cannot slip past it; nothing else ever resolves a path.

Exit codes: 0 done, 1 applied with failures, 2 could not start (bad paths, lock
held, unusable state directory, unwritable LaunchAgents directory), 3 wrong
platform for the agent commands, 4 `launchctl bootstrap` failed.

## Automation (macOS)

`install-agent` writes `~/Library/LaunchAgents/com.claude-code-multi-account-bridge.plist`
with `WatchPaths` on the `claude-code-sessions` root, every ACCOUNT directory and
every current leaf, `ThrottleInterval 5` and `StartInterval 10`, running
`/usr/bin/python3 <repo>/bridge.py sync --execute --if-changed` (a fingerprint
fast path; a full sync at least every 60 s — an account switch inside the app
writes nothing to disk, so no WatchPaths event fires). All three levels are needed because launchd
fires on a change to a watched directory's own contents: a new account changes
the root, but a new org folder under an existing account only changes that
account's directory. Sync re-discovers leaves on every run regardless, and the
installer can be re-run to refresh the watch list. Install always boots the label
out before bootstrapping (it can be loaded from a plist that has since moved),
and a failed bootstrap DELETES the plist it just wrote — the old copy is already
booted out, so a surviving plist would claim an installation that is not running.
The job runs a COPY of the script placed in the state directory, never the
checkout: launchd starts `/usr/bin/python3` without the user's privacy grants,
so a script under `~/Documents`/`~/Desktop`/`~/Downloads` fails with "Operation
not permitted" on every run (measured on macOS 26; found only by watching a real
launchd run — every static check was green). `install-agent` refreshes the copy.
`uninstall-agent` boots it out and deletes the plist; both refuse on a non-macOS
platform without invoking launchctl at all, and both report an unwritable
LaunchAgents directory as a clean exit 2 rather than a traceback — install says
so BEFORE booting anything out when it is the directory it cannot create, and
says the previous job has already been booted out when it is the plist it cannot
write.

`status` prints the leaves, per-leaf counts, the counts line (always), the drift
(what a sync would change), and the agent state — which checks BOTH that the
plist exists and that `launchctl print` succeeds, since either half alone is a
misleading answer. A job launchd holds with no plist on disk is reported as "not
loaded properly", not as "loaded": it will not come back after a logout and there
is nothing on disk to bring it back. Off macOS there is no launchd to ask, so the
answer is "unknown" and the uid is never looked up (`os.getuid` does not exist on
Windows). `status` logs exactly the same parse-failure / blocked / blocked-leaf /
ignored / id-mismatch warnings a sync would, so a read-only look is never quieter
than a write.

## Non-goals

- claude.ai chat history (server-side per account).
- Sign-in material, server-downloaded manifests, the bypass-permissions
  settings and `archived-sessions.idx` (see the README's "Not in scope").
- Live refresh of a running app. The app shows merged records on its next account
  load or relaunch.

## Constraints

- Python 3.9+ standard library only (`/usr/bin/python3` on macOS ships 3.9).
- Account-agnostic: no account ids, user names, home paths or emails anywhere in the
  repo. Tests build synthetic trees under a temp dir.
- Never writes through a symlink. In the session folders the only delete is a
  `local_<uuid>.json` superseded by a tombstone, guarded again on the filename at
  delete time. Beyond that the tool removes only its own leftovers: stale
  `.bridge-tmp-*` files in EVERY leaf it read (regular files with that prefix
  only, never a directory or symlink, and only on an `--execute` run, which is
  the only one that holds the lock — never in `status` or a dry run), backups
  past the newest 10 (never a symlink), its lock file — and only the one it
  claimed itself — and its plist on `uninstall-agent`.
- The agent's two log files under `~/Library/Logs` are append-only and rotated by
  nobody; a long-lived install needs an external truncation or newsyslog rule.
