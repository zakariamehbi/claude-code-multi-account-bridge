# claude-code-multi-account-bridge

One machine, two Claude accounts, one session list.

## The problem

The Claude desktop app keeps its **Code** tab session list *per signed-in
account*. Each account gets its own folder:

```
<userData>/claude-code-sessions/<accountUuid>/<orgUuid>/
    local_<sessionId>.json     one record per session (title, cwd, isArchived, ...)
    deleted_<sessionId>        tombstone written when a session is deleted
    scheduled-tasks.json       per-account — not ours
    backlog/, spaces.json      per-account — not ours
```

The transcripts those records point at are **already shared** across accounts
(they live outside `userData`, keyed by session id), so a record copied into
another account's folder is fully resumable. Only the *list* is per account.

So if you alternate between two accounts — for example when one hits its weekly
usage limit — the sidebar you see depends on which account you happen to be
signed into. Work started on one account is invisible from the other, even
though the conversation itself is right there on disk.

This tool merges those lists.

## What it does

`sync` converges every `<account>/<org>` folder to the same set of records:

1. **Discover** the leaf folders — exactly two levels under
   `claude-code-sessions`, both directory names canonical UUIDs, both real
   directories.
2. **Union by session id**, newest copy wins (see the merge rules below).
3. **Tombstones win** — a delete on one account is a delete everywhere.
4. **Write** the winner into every folder that lacks it or holds a different
   copy, and nothing else.
5. **Report everything it could not act on** — a path it can neither read nor
   write, or a whole folder it cannot read or write, is counted, logged and left
   alone, so the plan converges instead of proposing the same impossible write on
   every run.

A missing or mistyped sessions root is an error, not "nothing to do": `sync` and
`status` both name the path and exit non-zero rather than reporting an empty,
in-sync tree.

The desktop app reads its folder when an account loads and does not watch it, so
a merged list shows up on your next account switch or app relaunch — not
mid-session.

## Install

No dependencies, no build, no install step. Python 3.9+ standard library only —
which is exactly what macOS already ships at `/usr/bin/python3`.

```sh
git clone <this repo>
cd claude-code-multi-account-bridge
python3 bridge.py status
```

## Usage

```sh
python3 bridge.py status            # what exists, and what a sync would change
python3 bridge.py sync              # dry run (the default) — writes nothing
python3 bridge.py sync --execute    # take a backup, then apply
python3 bridge.py sync --execute -v # ... with debug logging
```

Every run prints its counts — what the plan says should happen, and, when
executing, what actually happened:

```
sync (execute): leaves=2 records_seen=137 copies_written=11 records_deleted=2 tombstones_copied=2 parse_failures=0 blocked=0 blocked_leaves=0 ignored=0 id_mismatches=0
backup: ~/.claude-code-multi-account-bridge/backups/20260101T120000000000Z skipped=0
applied: copies_written=11 tombstones_copied=2 records_deleted=2 skipped=0 errors=0
```

`status` prints the same counts (as a `counts:` line) whether or not there is
anything to do — a blocked path or an unreadable folder can sit in a tree that
needs no writes at all, and "every leaf is in sync" must not hide it.

The plan line (`sync (...)`) says what *should* happen; the `applied:` line says
what *did*. Nothing the tool declines to do is ever invisible:

| Count | Line | Meaning |
| --- | --- | --- |
| `records_seen` | plan | distinct live session ids with at least one readable, parsable copy; tombstoned ids are not counted, and neither is an id whose only copies are blocked (two names differing only in case, say) |
| `copies_written` / `tombstones_copied` / `records_deleted` | both | the work itself |
| `parse_failures` | plan | records that are not a JSON object: never chosen as a winner, never deleted except by a tombstone |
| `blocked` | plan | paths that can be neither read nor written — a directory or symlink wearing a record's name, an unreadable file, one over the 8 MB cap, or one of two names for the same id that differ only in case. No write and no delete is planned for them, so the plan still converges |
| `blocked_leaves` | plan | whole account/org folders that cannot be listed, or cannot be written into (no write or execute permission). Nothing is written into them and nothing in them is deleted; the records they did yield still take part as sources |
| `ignored` | plan | names that only look like ours (`local_`/`deleted_` followed by something that is not a canonical UUID). Listed with `-v` |
| `id_mismatches` | plan | the record's own `id` field disagrees with its filename. The filename wins (it is what the app reads the folder by) and the record is still synced |
| `skipped` | applied | a write refused at the last moment because the destination changed between planning and writing, or a delete refused by a guard. The copy on disk is kept, the reason is logged, and the exit code stays 0 — nothing failed, and the next run converges |
| `skipped` | `backup:` line | a DIFFERENT counter with the same name: paths deliberately left out of the backup because they could not be read (each one logged). Not related to the `applied` one above |
| `errors` | applied | something failed outright — also makes the exit code non-zero. A copy that landed but whose mtime could not be restored counts in BOTH `copies_written` and `errors`: the bytes are there, but that copy now looks newer than every other and would win the next merge |

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | done (or nothing to do) |
| 1 | it started but did not finish cleanly — a write, a delete or the backup failed (`errors` above) |
| 2 | it could not start, or could not finish an agent command: bad `--user-data`/`--state-dir`, another run holds the lock, the state directory is not usable (not writable, or something that is not a regular file sits at the lock path), or the `LaunchAgents` directory cannot be written |
| 3 | `install-agent` / `uninstall-agent` on a non-macOS platform |
| 4 | `launchctl bootstrap` failed (the plist is removed again) |

