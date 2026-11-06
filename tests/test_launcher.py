"""Cross-platform discovery/launch tests; vendor inference is never requested."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from support import ROOT, client as acp, constants, launcher


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opencode-discovery-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.home_patch = patch.object(launcher.Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        which_patch = patch.object(launcher.shutil, "which", return_value=None)
        self.which = which_patch.start()
        self.addCleanup(which_patch.stop)

    def executable(self, relative):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("offline discovery fixture", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def test_explicit_path_with_spaces_is_one_executable(self):
        path = str(self.home / "Tools with spaces/opencode.exe")
        os.environ["HERMES_OPENCODE_ACP_COMMAND"] = path
        self.assertEqual(constants.opencode_command(), path)
        self.which.assert_not_called()

    def test_tilde_override_expands_home(self):
        with patch.object(launcher.Path, "expanduser", return_value=self.home / "opencode"):
            os.environ["HERMES_OPENCODE_ACP_COMMAND"] = "~/opencode"
            self.assertEqual(constants.opencode_command(), str(self.home / "opencode"))

    def test_path_selection_precedes_fallback_directories(self):
        self.which.return_value = str(self.home / "selected/opencode")
        self.executable(".opencode/bin/opencode")
        self.assertEqual(constants.opencode_command(), self.which.return_value)

    def test_posix_user_install_is_discovered_without_path(self):
        expected = self.executable(".opencode/bin/opencode")
        with patch.object(launcher, "PLATFORM", "linux"):
            self.assertEqual(constants.opencode_command(), expected)

    def test_bun_user_install_is_discovered_without_path(self):
        expected = self.executable(".bun/bin/opencode")
        with patch.object(launcher, "PLATFORM", "darwin"):
            self.assertEqual(constants.opencode_command(), expected)

    def test_both_macos_homebrew_locations_are_supported(self):
        for location in ("/opt/homebrew/bin/opencode", "/usr/local/bin/opencode"):
            expected = Path(location)
            with self.subTest(location=location), patch.object(launcher, "PLATFORM", "darwin"), \
                    patch.object(launcher.Path, "is_file", lambda path: path == expected), \
                    patch.object(launcher.os, "access", return_value=True):
                self.assertEqual(constants.opencode_command(), str(expected))

    def test_windows_npm_cmd_is_discovered(self):
        os.environ["APPDATA"] = str(self.home / "Roaming")
        expected = self.executable("Roaming/npm/opencode.cmd")
        with patch.object(launcher, "PLATFORM", "win32"):
            self.assertEqual(constants.opencode_command(), expected)

    def test_windows_native_npm_package_is_discovered(self):
        os.environ["APPDATA"] = str(self.home / "Roaming")
        expected = self.executable("Roaming/npm/node_modules/@opencode/cli/bin/opencode.exe")
        with patch.object(launcher, "PLATFORM", "win32"):
            self.assertEqual(constants.opencode_command(), expected)

    def test_windows_scoop_is_discovered(self):
        expected = self.executable("scoop/shims/opencode.exe")
        with patch.object(launcher, "PLATFORM", "win32"):
            self.assertEqual(constants.opencode_command(), expected)

    def test_windows_bun_native_package_is_discovered(self):
        expected = self.executable(".bun/install/global/node_modules/@opencode/cli/bin/opencode.exe")
        with patch.object(launcher, "PLATFORM", "win32"):
            self.assertEqual(constants.opencode_command(), expected)

    def test_windows_winget_link_is_discovered(self):
        os.environ["LOCALAPPDATA"] = str(self.home / "Local")
        expected = self.executable("Local/Microsoft/WinGet/Links/opencode.exe")
        with patch.object(launcher, "PLATFORM", "win32"):
            self.assertEqual(constants.opencode_command(), expected)

    def test_missing_install_returns_bare_name_for_actionable_launch_error(self):
        with patch.object(launcher.Path, "is_file", return_value=False):
            self.assertEqual(constants.opencode_command(), "opencode")


class LaunchTests(unittest.TestCase):
    def test_posix_argv_keeps_spaces_unicode_and_metacharacters_literal(self):
        command = "/operator tools/雪 & tools/opencode"
        with patch.object(launcher, "PLATFORM", "darwin"):
            argv, flags = launcher.launch_command(command, ["acp"])
        self.assertEqual(argv, [command, "acp"])
        self.assertEqual(flags, {"start_new_session": True})

    def test_windows_native_executable_does_not_use_a_shell(self):
        command = str(Path("C:/operator tools/雪 & tools/opencode.exe"))
        with patch.object(launcher, "PLATFORM", "win32"):
            argv, flags = launcher.launch_command(command, ["acp"])
        self.assertEqual(argv, [command, "acp"])
        self.assertNotIn("executable", flags)
        self.assertNotIn("shell", flags)
        self.assertNotIn("start_new_session", flags)

    def test_windows_bare_name_resolves_cmd_suffix_before_launch(self):
        shim = str(Path("C:/operator tools/opencode.cmd"))
        with patch.object(launcher, "PLATFORM", "win32"), \
                patch.object(launcher.shutil, "which", return_value=shim):
            argv, flags = launcher.launch_command("opencode", ["acp"])
        self.assertIn('"' + shim + '" "acp"', argv)
        self.assertIn("executable", flags)

    def test_batch_tokens_are_quoted_and_autorun_expansion_disabled(self):
        command = str(Path("C:/Tools & Helpers (v2)/opencode.cmd"))
        with patch.object(launcher, "PLATFORM", "win32"):
            argv, flags = launcher.launch_command(command, ["acp"])
        self.assertIsInstance(argv, str)
        self.assertIn('/d /s /v:off /c ""' + command + '" "acp""', argv)
        self.assertNotIn("shell", flags)

    def test_batch_percent_expansion_quotes_and_control_characters_are_rejected(self):
        for token in ("%UNEXPECTED%", 'bad"quote', "bad\nline", "bad\rline", "bad\x00byte"):
            with self.subTest(token=repr(token)), patch.object(launcher, "PLATFORM", "win32"), \
                    self.assertRaisesRegex(ValueError, "native opencode.exe"):
                launcher.launch_command("opencode.cmd", [token])

    def test_native_windows_percent_filename_does_not_need_shell_expansion(self):
        command = str(Path("C:/100% local/opencode.exe"))
        with patch.object(launcher, "PLATFORM", "win32"):
            argv, _ = launcher.launch_command(command, ["acp"])
        self.assertEqual(argv, [command, "acp"])

    def test_powershell_scripts_do_not_trigger_execution_policy_bypass(self):
        with patch.object(launcher, "PLATFORM", "win32"), self.assertRaisesRegex(ValueError, "PowerShell"):
            launcher.launch_command("opencode.ps1", ["acp"])

    def test_failed_windows_job_setup_reaps_process_and_closes_streams(self):
        proc = MagicMock()
        job = MagicMock()
        job.attach_and_resume.side_effect = OSError("synthetic job failure")
        with patch.object(launcher, "PLATFORM", "win32"), \
                patch.object(launcher, "launch_command", return_value=(["opencode.exe", "acp"], {})), \
                patch.object(launcher.subprocess, "Popen", return_value=proc), \
                patch.object(launcher, "_WindowsJob", return_value=job), \
                self.assertRaises(OSError):
            launcher.ProcessTree("opencode.exe", ["acp"], cwd=str(ROOT), env={})
        proc.kill.assert_called_once()
        proc.wait.assert_called_once_with(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close.assert_called_once()
        job.close.assert_called_once()

    def test_failed_windows_job_creation_never_spawns_a_process(self):
        with patch.object(launcher, "PLATFORM", "win32"), \
                patch.object(launcher.subprocess, "Popen") as spawn, \
                patch.object(launcher, "_WindowsJob", side_effect=OSError("synthetic job failure")), \
                self.assertRaises(OSError):
            launcher.ProcessTree("opencode.exe", ["acp"], cwd=str(ROOT), env={})
        spawn.assert_not_called()

    def test_windows_process_is_suspended_until_job_assignment(self):
        job, events = MagicMock(), []
        def create_job():
            events.append("job")
            return job
        def spawn(*args, **kwargs):
            events.append("spawn")
            self.assertTrue(kwargs["creationflags"] & 0x00000004)
            return MagicMock()
        job.attach_and_resume.side_effect = lambda pid: events.append("attach-and-resume")
        with patch.object(launcher, "PLATFORM", "win32"), \
                patch.object(launcher, "_WindowsJob", side_effect=create_job), \
                patch.object(launcher.subprocess, "Popen", side_effect=spawn):
            owner = launcher.ProcessTree("opencode.exe", ["acp"], cwd=str(ROOT), env={})
            owner.stop()
            owner.close()
        self.assertEqual(events, ["job", "spawn", "attach-and-resume"])
        job.stop.assert_called_once()
        job.close.assert_called_once()

    def test_windows_home_environment_survives_but_credentials_do_not(self):
        values = {"HOMEDRIVE": "C:", "HOMEPATH": "\\Users\\test", "APPDATA": "test-roaming",
                  "LOCALAPPDATA": "test-local", "SYSTEMROOT": "test-windows", "PATHEXT": ".EXE;.CMD",
                  "OPENAI_API_KEY": "SYNTHETIC_NOT_A_CREDENTIAL", "HERMES_YOLO_MODE": "1"}
        with patch.dict(os.environ, values, clear=True):
            actual = acp.child_environment()
        self.assertEqual(actual, {key: value for key, value in values.items()
                                  if key not in ("OPENAI_API_KEY", "HERMES_YOLO_MODE")})


class PortableStdioTests(unittest.TestCase):
    def complete(self, item, text="synthetic offline request"):
        self.addCleanup(item.close)
        return item.chat.completions.create(model=constants.DEFAULT_MODEL,
                                           messages=[{"role": "user", "content": text}], timeout=10)

    def test_unicode_protocol_round_trip(self):
        marker = "ACP café — 雪 👋"
        item = acp.OpenCodeACPClient(command=sys.executable, args=[str(ROOT / "tests/fake_acp.py"), "unicode"])
        self.assertEqual(self.complete(item, marker).choices[0].message.content, marker)

    def test_stdio_and_workspace_paths_with_spaces_and_unicode(self):
        with tempfile.TemporaryDirectory(prefix="opencode ACP café Ω ") as directory:
            folder = Path(directory)
            server = folder / "fake server.py"
            shutil.copyfile(ROOT / "tests/fake_acp.py", server)
            item = acp.OpenCodeACPClient(command=sys.executable, args=[str(server)])
            item.workspace = directory
            self.assertEqual(self.complete(item).choices[0].message.content, "FAKE_ACP_OK")

    @unittest.skipUnless(sys.platform == "win32", "requires a real Windows cmd.exe and process API")
    def test_real_windows_batch_wrappers_with_spaces_unicode_and_metacharacters(self):
        with tempfile.TemporaryDirectory(prefix="opencode ACP café Ω & (test) ! ") as directory:
            folder = Path(directory)
            shutil.copyfile(ROOT / "tests/fake_acp.py", folder / "fake_acp.py")
            for extension in ("cmd", "bat"):
                with self.subTest(extension=extension):
                    wrapper = folder / ("opencode." + extension)
                    wrapper.write_text('@echo off\nif not "%~1"=="acp" exit /b 97\n"' + sys.executable +
                                       '" "%~dp0fake_acp.py" normal\n', encoding="utf-8")
                    item = acp.OpenCodeACPClient(command=str(wrapper))
                    self.assertEqual(self.complete(item).choices[0].message.content, "FAKE_ACP_OK")

    @unittest.skipUnless(sys.platform == "win32", "requires real Windows Job Objects")
    def test_windows_job_stops_descendants_without_killing_unrelated_process(self):
        if sys.platform != "win32":
            self.skipTest("Windows only")
        import ctypes
        from ctypes import wintypes

        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        def reap_unrelated():
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=5)
        self.addCleanup(reap_unrelated)
        item = acp.OpenCodeACPClient(command=sys.executable, args=[str(ROOT / "tests/fake_acp.py"), "descendant"])
        reply = self.complete(item).choices[0].message.content
        pid = int(reply.removeprefix("FAKE_CHILD_PID:"))
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        handle = kernel.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | TERMINATE
        if handle:
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
            finally:
                kernel.TerminateProcess(handle, 1)  # reap the fixture even if the assertion fails
                kernel.CloseHandle(handle)
        else:
            self.assertIn(ctypes.get_last_error(), (87, 1168), "cannot establish that the owned child exited")
        self.assertIsNone(unrelated.poll(), "cleanup must not target unrelated user processes")


if __name__ == "__main__":
    unittest.main()
