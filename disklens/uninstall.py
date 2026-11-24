"""Clean-up used by the Windows installer (installer/DiskLens.iss).

    python -m disklens.uninstall --quit     close the tray icon and the panel (before an upgrade)
    python -m disklens.uninstall            also remove "Start with Windows" and the scheduled task
    python -m disklens.uninstall --purge    also delete the settings and the collected history
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

from . import instance
from .config import APP_DIR, CONFIG_FILE

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0

from ctypes import wintypes as wt  # noqa: E402
instance.k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
instance.k32.WaitForSingleObject.restype = wt.DWORD
instance.k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
instance.k32.OpenProcess.restype = wt.HANDLE
instance.k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
instance.k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
instance.u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
instance.u32.IsWindow.argtypes = [wt.HWND]


def _here(text):
    """True if a path/command line points into this DiskLens folder (not another copy)."""
    return os.path.normcase(str(APP_DIR)) in os.path.normcase(text or "")


def _window_exe(hwnd):
    pid = ctypes.c_ulong()
    instance.u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = instance.k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        ok = instance.k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        instance.k32.CloseHandle(h)


def _process_of(hwnd):
    """Handle (wait + terminate rights) of the process owning a window, or None."""
    pid = wt.DWORD()
    instance.u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return instance.k32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, False, pid.value) or None


def quit_running(wait=10.0):
    """Close this copy's panel and tray icon, like the user would, and wait for the processes to
    end (the panel takes a moment to unload WebView2; its DLLs must be free before files go)."""
    procs = []
    for hwnd, msg in ((instance.panel_window(), instance.WM_CLOSE), (instance.tray_window(), instance.MSG_QUIT)):
        if hwnd and _here(_window_exe(hwnd)):
            h = _process_of(hwnd)
            instance.u32.PostMessageW(hwnd, msg, 0, 0)
            if h:
                procs.append(h)
    end = time.time() + wait
    for h in procs:
        left = max(0, int((end - time.time()) * 1000))
        if instance.k32.WaitForSingleObject(h, left) != WAIT_OBJECT_0:
            instance.k32.TerminateProcess(h, 1)          # hung: end it so the files can be replaced
            instance.k32.WaitForSingleObject(h, 3000)
        instance.k32.CloseHandle(h)


def _task_points_here():
    from . import tasks
    r = subprocess.run(["schtasks", "/Query", "/TN", tasks.TASK_NAME, "/XML"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode == 0 and _here(r.stdout.decode("utf-16", "ignore") if r.stdout[:2] == b"\xff\xfe"
                                        else r.stdout.decode("utf-8", "ignore"))


def remove_integration():
    """Remove this copy's "Start with Windows" entry and scheduled collection task."""
    if instance.autostart_enabled():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, instance.RUN_KEY) as k:
                cmd = winreg.QueryValueEx(k, instance.RUN_VALUE)[0]
        except OSError:
            cmd = ""
        if _here(cmd):
            instance.set_autostart(False)
    if _task_points_here():
        from . import tasks
        tasks.uninstall()                      # asks UAC: the task was created as administrator


def _data_dir_from_config():
    try:
        d = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("data_dir")
    except (OSError, ValueError):
        return None
    return Path(d) if d else None


def purge():
    """Delete config.json and the data folder. Only a folder that is clearly DiskLens data is removed."""
    d = _data_dir_from_config() or APP_DIR / "data"
    looks_ours = d.is_dir() and ((d / "index.db").exists() or d == APP_DIR / "data")
    if looks_ours and len(d.parts) > 2:        # never a drive root
        shutil.rmtree(d, ignore_errors=True)
        parent = d.parent
        if parent.name.lower() == "disklens" and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    for f in (CONFIG_FILE, APP_DIR / "scan.log"):
        try:
            f.unlink()
        except OSError:
            pass


def main(argv):
    quit_running()
    if "--quit" in argv:
        return 0
    try:
        remove_integration()
    except OSError:
        pass
    if "--purge" in argv:
        purge()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