Useful flags:

| Flag | Subcommands | Meaning |
| --- | --- | --- |
| `--user-data DIR` | `sync`, `status`, `install-agent` | override the platform `userData` directory |
| `--state-dir DIR` | `sync`, `status`, `install-agent` | override `~/.claude-code-multi-account-bridge` (backups + lock). Must be outside the sessions tree |
| `--agents-dir DIR` | `status`, `install-agent`, `uninstall-agent` | override `~/Library/LaunchAgents` |
| `-v` | all | debug logging. Counts go to stdout, every log line to stderr |

`userData` is discovered per platform and can always be overridden:

| Platform | Default |
| --- | --- |
| macOS | `~/Library/Application Support/Claude` |
| Windows | `%APPDATA%\Claude` |
| Linux | `~/.config/Claude` |

## Automatic syncing (macOS)

```sh
python3 bridge.py install-agent     # write + load the LaunchAgent
python3 bridge.py status            # ... says whether it is loaded
python3 bridge.py uninstall-agent   # unload it and delete the plist
```

`install-agent` writes
`~/Library/LaunchAgents/com.claude-code-multi-account-bridge.plist` with
`WatchPaths` on the sessions root, **every account folder** and every current
leaf folder, `ThrottleInterval 5` and a `StartInterval 60` floor (a run every
minute even when nothing changed on disk — switching accounts inside the app
writes nothing, and a converged tree costs a run nothing), running
`/usr/bin/python3 <repo>/bridge.py sync --execute`. All three levels are watched because launchd fires on a change
to a watched folder's *own* contents: a new account shows up as a change to the
root, but a new **org** folder under an account that already exists only shows
up as a change to that account folder. `sync` re-discovers leaves on every run
regardless, and re-running `install-agent` refreshes the watch list. Logs land
in `~/Library/Logs/com.claude-code-multi-account-bridge.{out,err}.log`.

Those two log files are **append-only and never rotated** — launchd does not
rotate them and neither does this tool. A quiet run writes one line, so they
grow slowly, but if you leave the agent installed for months, truncate them
yourself (`: > ~/Library/Logs/com.claude-code-multi-account-bridge.out.log`) or
add them to a `newsyslog.d` rule.

`status` also reports the agent, and checks **both** halves: whether the plist
is on disk and whether launchd actually has the job. A plist that is present but
not loaded does nothing while looking installed, and a job launchd holds with no
plist on disk will not come back after a logout — that one reads `not loaded
properly - launchd has the job but there is no plist at ...`, because it is a
broken install, not a working one. Away from macOS there is no launchd to ask, so
the answer is `unknown` rather than a verdict.

## Merge rules

For each session id found in any leaf folder:

1. **Newest `mtime` wins.** Archiving a session rewrites the file — bumping its
   mtime — without necessarily bumping `lastActivityAt`, so mtime is the
   primary key. That is what makes "archived on one account" propagate to the
   other.
2. **Tie on mtime → larger `lastActivityAt`.**
3. **Still tied → larger SHA-256 of the file bytes.** Pure determinism: the
   same tree always yields the same answer, whatever order the folders were
   read in.
4. **A tombstone anywhere wins over everything.** `deleted_<id>` in any folder
   removes `local_<id>.json` from *every* folder and is copied byte-for-byte
   into every folder that lacks it. Session ids are UUIDs and are never reused,
   so a delete is final.
