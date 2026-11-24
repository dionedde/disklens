"""Thin ctypes wrappers around the Win32 APIs DiskLens needs (no third-party deps)."""
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# --- file attribute / reparse constants -------------------------------------------------
FILE_ATTRIBUTE_SPARSE_FILE = 0x200
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_COMPRESSED = 0x800
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
IO_REPARSE_TAG_WOF = 0x80000017          # CompactOS / "compact /exe" compression
REPARSE_NAME_SURROGATE_BIT = 0x20000000  # symlinks, junctions, mount points

PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000

# SHQueryUserNotificationState results that mean "user is busy, don't disturb"
QUNS_BUSY = 2                  # full-screen app (game, video)
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
BUSY_STATES = {QUNS_BUSY: "Aplicativo em tela cheia",
               QUNS_RUNNING_D3D_FULL_SCREEN: "Jogo em tela cheia",
               QUNS_PRESENTATION_MODE: "Modo de apresentação"}


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

    @property
    def value(self):
        return (self.hi << 32) | self.lo


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


k32.GetCurrentProcess.restype = wt.HANDLE
k32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]
k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
k32.GetCompressedFileSizeW.restype = wt.DWORD
k32.GetCompressedFileSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]


def system_times():
    """(idle, kernel, user) in 100ns units, summed over all CPUs. kernel includes idle."""
    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    return idle.value, kernel.value, user.value


def process_cpu_time():
    """CPU time used by this process (kernel+user), 100ns units."""
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                        ctypes.byref(k), ctypes.byref(u))
    return k.value + u.value


def memory_load():
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.dwMemoryLoad


def user_busy_reason():
    """Return a human reason if a full-screen game/app/presentation is running, else None."""
    st = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(st)) != 0:
            return None
    except OSError:
        return None
    from .i18n import tr
    r = BUSY_STATES.get(st.value)
    return tr(r) if r else r


def set_background_mode():
    """Lowest CPU, I/O and memory priority for this process: other programs (games) always win."""
    return bool(k32.SetPriorityClass(k32.GetCurrentProcess(), PROCESS_MODE_BACKGROUND_BEGIN))


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wt.DWORD), ("Luid", LUID), ("Attributes", wt.DWORD)]


def enable_privilege(name="SeBackupPrivilege"):
    """Enable a privilege the (admin) token holds. With SeBackupPrivilege, directory listing
    bypasses folder permissions (read-only), so protected folders such as
    System Volume Information can be measured. Returns True if enabled."""
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    adv.LookupPrivilegeValueW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.POINTER(LUID)]
    adv.AdjustTokenPrivileges.argtypes = [wt.HANDLE, wt.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES), wt.DWORD,
                                          ctypes.c_void_p, ctypes.c_void_p]
    tok = wt.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(tok)):
        return False
    try:
        luid = LUID()
        if not adv.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES(1, luid, 0x00000002)   # SE_PRIVILEGE_ENABLED
        ctypes.set_last_error(0)
        if not adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None):
            return False
        return ctypes.get_last_error() == 0          # 1300 = not all assigned (not admin)
    finally:
        k32.CloseHandle(tok)


def compressed_size(path):
    """Actual bytes on disk for compressed/sparse/WOF files, or None on error."""
    high = wt.DWORD(0)
    ctypes.set_last_error(0)
    low = k32.GetCompressedFileSizeW(path, ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
        return None
    return (high.value << 32) | low


def cluster_size(root):
    spc, bps, nfree, ntotal = wt.DWORD(), wt.DWORD(), wt.DWORD(), wt.DWORD()
    if k32.GetDiskFreeSpaceW(root, ctypes.byref(spc), ctypes.byref(bps),
                             ctypes.byref(nfree), ctypes.byref(ntotal)):
        return spc.value * bps.value
    return 4096


def is_admin():
    try:
        return bool(shell32.IsUserAnAdmin())
    except OSError:
        return False


def volume_label(root):
    buf = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    if k32.GetVolumeInformationW(root, buf, 261, None, None, None, fs, 261):
        return buf.value, fs.value
    return "", ""


def fixed_volumes():
    """Fixed (local) drives with size info."""
    out = []
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        if k32.GetDriveTypeW(root) != 3:  # DRIVE_FIXED
            continue
        try:
            du = shutil.disk_usage(root)
        except OSError:
            continue
        if du.total < 2 * 1024 ** 3:      # skip tiny/system-reserved volumes
            continue
        label, fs = volume_label(root)
        out.append({"volume": root[:2], "label": label, "fs": fs,
                    "total": du.total, "used": du.used, "free": du.free})
    return out


def relaunch_as_admin(exe, params, cwd):
    """Ask UAC to start a new elevated instance. True if the user accepted."""
    rc = shell32.ShellExecuteW(None, "runas", exe, params, cwd, 1)
    return rc > 32


def reveal_in_explorer(path, select=True):
    if select:
        subprocess.Popen(["explorer", "/select,", path])
    else:
        subprocess.Popen(["explorer", path])


def system_drive():
    return os.environ.get("SystemDrive", "C:").upper()
