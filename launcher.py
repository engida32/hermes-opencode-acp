"""Portable operator-owned CLI discovery, launch quoting and process cleanup.

Windows uses a private, unnamed Job Object so stopping a wrapper also stops its
children. No shell processes or commands supplied by a model are ever accepted.
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
from typing import Any

PLATFORM = sys.platform


def resolve_opencode():
    """Respect an explicit executable/PATH before common GUI installation paths."""
    override = os.environ.get("HERMES_OPENCODE_ACP_COMMAND", "").strip()
    if override:
        return str(Path(override).expanduser())
    found = shutil.which("opencode")
    if found:
        return found
    home = Path.home()
    directories = [home / ".bun/bin", home / ".opencode/bin", home / ".local/bin"]
    if PLATFORM == "win32":
        directories.extend([
            home / ".bun/install/global/node_modules/@opencode/cli/bin",
            home / "scoop/shims",
        ])
        appdata = os.environ.get("APPDATA")
        if appdata:
            npm = Path(appdata) / "npm"
            directories.extend([npm, npm / "node_modules/@opencode/cli/bin"])
        local = os.environ.get("LOCALAPPDATA")
        if local:
            directories.append(Path(local) / "Microsoft/WinGet/Links")
        names = ("opencode.exe", "opencode.cmd", "opencode.bat", "opencode")
    else:
        directories.extend([Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")])
        names = ("opencode",)
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return "opencode"


def launch_command(command, args) -> tuple[list[str] | str, dict[str, Any]]:
    """Return Popen's argv/options without shell=True or POSIX shell parsing.

    npm's Windows .cmd/.bat launchers need cmd.exe. Quote every token, disable
    AutoRun/delayed expansion, and refuse percent expansion/control characters.
    An executable path containing '%' can instead name the native .exe directly.
    """
    command = os.fspath(command)
    args = [os.fspath(arg) for arg in args]
    if PLATFORM != "win32":
        return [command, *args], {"start_new_session": True}
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    # Resolve bare names before inspecting their suffix: PATH may select a shim.
    if not os.path.dirname(command):
        command = shutil.which(command) or command
    suffix = Path(command).suffix.lower()
    if suffix == ".ps1":
        raise ValueError("Select OpenCode's native .exe or .cmd launcher, not a PowerShell .ps1 script")
    if suffix not in (".cmd", ".bat"):
        return [command, *args], flags
    tokens = [command, *args]
    if any(any(char in token for char in ('%', '"', '\r', '\n', '\x00')) for token in tokens):
        raise ValueError("Unsafe expansion in Windows batch launcher; set HERMES_OPENCODE_ACP_COMMAND to the native opencode.exe instead")
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("SystemRoot") or r"C:\Windows"
    cmd = str(Path(system_root) / "System32/cmd.exe")
    # A string is deliberate: list2cmdline must not re-escape /c's nested quotes.
    payload = " ".join('"' + token + '"' for token in tokens)
    line = subprocess.list2cmdline([cmd]) + ' /d /s /v:off /c "' + payload + '"'
    return line, {**flags, "executable": cmd}


class _WindowsJob:
    """Own only the processes assigned to this non-inheritable Windows job."""
    _kernel: Any
    _handle: Any
    _ctypes: Any
    _thread_entry: Any

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Windows process jobs require a Windows Python runtime")
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32First.restype = wintypes.BOOL
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        self._ctypes, self._thread_entry = ctypes, ThreadEntry
        self._kernel, self._handle = kernel, None
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def attach_and_resume(self, pid):
        kernel, ctypes = self._kernel, self._ctypes
        process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(process)
        # Popen closes the primary thread handle. Since CREATE_SUSPENDED kept
        # the child from running, it has one thread and cannot spawn descendants
        # before job assignment. Use documented APIs to find/resume that thread.
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = self._thread_entry()
            entry.dwSize = ctypes.sizeof(entry)
            available = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.th32OwnerProcessID == pid:
                    thread = kernel.OpenThread(0x0002, False, entry.th32ThreadID)  # SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel.ResumeThread(thread) != 1:
                            raise OSError("Could not resume the newly contained OpenCode process")
                        return
                    finally:
                        kernel.CloseHandle(thread)
                available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("Could not find the suspended OpenCode process thread")
        finally:
            kernel.CloseHandle(snapshot)

    def stop(self):
        if self._handle:
            self._kernel.TerminateJobObject(self._handle, 1)

    def close(self):
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


class ProcessTree:
    """A contained ACP process plus its owned process group/job, not a sandbox."""
    def __init__(self, command, args, *, cwd, env):
        argv, options = launch_command(command, args)
        self._lock, self._job = threading.Lock(), None
        # GUI backends may omit Homebrew/Bun's bin from PATH. Their launchers can
        # need a sibling runtime; prepend only this operator-selected directory.
        env = dict(env)
        parent = os.path.dirname(os.fspath(command))
        if parent and os.path.isabs(parent):
            env["PATH"] = parent + (os.pathsep + env["PATH"] if env.get("PATH") else "")
        if PLATFORM == "win32":
            self._job = _WindowsJob()
            options["creationflags"] = options.get("creationflags", 0) | 0x00000004  # CREATE_SUSPENDED
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                         bufsize=1, cwd=cwd, env=env, **options)
        except BaseException:
            self.close()
            raise
        if self._job:
            try:
                self._job.attach_and_resume(self.proc.pid)
            except BaseException:
                self.proc.kill()
                self.proc.wait(timeout=2)
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    stream.close()
                self.close()
                raise

    def stop(self, force=False):
        with self._lock:
            try:
                if self._job:
                    self._job.stop()
                elif PLATFORM != "win32":
                    os.killpg(self.proc.pid, signal.SIGKILL if force else signal.SIGTERM)
                elif self.proc.poll() is None:
                    self.proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def close(self):
        with self._lock:
            if self._job:
                self._job.close()
                self._job = None