5. **A record that does not parse as a JSON object is never chosen** — it is
   reported on stderr and counted in `parse_failures`. It is never deleted
   *except* by a tombstone: a tombstone is a decision about the session, not
   about the bytes, so `deleted_<id>` removes an unparsable `local_<id>.json`
   like any other. If another folder holds a valid copy of that id, the valid
   copy replaces it.
6. **Case is identity, but the name on disk is the address.** `local_<UUID>.json`
   and `local_<uuid>.json` are the *same file* on a Mac's default filesystem and
   *two files* on a case-sensitive one, so ids are compared lower-cased while
   every path is built from the name the folder actually listed. If one folder
   really does hold two names for one id — only possible on a case-sensitive
   volume — there is no way to know which one the app reads, so both are counted
   in `blocked` and neither is touched.
7. **Converged means zero writes.** Winners are written with the winner's
   mtime, so a second run finds nothing to do.

## Safety

- **Dry run is the default.** `sync` alone only reports; writing needs
  `--execute`. A dry run takes no lock and creates no state directory — it is
  the one command that still works while something else holds the lock, which
  is how you find out what is going on.
- **A backup precedes the first write of a run.** The whole
  `claude-code-sessions` tree is copied to
  `~/.claude-code-multi-account-bridge/backups/<UTC timestamp>/`, keeping the
  newest 10, with every file's bytes and exact mtime preserved. No backup is
  taken for a dry run, or when there is nothing to do. Anything this tool already
  reported as **unreadable** — a record, a whole account/org folder, or a
  directory wearing a record's name that cannot be read either — is left out of
  the copy, named in the log and counted as `skipped` on the `backup:` line — otherwise a single
  unreadable record would fail the backup, and so the whole run, on every run for
  ever, which is the opposite of the "report it and step around it" rule those
  paths exist for. **Any other copy failure stops the run:** it removes the
  half-written backup, applies nothing and exits non-zero.
- **A symlink in the backups folder is not a backup.** One named like a
  timestamp is never chmod'd, never deleted and never counted against the
  newest-10 ring.
- **A lock file** (`~/.claude-code-multi-account-bridge/bridge.lock`) stops
  overlapping `--execute` runs; a second run exits 2 and names the file and the
  pid holding it. The pid is written into `bridge.lock.<pid>.tmp` first and the
  lock is claimed by hard-linking that file into place, so the lock is taken in
  one atomic step and is never visible without its owner's pid inside it. A lock
  whose owning process is **still alive is never stolen**. A lock left behind by
  a killed run — its pid gone, or no pid ever written — is reclaimed once, with a
  warning saying so, and only if it is still the very same file that was judged
  stale. On the way out a run removes **only the lock it claimed itself**, so it
  can never delete a lock that belongs to somebody else. If something that is not
  a regular file sits at that path, the state directory is reported as unusable
  rather than guessed about.
- **Writes are atomic** — temp file in the *destination folder* (so the rename
  cannot cross filesystems), then `rename`, then the winner's mtime is restored
  and the folder is flushed. A reader never sees a half-written file, and an
  existing destination keeps its permission bits.
- **The destination is re-checked immediately before the rename.** If the app
  wrote a newer copy in the meantime, that copy is kept, the write is refused
  and counted in `skipped`. Overwriting it would also have stamped the older
  copy's mtime on it, hiding the loss from every later run. To be honest about
  it: that check **narrows** the window, it cannot close it — there is no atomic
  "replace only if unchanged" here, so a copy written in the last sliver between
  the check and the rename is still lost. The window is microseconds wide, and
  the backup from the same run is the way back.
- **Symlinks are never followed and never written through.** A symlinked
  account or org folder is skipped during discovery; a record or tombstone path
  that is a symlink is counted in `blocked` and no write or delete is ever
  planned for it. Same for a directory wearing a record's name, and for a copy
  that cannot be read — an unreadable copy additionally blocks *every* write of
  that session id, since a copy we cannot read might be the newest one.
- **A folder it cannot use is stepped around, not fought.** An account/org
  folder with no read permission is never read from and never written into; one
  with no write (or execute) permission is still read — it may hold the newest
  copy of a session — but nothing is written into it and nothing in it is
  deleted. Both are counted in `blocked_leaves` and named in the log on every
  run. Without this the plan could never converge, which meant a full backup and
  a fresh temp file on every single run.
- **Nothing else in the session folders is touched.** Only `local_<uuid>.json`
  and `deleted_<uuid>` are read or written. `scheduled-tasks.json`, `backlog/`,
  `spaces.json` and any other file in those folders are left exactly as they
  were — bytes and timestamps.
