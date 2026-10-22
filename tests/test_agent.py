"""LaunchAgent plist generation and install/uninstall. launchctl is never run."""

import contextlib
import io
import os
import plistlib
import shutil
import sys
import tempfile
import unittest

import bridge_testlib as tl
import bridge

SCRIPT = "/synthetic/repo/bridge.py"
ROOT = "/synthetic/userData/claude-code-sessions"
LEAVES = [os.path.join(ROOT, tl.uid(1), tl.uid(11)),
          os.path.join(ROOT, tl.uid(2), tl.uid(22))]
ACCOUNTS = [os.path.join(ROOT, tl.uid(1)), os.path.join(ROOT, tl.uid(2))]

# N15: the CLI paths go through sys.platform, so they can only run on macOS.
# Everything they cover is also tested by calling install_agent /
# uninstall_agent with platform="darwin", which runs everywhere.
darwin_only = unittest.skipUnless(sys.platform == "darwin",
                                  "launchd is macOS only")


class PlistContentTest(unittest.TestCase):
    def plist(self, **kwargs):
        params = dict(script_path=SCRIPT, root=ROOT, leaves=LEAVES)
        params.update(kwargs)
        return plistlib.loads(bridge.plist_bytes(**params))

    def test_label(self):
        self.assertEqual(self.plist()["Label"], bridge.LABEL)
        self.assertEqual(bridge.LABEL, "com.claude-code-multi-account-bridge")

    def test_program_arguments_use_usr_bin_python3_and_an_absolute_script(self):
        argv = self.plist()["ProgramArguments"]
        self.assertEqual(argv, ["/usr/bin/python3", SCRIPT, "sync", "--execute", "--if-changed"])
        self.assertTrue(os.path.isabs(argv[1]))

    def test_program_arguments_carry_the_overrides_when_given(self):
        argv = self.plist(user_data="/synthetic/userData",
                          state_dir="/synthetic/state")["ProgramArguments"]
        self.assertEqual(argv, [
            "/usr/bin/python3", SCRIPT, "sync", "--execute", "--if-changed",
            "--user-data", "/synthetic/userData",
            "--state-dir", "/synthetic/state"])

    def test_watch_paths_are_the_root_the_accounts_and_every_leaf(self):
        """N10: an org folder appearing under an EXISTING account fires nothing
        unless that account directory is watched too — the root's own mtime does
        not change when a grandchild is created."""
        self.assertEqual(self.plist()["WatchPaths"], [ROOT] + ACCOUNTS + LEAVES)

    def test_an_account_directory_is_listed_once_however_many_orgs_it_has(self):
        leaves = [os.path.join(ROOT, tl.uid(1), tl.uid(11)),
                  os.path.join(ROOT, tl.uid(1), tl.uid(12))]
        watched = self.plist(leaves=leaves)["WatchPaths"]

        self.assertEqual(watched, [ROOT, os.path.join(ROOT, tl.uid(1))] + leaves)
        self.assertEqual(len(watched), len(set(watched)))

    def test_the_root_is_never_repeated_as_an_account_directory(self):
        watched = self.plist(leaves=[os.path.join(ROOT, tl.uid(1))])["WatchPaths"]
        self.assertEqual(watched, [ROOT, os.path.join(ROOT, tl.uid(1))])

    def test_throttle_interval_is_five(self):
        self.assertEqual(self.plist()["ThrottleInterval"], 5)

    def test_a_periodic_floor_runs_the_job_even_when_nothing_fires_watch_paths(self):
        """The app reads its folder on account load, which writes nothing, so a
        switch made after a quiet stretch needs a run that no WatchPaths event
        triggers. A converged tree costs a run nothing (no writes, no backup)."""
        self.assertEqual(self.plist()["StartInterval"], bridge.START_INTERVAL)

    def test_log_paths_live_under_library_logs(self):
        parsed = self.plist()
        logs = os.path.expanduser(os.path.join("~", "Library", "Logs"))
        self.assertEqual(parsed["StandardOutPath"],
                         os.path.join(logs, bridge.LABEL + ".out.log"))
        self.assertEqual(parsed["StandardErrorPath"],
                         os.path.join(logs, bridge.LABEL + ".err.log"))

    def test_it_is_a_well_formed_apple_plist_document(self):
        raw = bridge.plist_bytes(script_path=SCRIPT, root=ROOT, leaves=LEAVES)
        self.assertTrue(raw.startswith(b"<?xml"))
        self.assertIn(b"-//Apple//DTD PLIST 1.0//EN", raw)
        self.assertIn(b"<key>ThrottleInterval</key>", raw)

    def test_it_does_not_run_at_load_or_keep_alive(self):
        parsed = self.plist()
        self.assertNotIn("RunAtLoad", parsed)
        self.assertNotIn("KeepAlive", parsed)


