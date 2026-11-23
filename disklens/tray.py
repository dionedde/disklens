"""Notification-area (tray) icon for DiskLens. Standard library only (ctypes + Win32).

Off by default: turned on in the Timeline tab (config "tray"). The icon (icon.py) is a magnifying
glass over a pie chart of the system drive (blue, orange above 85%, red above 95%). Click: open the panel. Right click: free space, collection status, "Coletar agora",
"Iniciar com o Windows", "Sair". Closing the panel window leaves the icon here.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
import traceback

from . import icon, instance, winapi
from .config import data_dir

u32 = ctypes.WinDLL("user32", use_last_error=True)
s32 = ctypes.WinDLL("shell32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("a", wt.DWORD), ("b", wt.WORD), ("c", wt.WORD), ("d", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
                ("time", wt.DWORD), ("pt", POINT)]


u32.DefWindowProcW.restype = LRESULT
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
u32.CreateWindowExW.restype = wt.HWND
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p]
u32.DestroyWindow.argtypes = [wt.HWND]
u32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT]
u32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
u32.CreatePopupMenu.restype = wt.HMENU
u32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
u32.SetMenuDefaultItem.argtypes = [wt.HMENU, wt.UINT, wt.UINT]
u32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
                               ctypes.c_void_p]
u32.DestroyMenu.argtypes = [wt.HMENU]
u32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.LoadImageW.restype = wt.HANDLE
u32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
u32.DestroyIcon.argtypes = [wt.HICON]
u32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
u32.ChangeWindowMessageFilterEx.argtypes = [wt.HWND, wt.UINT, wt.DWORD, ctypes.c_void_p]
s32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

WM_DESTROY, WM_CLOSE, WM_TIMER, WM_NULL = 0x0002, 0x0010, 0x0113, 0x0000
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
WM_TRAY = instance.WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
MF_STRING, MF_GRAYED, MF_SEPARATOR, MF_CHECKED = 0, 1, 0x800, 8
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40

ID_OPEN, ID_COLLECT, ID_AUTOSTART, ID_QUIT, ID_ENABLE = 1, 2, 3, 4, 5
REFRESH_MS = 60_000


# ---- status -------------------------------------------------------------------------------
def _gb(b):
    return f"{b / 1024 ** 3:.1f}".replace(".", ",")


def read_status():
    """Latest 15-minute measurement per drive + agent state. Cheap (one small query)."""
    from . import agent, db
    vols = []
    try:
        with db.index_conn() as con:
            for r in con.execute("SELECT volume, total, used, free, MAX(ts) AS ts FROM samples "
                                 "WHERE total > 0 GROUP BY volume ORDER BY volume"):
                vols.append({"volume": r[0], "total": r[1], "used": r[2], "free": r[3], "ts": r[4]})
    except Exception:
        pass
    live = {v["volume"]: v for v in winapi.fixed_volumes()}     # always current, even without the agent
    for v in vols:
        lv = live.pop(v["volume"], None)
        if lv:
            v.update(total=lv["total"], free=lv["free"], used=lv["total"] - lv["free"])
    for lv in live.values():
        vols.append({"volume": lv["volume"], "total": lv["total"], "free": lv["free"],
                     "used": lv["total"] - lv["free"], "ts": None})
    vols.sort(key=lambda v: v["volume"])
    st = agent.load_state()
    return {"volumes": vols, "last_run": st.get("last_run"), "system": winapi.system_drive()}


def _ago(ts):
    if not ts:
        return "ainda não rodou"
    m = (time.time() - ts) / 60
    if m < 1:
        return "agora há pouco"
    if m < 60:
        return f"há {m:.0f} min"
    if m < 48 * 60:
        return f"há {m / 60:.0f} h"
    return f"há {m / 1440:.0f} dias"


# ---- tray window --------------------------------------------------------------------------
class Tray:
    def __init__(self):
        self.hwnd = None
        self.hicon = None
        self.icon_key = None
        self.status = {"volumes": []}
        self.installed = None
        self.installed_at = 0
        self._proc = WNDPROC(self._wndproc)        # keep a reference: ctypes callbacks must stay alive
        self.taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated")
        self.log = data_dir() / "tray.log"

    def _log(self, msg):
        try:
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except OSError:
            pass

    # window ---------------------------------------------------------------------------
    def create(self):
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = instance.TRAY_CLASS
        u32.RegisterClassW(ctypes.byref(wc))
        # hidden top-level window (not message-only: those never get the TaskbarCreated broadcast)
        self.hwnd = u32.CreateWindowExW(0, instance.TRAY_CLASS, "DiskLens", 0, 0, 0, 0, 0, None, None, hinst, None)
        # let a non-elevated launcher ask an elevated tray to open the panel
        u32.ChangeWindowMessageFilterEx(self.hwnd, instance.MSG_OPEN, 1, None)
        u32.ChangeWindowMessageFilterEx(self.hwnd, instance.MSG_QUIT, 1, None)
        u32.ChangeWindowMessageFilterEx(self.hwnd, self.taskbar_created, 1, None)
        self.refresh(add=True)
        u32.SetTimer(self.hwnd, 1, REFRESH_MS, None)

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        return nid

    def _system(self):
        s = self.status
        return next((v for v in s["volumes"] if v["volume"] == s.get("system")), None) or \
            (s["volumes"][0] if s["volumes"] else None)

    def refresh(self, add=False):
        try:
            self.status = read_status()
        except Exception:
            self._log(traceback.format_exc())
        sv = self._system()
        frac = sv["used"] / sv["total"] if sv and sv["total"] else 0
        key = (round(frac * 50), icon.color_for(frac))           # redraw only on a visible change
        if key != self.icon_key:
            path = data_dir() / "tray.ico"
            path.write_bytes(icon.ico(frac, sizes=(16, 20, 24, 32)))
            h = u32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                old, self.hicon, self.icon_key = self.hicon, h, key
                if old:
                    u32.DestroyIcon(old)
        tip = "DiskLens"
        if sv:
            tip += f"\n{sv['volume']} {_gb(sv['free'])} GB livres ({frac * 100:.0f}% usado)"
        tip += f"\nÚltima medição: {_ago(self.status.get('last_run'))}"
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = tip[:127]
        if not s32.Shell_NotifyIconW(NIM_MODIFY if not add else NIM_ADD, ctypes.byref(nid)) and not add:
            s32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))     # explorer restarted / icon lost

    def notify(self, title, text):
        nid = self._nid()
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        s32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def agent_installed(self):
        if self.installed is None or time.time() - self.installed_at > 300:
            from . import tasks
            try:
                self.installed = tasks.is_installed()
            except Exception:
                self.installed = False
            self.installed_at = time.time()
        return self.installed

    # menu -----------------------------------------------------------------------------
    def menu(self):
        self.refresh()
        m = u32.CreatePopupMenu()
        u32.AppendMenuW(m, MF_STRING, ID_OPEN, "Abrir painel")
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        for v in self.status["volumes"]:
            pct = v["used"] / v["total"] * 100 if v["total"] else 0
            u32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0,
                            f"{v['volume']}  {_gb(v['free'])} GB livres de {_gb(v['total'])} GB ({pct:.0f}% usado)")
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        if self.agent_installed():
            u32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0,
                            f"Coleta automática ativa · última {_ago(self.status.get('last_run'))}")
            u32.AppendMenuW(m, MF_STRING, ID_COLLECT, "Medir agora")
        else:
            u32.AppendMenuW(m, MF_STRING | MF_GRAYED, 0, "Coleta automática desativada")
            u32.AppendMenuW(m, MF_STRING, ID_ENABLE, "Ativar coleta automática…")
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        u32.AppendMenuW(m, MF_STRING | (MF_CHECKED if instance.autostart_enabled() else 0), ID_AUTOSTART,
                        "Iniciar com o Windows")
        u32.AppendMenuW(m, MF_STRING, ID_QUIT, "Sair do DiskLens")
        u32.SetMenuDefaultItem(m, ID_OPEN, False)
        pt = POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)                  # so the menu closes when clicking elsewhere
        cmd = u32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        u32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u32.DestroyMenu(m)
        self.command(cmd)

    def command(self, cmd):
        if cmd == ID_OPEN:
            self.open_panel()
        elif cmd == ID_ENABLE:
            self.open_panel()
            self.notify("DiskLens", "Ative a coleta na aba Linha do tempo → Ativar coleta automática.")
        elif cmd == ID_COLLECT:
            def run():
                from . import tasks
                tasks.run_agent_now()
                time.sleep(15)
                u32.PostMessageW(self.hwnd, WM_TIMER, 1, 0)
            threading.Thread(target=run, daemon=True).start()
            self.notify("DiskLens", "Medindo o disco em segundo plano…")
        elif cmd == ID_AUTOSTART:
            try:
                on = instance.set_autostart(not instance.autostart_enabled())
                self.notify("DiskLens", "O ícone vai aparecer ao entrar no Windows." if on else
                            "O DiskLens não vai mais iniciar com o Windows. A coleta automática continua.")
            except OSError as e:
                self.notify("DiskLens", f"Não foi possível alterar: {e}")
        elif cmd == ID_QUIT:
            instance.close_panel()
            u32.DestroyWindow(self.hwnd)

    def open_panel(self):
        threading.Thread(target=instance.start_panel, daemon=True).start()

    # messages ---------------------------------------------------------------------------
    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TRAY:
                if lp == WM_LBUTTONUP:
                    self.open_panel()
                elif lp in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self.menu()
                return 0
            if msg == instance.MSG_OPEN:
                self.open_panel()
                return 0
            if msg == instance.MSG_QUIT or msg == WM_CLOSE:
                u32.DestroyWindow(hwnd)
                return 0
            if msg == WM_TIMER:
                self.refresh()
                return 0
            if msg == self.taskbar_created:
                self.refresh(add=True)
                return 0
            if msg == WM_DESTROY:
                s32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
                u32.PostQuitMessage(0)
                return 0
        except Exception:
            self._log(traceback.format_exc())
            return 0
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def loop(self):
        m = MSG()
        while u32.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(m))
            u32.DispatchMessageW(ctypes.byref(m))


def main(open_panel=True):
    if "pythonw" in sys.executable.lower() or sys.stderr is None:
        sys.stdout = sys.stderr = open(data_dir() / "tray.log", "a", encoding="utf-8")
    from .config import load as load_config
    if not load_config().get("tray"):
        if open_panel:
            from .app import main as panel
            panel(replace=winapi.is_admin())
        return
    elevated = winapi.is_admin()
    if not instance.claim(instance.TRAY_MUTEX):
        # the icon is already there
        if elevated and open_panel:
            instance.start_panel(replace=True)        # admin requested: this (elevated) process opens it
        elif open_panel:
            h = instance.tray_window()
            if h:
                instance.u32.PostMessageW(h, instance.MSG_OPEN, 0, 0)
            else:
                instance.start_panel()
        return
    instance.refresh_autostart()
    t = Tray()
    t.create()
    if open_panel:
        instance.start_panel(replace=elevated)
    t.loop()


if __name__ == "__main__":
    main(open_panel="--tray" not in sys.argv)