- **What the tool removes, exhaustively.** In your session folders: a
  `local_<uuid>.json` superseded by a tombstone, and nothing else — the guard
  is re-checked at delete time, on the filename. Plus its own leftovers: stale
  `.bridge-tmp-*` files in **every** folder it read, not only the ones it is
  about to write to (only regular files with that exact prefix, never a
  directory or symlink, and only on a `sync --execute` run, which is the only
  one holding the lock — a dry run and `status` sweep nothing). That sweep runs
  before the backup and even when there is nothing else to do, because a
  converged tree is exactly where a leftover would otherwise sit for ever.
  Then: backups past the newest 10 (never a symlink), the lock file it claimed
  itself, and — on `uninstall-agent` — its plist.

## Troubleshooting

**`another run is already running (pid 1234, lock file ...); delete it if you are
sure no run is in progress`** — exactly what it says, and the message names the
file. Two ways to get it:

- A run really is in progress. Wait for it. `sync` (dry run) and `status` work
  regardless: they take no lock at all.
- **A pid that has been reused.** A killed run leaves its lock behind; the lock
  is normally reclaimed automatically, because the pid inside it belongs to
  nothing any more. But pids get recycled, and if that number has since been
  handed to some *unrelated* process, the lock looks alive and stays alive. There
  is no way to tell the two apart from the outside — a pid is all the lock has —
  so the tool refuses rather than stealing a lock that might be live. Check that
  no `bridge.py` is running (`ps ax | grep bridge.py`) and delete the file the
  message named:

  ```sh
  rm ~/.claude-code-multi-account-bridge/bridge.lock
  ```

**`cannot use the state directory ... exists and is not a regular file`** —
something other than a lock file (a folder, a symlink) is sitting at
`~/.claude-code-multi-account-bridge/bridge.lock`. The tool will not delete it:
move it out of the way yourself, or point `--state-dir` somewhere else.

**`blocked_leaves=1` and a warning naming one of your account folders** — that
folder's permissions stop the tool reading it or writing into it. Everything else
still converges. `chmod u+rwx` on the folder if you want it included; otherwise
it is left alone, run after run, and said so each time.

**`blocked=N`, drift `none`** — there is nothing to do *and* something the tool
declined to touch (a directory or symlink wearing a record's name, an unreadable
file, a record over the 8 MB cap). The counts line names the count on every run
and each path is logged; nothing will be written to or deleted at those paths
until you resolve them by hand.

**`skipped=N` on the applied line, exit 0** — the desktop app wrote a newer copy
of a record between the plan and the write, so that copy was kept and the write
was dropped. Nothing failed, and the next run (or the next `WatchPaths` trigger)
converges. The reason is logged for each one.

**`not loaded properly - launchd has the job but there is no plist at ...`** —
run `python3 bridge.py install-agent` again; it rewrites the plist and reloads
the job.

## Platform notes

The `sync` and `status` commands work anywhere a Claude `userData` directory
exists (macOS, Windows, Linux). The automation is macOS-only for now, because it
is a launchd LaunchAgent; `install-agent` refuses to run elsewhere. On Windows
or Linux, call `bridge.py sync --execute` from Task Scheduler, a systemd timer,
or cron.

## Uninstall

```sh
python3 bridge.py uninstall-agent            # unload + delete the plist
rm -rf ~/.claude-code-multi-account-bridge   # backups and lock file
rm -rf <this clone>
```

Nothing else is installed anywhere. Records already merged into your account
folders stay there — they are ordinary session records, and the app treats them
as its own.

## Not in scope

- claude.ai chat history (server-side, and genuinely per account).
- `scheduled-tasks.json`, `backlog/`, `spaces.json`, and every other
  per-account file.
- The `local-agent-mode-sessions` tree — same layout, may be added later.
- Live refresh of a running app.

## Development

```sh
/usr/bin/python3 -m unittest discover -s tests -v   # 3.9, as macOS ships it
python3 -m unittest discover -s tests -v            # whatever is newer
```

The merge is pure: `discover_leaves` → `read_leaf` → `plan_sync` → `Plan` →
`apply_plan`. `plan_sync` touches no filesystem, so most of the suite asserts on
plan objects directly. Tests build synthetic trees under a temp directory and
never read a real Claude directory; `launchctl` sits behind an injectable
runner and is never executed by a test.

## License

MIT — see [LICENSE](LICENSE).
