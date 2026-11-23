"""DiskLens panel: starts the local API and opens the window (pywebview, Edge WebView2).
Falls back to the default browser if pywebview is unavailable.

Only one panel runs at a time: opening it again brings the existing window to the front.
With ``replace`` (an elevated relaunch) the old window is closed first."""
import ctypes
import mimetypes
import sys
import time
import webbrowser

from . import icon, instance, server
from .config import data_dir

WM_SETICON = 0x0080


def _set_window_icon(title):
    """WinForms windows show the Python icon; put the DiskLens icon on the window and taskbar."""
    u32 = ctypes.windll.user32
    u32.LoadImageW.restype = ctypes.c_void_p
    u32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint]
    u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    u32.FindWindowW.restype = ctypes.c_void_p
    path = data_dir() / "app.ico"
    if not path.exists():
        path.write_bytes(icon.ico(0.62))
    for _ in range(50):
        hwnd = u32.FindWindowW(None, title)
        if hwnd:
            for kind, size in ((0, 16), (1, 32)):          # ICON_SMALL, ICON_BIG
                h = u32.LoadImageW(None, str(path), 1, size, size, 0x10)
                if h:
                    u32.SendMessageW(hwnd, WM_SETICON, kind, h)
            return
        time.sleep(0.1)


def main(replace=False):
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    log = open(data_dir() / "app.log", "a", encoding="utf-8")
    if sys.stderr is None or "pythonw" in sys.executable.lower():
        sys.stdout = sys.stderr = log

    if not instance.claim(instance.PANEL_MUTEX):
        if not replace:
            instance.start_panel()                 # already open: focus it
            return
        instance.close_panel(wait=15)
        if not instance.claim(instance.PANEL_MUTEX):
            return
    try:   # own taskbar group, so the taskbar shows the DiskLens icon instead of Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DiskLens.Painel")
    except (AttributeError, OSError):
        pass

    srv, url = server.start()
    try:
        import webview
    except Exception:
        webview = None
    if webview is not None:
        win = webview.create_window(instance.PANEL_TITLE, url, width=1440, height=920, min_size=(1000, 680))
        win.events.shown += lambda: _set_window_icon(instance.PANEL_TITLE)
        # one fixed browser profile next to the data, instead of a new temp folder on every launch
        # (the default private mode leaves ~70 MB in %TEMP% whenever the window is not closed cleanly)
        webview.start(private_mode=False, storage_path=str(data_dir() / "webview"))
        return
    webbrowser.open(url)
    print("DiskLens aberto no navegador. Feche esta janela para encerrar.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