class AgentCase(unittest.TestCase):
    """Helpers only — no tests, so nothing here runs twice per subclass."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bridge-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.user_data = os.path.join(self.base, "userData")
        self.state_dir = os.path.join(self.base, "state")
        self.agents_dir = os.path.join(self.base, "LaunchAgents")
        self.root = tl.make_root(self.user_data)
        self.leaf = tl.make_leaf(self.user_data, tl.uid(1), tl.uid(11))
        self.runner = tl.RecordingRunner()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bridge.main(list(args), runner=self.runner)
        return code, out.getvalue()

    def plist_path(self):
        return os.path.join(self.agents_dir, bridge.LABEL + ".plist")

    def install(self, **kwargs):
        """install_agent with the platform INJECTED, so this runs anywhere."""
        params = dict(agents_dir=self.agents_dir, root=self.root,
                      leaves=[self.leaf], runner=self.runner, platform="darwin",
                      state_dir=self.state_dir, echo=None)
        params.update(kwargs)
        return bridge.install_agent(**params)

    def uninstall(self, **kwargs):
        params = dict(agents_dir=self.agents_dir, runner=self.runner,
                      platform="darwin", echo=None)
        params.update(kwargs)
        return bridge.uninstall_agent(**params)

    def verbs(self):
        return [call[1] for call in self.runner.calls]


class AgentScriptCopyTest(AgentCase):
    """launchd runs /usr/bin/python3 WITHOUT the user's privacy grants, so a
    script that lives under ~/Documents, ~/Desktop or ~/Downloads fails with
    "Operation not permitted" on every run (measured on macOS 26). The agent
    therefore runs a copy placed in the state directory, which is never under a
    protected folder."""

    def source(self):
        return os.path.join(self.base, "checkout", "bridge.py")

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.dirname(self.source()))
        with open(self.source(), "wb") as handle:
            handle.write(b"# the checkout's script\n")

    def test_the_agent_runs_a_copy_inside_the_state_directory(self):
        self.assertEqual(self.install(script_path=self.source()), 0)
        with open(self.plist_path(), "rb") as handle:
            argv = plistlib.load(handle)["ProgramArguments"]
        copied = os.path.join(self.state_dir, "bridge.py")
        self.assertEqual(argv[1], copied)
        with open(copied, "rb") as handle:
            self.assertEqual(handle.read(), b"# the checkout's script\n")

    def test_reinstall_refreshes_the_copy(self):
        self.assertEqual(self.install(script_path=self.source()), 0)
        with open(self.source(), "wb") as handle:
            handle.write(b"# newer\n")
        self.assertEqual(self.install(script_path=self.source()), 0)
        with open(os.path.join(self.state_dir, "bridge.py"), "rb") as handle:
            self.assertEqual(handle.read(), b"# newer\n")

    def test_an_uncopyable_script_is_a_clean_exit_2_before_any_bootout(self):
        os.makedirs(self.state_dir)
        os.chmod(self.state_dir, 0o500)
        self.addCleanup(os.chmod, self.state_dir, 0o700)
        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            self.assertEqual(self.install(script_path=self.source()), 2)
        self.assertEqual(self.verbs(), [])
        self.assertFalse(os.path.exists(self.plist_path()))


class InstallTest(AgentCase):
    @darwin_only
    def test_install_writes_the_plist_and_bootstraps_it(self):
        code, out = self.run_cli(
            "install-agent", "--user-data", self.user_data,
            "--state-dir", self.state_dir, "--agents-dir", self.agents_dir)

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.plist_path()))
        with open(self.plist_path(), "rb") as handle:
            parsed = plistlib.loads(handle.read())
        self.assertEqual(parsed["WatchPaths"],
                         [self.root, os.path.dirname(self.leaf), self.leaf])
        # The agent runs the COPY in the state dir, never the checkout: launchd
        # has no privacy grant for ~/Documents and the like (AgentScriptCopyTest).
        self.assertEqual(parsed["ProgramArguments"][:2],
                         ["/usr/bin/python3",
                          os.path.join(self.state_dir, "bridge.py")])
        self.assertTrue(os.path.isfile(os.path.join(self.state_dir, "bridge.py")))
        domain = "gui/%d" % os.getuid()
        self.assertEqual(self.runner.calls[-1],
                         ["launchctl", "bootstrap", domain, self.plist_path()])
        self.assertIn(self.plist_path(), out)

    @darwin_only
    def test_uninstall_through_the_cli_removes_the_plist(self):
        self.install()
        self.runner.calls = []

        code, out = self.run_cli("uninstall-agent", "--agents-dir", self.agents_dir)

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.plist_path()))
        self.assertIn("removed", out)

    def test_install_is_repeatable_and_boots_out_any_previous_copy_first(self):
        self.install()
        self.runner.calls = []

        self.assertEqual(self.install(), 0)

        self.assertEqual(self.verbs(), ["bootout", "bootstrap"])

    def test_the_first_install_boots_out_before_bootstrapping_too(self):
        """N16: a job can be loaded with no plist here (a moved or deleted file),
        and bootstrap then fails with "already loaded". Booting out first is
        free when there is nothing loaded — launchctl just returns non-zero."""
        self.assertFalse(os.path.exists(self.plist_path()))

        self.assertEqual(self.install(), 0)

        self.assertEqual(self.verbs(), ["bootout", "bootstrap"])

    def test_a_failing_bootstrap_removes_the_plist_and_hints_at_uninstall(self):
        """N16/P-F9a: a plist left behind after a failed bootstrap is the worst
        end state — the old job has already been booted out, so nothing is
        running, yet the file suggests something is installed."""
        self.runner.results["launchctl bootstrap"] = bridge.RunResult(
            returncode=5, stderr="Bootstrap failed: 5: Input/output error")

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code = self.install()

        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(self.plist_path()))
        joined = "\n".join(logs.output)
        self.assertIn("removed", joined)
        self.assertIn("uninstall-agent", joined)

    def test_uninstall_boots_out_and_deletes_the_plist(self):
        self.install()
        self.runner.calls = []
        lines = []

        code = self.uninstall(echo=lines.append)

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.plist_path()))
        self.assertEqual(self.runner.calls[0], [
            "launchctl", "bootout", "gui/%d/%s" % (os.getuid(), bridge.LABEL)])
        self.assertIn("removed", " ".join(lines))

    def test_uninstall_is_clean_when_nothing_is_installed(self):
        lines = []
        code = self.uninstall(echo=lines.append)
        self.assertEqual(code, 0)
        self.assertIn("no plist", " ".join(lines))

    def test_install_refuses_on_a_non_macos_platform(self):
        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            code = self.install(platform="linux")

        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(self.plist_path()))
        self.assertEqual(self.runner.calls, [])

    def test_uninstall_refuses_on_a_non_macos_platform(self):
        """N21: the guard exists on both sides, and neither may run launchctl."""
        with self.assertLogs(bridge.LOGGER, level="ERROR"):
            code = self.uninstall(platform="linux")

        self.assertNotEqual(code, 0)
        self.assertEqual(self.runner.calls, [])


class UnwritableAgentsDirTest(AgentCase):
    """R2-NB5: both commands tracebacked out of an unwritable agents directory.

    A traceback is not a diagnosis, and install had already booted the previous
    job out by the time it failed — so nothing was running and nothing said so.
    """

    def cage(self):
        cage = os.path.join(self.base, "cage")
        os.makedirs(cage)
        os.chmod(cage, 0o500)
        self.addCleanup(os.chmod, cage, 0o700)
        return cage

    def test_install_cannot_create_the_directory_is_a_clean_exit_two(self):
        agents_dir = os.path.join(self.cage(), "LaunchAgents")

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code = self.install(agents_dir=agents_dir)

        self.assertEqual(code, 2)
        self.assertIn(agents_dir, "\n".join(logs.output))
        self.assertFalse(os.path.exists(agents_dir))
        # nothing was booted out, so nothing was disturbed either
        self.assertEqual(self.runner.calls, [])

    def test_install_cannot_write_the_plist_says_the_job_was_booted_out(self):
        os.makedirs(self.agents_dir)
        os.chmod(self.agents_dir, 0o500)
        self.addCleanup(os.chmod, self.agents_dir, 0o700)

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code = self.install()

        self.assertEqual(code, 2)
        joined = "\n".join(logs.output)
        self.assertIn(self.plist_path(), joined)
        self.assertIn("booted out", joined)
        self.assertFalse(os.path.exists(self.plist_path()))
        # it must not claim to have loaded anything
        self.assertEqual(self.verbs(), ["bootout"])

    def test_uninstall_cannot_delete_the_plist_is_a_clean_exit_two(self):
        self.install()
        os.chmod(self.agents_dir, 0o500)
        self.addCleanup(os.chmod, self.agents_dir, 0o700)
        lines = []

        with self.assertLogs(bridge.LOGGER, level="ERROR") as logs:
            code = self.uninstall(echo=lines.append)

        self.assertEqual(code, 2)
        self.assertIn(self.plist_path(), "\n".join(logs.output))
        self.assertTrue(os.path.exists(self.plist_path()))
        self.assertNotIn("removed", " ".join(lines))


class AgentStateTest(unittest.TestCase):
    """N21: agent_is_loaded is a three-way answer, not a boolean."""

    def test_a_zero_exit_from_launchctl_print_means_loaded(self):
        runner = tl.RecordingRunner(
            {"launchctl print": bridge.RunResult(returncode=0)})
        self.assertIs(bridge.agent_is_loaded(runner=runner, platform="darwin"),
                      True)

    def test_a_non_zero_exit_means_not_loaded(self):
        runner = tl.RecordingRunner(
            {"launchctl print": bridge.RunResult(returncode=113)})
        self.assertIs(bridge.agent_is_loaded(runner=runner, platform="darwin"),
                      False)

    def test_launchctl_missing_altogether_is_unknown_not_false(self):
        def missing(argv, check=False):
            raise OSError(2, "No such file or directory: 'launchctl'")

        self.assertIsNone(bridge.agent_is_loaded(runner=missing,
                                                 platform="darwin"))

    def test_a_runner_that_rejects_the_argv_is_unknown_too(self):
        def bad(argv, check=False):
            raise ValueError("embedded null byte")

        self.assertIsNone(bridge.agent_is_loaded(runner=bad, platform="darwin"))

    def test_a_non_macos_platform_is_unknown_and_asks_launchctl_nothing(self):
        """R2-B3: os.getuid() does not exist on Windows, and it used to be
        called inside a try that catches only OSError/ValueError — so `status`
        tracebacked there instead of saying it could not tell."""
        runner = tl.RecordingRunner(
            {"launchctl print": bridge.RunResult(returncode=0)})

        for platform in ("win32", "cygwin", "linux"):
            with self.subTest(platform=platform):
                self.assertIsNone(bridge.agent_is_loaded(runner=runner,
                                                         platform=platform))
        self.assertEqual(runner.calls, [])

    def test_the_uid_is_never_looked_up_off_darwin(self):
        """The traceback came from os.getuid itself, so pin the call site."""
        original = os.getuid

        def explode():
            raise AttributeError("module 'os' has no attribute 'getuid'")

        os.getuid = explode
        self.addCleanup(setattr, os, "getuid", original)

        self.assertIsNone(bridge.agent_is_loaded(runner=tl.RecordingRunner(),
                                                 platform="win32"))


if __name__ == "__main__":
    unittest.main()
