"""Single instance, the panel process and "start with Windows" (standard library only).

Processes:
  * tray   - ``launch.pyw`` / ``launch.pyw --tray``: the notification-area icon (light, ~20 MB)
  * panel  - ``launch.pyw --panel``: the window (WebView2). Closing it ends only this process.
The 15-minute collection is a Task Scheduler task and runs whether or not either is open.
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import time
import winreg

from .config import APP_DIR

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32.CreateMutexW.restype = wt.HANDLE
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
k32.OpenMutexW.restype = wt.HANDLE
k32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.CloseHandle.argtypes = [wt.HANDLE]
u32.FindWindowW.restype = wt.HWND
u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.IsIconic.argtypes = [wt.HWND]

TRAY_MUTEX = "Local\\DiskLensTray"
PANEL_MUTEX = "Local\\DiskLensPanel"
TRAY_CLASS = "DiskLensTrayWnd"
PANEL_TITLE = "DiskLens - o que ocupa o seu disco"
WM_CLOSE = 0x0010
WM_APP = 0x8000
MSG_OPEN = WM_APP + 2          # another launcher asks the tray to open/focus the panel
MSG_QUIT = WM_APP + 3
ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5
SYNCHRONIZE = 0x00100000

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DiskLens"

_held = []      # keep mutex handles alive for the life of the process


def claim(name):
    """Create the named mutex. True if this process is the first owner."""
    h = k32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not h:
        return err != ERROR_ACCESS_DENIED      # exists, created by an elevated process
    if err == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(h)
        return False
    _held.append(h)
    return True


def running(name):
    h = k32.OpenMutexW(SYNCHRONIZE, False, name)
    if h:
        k32.CloseHandle(h)
        return True
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED


def tray_window():
    return u32.FindWindowW(TRAY_CLASS, None)


def panel_window():
    return u32.FindWindowW(None, PANEL_TITLE)


def focus(hwnd):
    if not hwnd:
        return False
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)                # SW_RESTORE
    u32.ShowWindow(hwnd, 5)                    # SW_SHOW
    u32.SetForegroundWindow(hwnd)
    return True


def close_panel(wait=0.0):
    """Ask the panel window to close (same as clicking X). Optionally wait for it to exit."""
    h = panel_window()
    if h:
        u32.PostMessageW(h, WM_CLOSE, 0, 0)
    end = time.time() + wait
    while wait and time.time() < end and running(PANEL_MUTEX):
        time.sleep(0.2)
    return not running(PANEL_MUTEX)


def pythonw():
    from .tasks import _pythonw
    return _pythonw()


def launch_cmd(*args):
    return [pythonw(), str(APP_DIR / "launch.pyw"), *args]


def start_panel(replace=False):
    """Open the panel, or bring the existing one to the front."""
    if running(PANEL_MUTEX) and not replace:
        for _ in range(25):                    # the window may still be loading
            if focus(panel_window()):
                return "focused"
            time.sleep(0.2)
        return "running"
    subprocess.Popen(launch_cmd("--panel", *(["--replace"] if replace else [])), cwd=str(APP_DIR),
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return "started"


def start_tray(open_panel=True):
    subprocess.Popen(launch_cmd(*([] if open_panel else ["--tray"])), cwd=str(APP_DIR),
                     creationflags=subprocess.CREATE_NO_WINDOW)


# ---- start with Windows (per user, no admin) --------------------------------------------
def autostart_command():
    return subprocess.list2cmdline(launch_cmd("--tray"))


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return bool(winreg.QueryValueEx(k, RUN_VALUE)[0])
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
    return autostart_enabled()


def refresh_autostart():
    """Keep the entry pointing at this folder/Python (the project may have been moved)."""
    if autostart_enabled():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                cur = winreg.QueryValueEx(k, RUN_VALUE)[0]
            if cur != autostart_command():
                set_autostart(True)
        except OSError:
            pass
