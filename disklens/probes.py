"""Measure disk usage that no folder scan can see, and reconcile it with the scan.

used (what Windows reports) - scanned (sum of readable files) = "unaccounted".
This module explains that gap item by item:
  * Shadow copies / restore points (WMI Win32_ShadowStorage, admin)
  * Windows reserved storage (registry ReserveManager, readable without admin)
  * NTFS metadata: $MFT (FSCTL_GET_NTFS_VOLUME_DATA, admin; estimated otherwise), USN journal, $LogFile
  * folders that could not be read (whatever is left)
Plus context that explains *why* visible system files have the size they have (pagefile, hiberfil).
"""
import ctypes
import ctypes.wintypes as wt
import json
import struct
import subprocess
import winreg

from . import winapi
from .i18n import num, tr

GB = 1024 ** 3
MB = 1024 ** 2

FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064
FSCTL_QUERY_USN_JOURNAL = 0x000900F4

_PS_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$out = @{}
function ts($d) { if ($d) { ([DateTimeOffset]$d).ToUnixTimeSeconds() } else { $null } }
try {
  $pf = Get-CimInstance Win32_PageFileUsage
  $out.pagefile = @($pf | ForEach-Object { @{ name = $_.Name; allocated_mb = $_.AllocatedBaseSize; current_mb = $_.CurrentUsage; peak_mb = $_.PeakUsage } })
  $out.pagefile_auto = (Get-CimInstance Win32_ComputerSystem).AutomaticManagedPagefile
  $out.boot_time = ts((Get-CimInstance Win32_OperatingSystem).LastBootUpTime)
} catch { $out.pagefile_error = $_.Exception.Message }
try {
  $vols = @(Get-CimInstance Win32_Volume | Where-Object { $_.DriveLetter })
  $out.shadow_storage = @(Get-CimInstance Win32_ShadowStorage | ForEach-Object {
    $id = $_.Volume.DeviceID
    $v = $vols | Where-Object { $_.DeviceID -eq $id } | Select-Object -First 1
    @{ drive = $v.DriveLetter; used = $_.UsedSpace; allocated = $_.AllocatedSpace; max = $_.MaxSpace; capacity = $v.Capacity } })
  $out.shadow_copies = @(Get-CimInstance Win32_ShadowCopy | ForEach-Object {
    $id = $_.VolumeName
    $v = $vols | Where-Object { $_.DeviceID -eq $id } | Select-Object -First 1
    @{ drive = $v.DriveLetter; created = ts($_.InstallDate) } })
} catch { $out.shadow_error = $_.Exception.Message }
try {
  $out.restore_points = @(Get-ComputerRestorePoint | ForEach-Object {
    @{ desc = $_.Description; created = ts([Management.ManagementDateTimeConverter]::ToDateTime($_.CreationTime)); type = $_.RestorePointType } })
} catch { $out.restore_error = $_.Exception.Message }
$out | ConvertTo-Json -Depth 5 -Compress
"""


def _powershell():
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_SCRIPT],
                           capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        txt = r.stdout.decode("utf-8", "replace").strip()
        return json.loads(txt) if txt else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _reg(path, names):
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            for n in names:
                try:
                    out[n] = winreg.QueryValueEx(k, n)[0]
                except OSError:
                    pass
    except OSError:
        pass
    return out


def _volume_ioctls(volume):
    """Exact NTFS metadata sizes; needs admin (volume handle with read access)."""
    k32 = winapi.k32
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
    k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                    ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    h = k32.CreateFileW("\\\\.\\" + volume, 0x80000000, 3, None, 3, 0, None)   # GENERIC_READ
    if h in (None, wt.HANDLE(-1).value, -1, 0xFFFFFFFFFFFFFFFF):
        return {}
    out = {}
    try:
        buf = ctypes.create_string_buffer(512)
        n = wt.DWORD()
        if k32.DeviceIoControl(h, FSCTL_GET_NTFS_VOLUME_DATA, None, 0, buf, 512, ctypes.byref(n), None):
            (_, _, total_cl, free_cl, reserved_cl, bps, bpc, bpfrs, _, mft_valid) = \
                struct.unpack_from("<qqqqqIIIIq", buf.raw)
            out["mft_bytes"] = mft_valid
            out["cluster"] = bpc
            out["reserved_clusters_bytes"] = reserved_cl * bpc
        if k32.DeviceIoControl(h, FSCTL_QUERY_USN_JOURNAL, None, 0, buf, 512, ctypes.byref(n), None):
            _, first, nxt, _, _, maxsize, delta = struct.unpack_from("<Qqqqqqq", buf.raw)
            out["usn_bytes"] = max(0, min(nxt - first, maxsize + delta))
    finally:
        k32.CloseHandle(h)
    return out


def collect(volume):
    """Raw measurements for this volume (cheap: ~1-3 s)."""
    volume = volume.upper()
    ps = _powershell()
    res = _reg(r"SOFTWARE\Microsoft\Windows\CurrentVersion\ReserveManager",
               ["ShippedWithReserves", "PassedPolicy", "BaseHardReserveSize", "HardReserveAdjustment",
                "BaseSoftReserveSize", "ActiveScenario"])
    power = _reg(r"SYSTEM\CurrentControlSet\Control\Power",
                 ["HibernateEnabled", "HiberFileType", "HiberFileSizePercent"])
    m = winapi.MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    winapi.k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {
        "volume": volume, "admin": winapi.is_admin(), "ram": m.ullTotalPhys,
        "ps": ps, "reserve": res, "power": power, "ntfs": _volume_ioctls(volume),
    }




# ---- texts ---------------------------------------------------------------------------------
# Every text is stored with its Portuguese template and parameters ("src"), so that
# localize() can show a snapshot taken in one language in another. Float parameters are
# formatted with the decimal separator of the language (2 decimals when the name ends in 2).
def _fmt(params):
    return {k: (num(v, 2 if k.endswith("2") else 1) if isinstance(v, float) else
                _text(v) if isinstance(v, (list, tuple)) else v) for k, v in params.items()}


def _text(src):
    if not src:
        return ""
    tmpl, params = src
    return tr(tmpl, **_fmt(params)) if params else tr(tmpl)


def _item(key, label, size, exact, desc, action="", safety="", in_scan=False):
    """label/desc/action: (template, params) pairs."""
    src = {"label": label, "desc": desc, "action": action or None}
    return {"key": key, "label": _text(label), "bytes": int(max(0, size)), "exact": exact, "desc": _text(desc),
            "action": _text(action), "safety": safety, "in_scan": in_scan, "src": src}


def localize(hidden):
    """Re-render the stored texts in the current language (no-op for old snapshots without "src")."""
    if not hidden:
        return hidden
    out = dict(hidden)
    items = []
    for it in hidden.get("items", []):
        it = dict(it)
        src = it.get("src")
        if src:
            for k in ("label", "desc", "action"):
                it[k] = _text(src.get(k))
        else:
            for k in ("label", "desc", "action"):
                it[k] = tr(it.get(k) or "")
        items.append(it)
    out["items"] = items
    out["notes"] = [_text(n) if isinstance(n, list) else tr(n) for n in hidden.get("notes_src") or hidden.get("notes", [])]
    return out


def _ntfs(raw, nfiles, ndirs, take):
    nt = raw.get("ntfs") or {}
    if nt.get("mft_bytes"):
        mft, exact = nt["mft_bytes"], True
    else:
        mft, exact = (nfiles + ndirs) * 1024 * 1.05, False
    meta = mft + nt.get("usn_bytes", 32 * MB) + 64 * MB    # + $LogFile (~64 MB)
    full = exact and "usn_bytes" in nt
    desc = ("A 'tabela de conteúdo' do disco: cerca de 1 KB para cada arquivo ou pasta que já existiu "
            "({millions2} milhões hoje). A $MFT cresce quando muitos arquivos são criados "
            "(node_modules, caches, pacotes Python) e nunca encolhe, mesmo depois de apagá-los.")
    if not exact:
        desc = desc[:-1] + ". Valor estimado: o tamanho exato só pode ser lido como administrador."
    take(_item("ntfs", ("Metadados do NTFS ($MFT, journal, $LogFile)", None), meta, full,
               (desc, {"millions2": (nfiles + ndirs) / 1e6}),
               ("Não há o que limpar: é a estrutura do sistema de arquivos.", None), "nao"))


def reconcile(raw, used, scanned, nfiles, ndirs, svi_bytes, denied_dirs, is_system):
    """Break the unaccounted gap down into explained items. Returns dict saved with the snapshot."""
    gap = max(0, used - scanned)
    remaining = gap
    items = []
    notes = []
    if scanned > used * 1.002:
        notes.append(["A soma dos arquivos passou do espaço usado em {over2} GB: algum arquivo foi contado "
                      "mais de uma vez ou cresceu durante o scan.", {"over2": (scanned - used) / GB}])
    vol = raw["volume"]
    ps = raw.get("ps") or {}
    admin = raw.get("admin")

    def take(it):
        nonlocal remaining
        if not it["in_scan"]:
            it["bytes"] = int(min(it["bytes"], remaining)) if not it["exact"] else it["bytes"]
            remaining = max(0, remaining - it["bytes"])
        items.append(it)

    # 1. Shadow copies / restore points --------------------------------------------------
    ss = [s for s in ps.get("shadow_storage") or [] if (s.get("drive") or "").upper() == vol]
    copies = [c for c in ps.get("shadow_copies") or [] if (c.get("drive") or "").upper() == vol]
    rps = ps.get("restore_points") or []
    shadow = {"available": bool(ss) or "shadow_error" not in ps, "copies": len(copies),
              "restore_points": rps if is_system else [], "storage": ss[0] if ss else None}
    label = ("Pontos de restauração (cópias de sombra)", None)
    if ss:
        s = ss[0]
        in_scan = svi_bytes >= s["used"] * 0.8      # admin + backup privilege read the files themselves
        cap = s.get("capacity") or 0
        if not s.get("max") or s["max"] > 10 ** 15:
            limit = ("sem limite", None)
        elif cap:
            limit = ("{gb} GB ({pct}% do disco)", {"gb": s["max"] / GB, "pct": round(s["max"] / cap * 100)})
        else:
            limit = ("{gb} GB", {"gb": s["max"] / GB})
        take(_item("shadow", label, s["used"], True,
                   ("{copies} cópia(s) de sombra guardadas. Limite configurado: {limit}. O Windows cria pontos "
                    "automaticamente antes de atualizações e instalações de drivers, e cada um guarda as versões "
                    "antigas dos blocos que mudaram desde então. Esse espaço cresce sem que nenhum arquivo novo apareça.",
                    {"copies": len(copies), "limit": limit}),
                   ("Painel de Controle → Sistema → Proteção do Sistema → Configurar: reduza o limite ou exclua os "
                    "pontos antigos. Limpeza de Disco → 'Mais opções' remove todos menos o mais recente.", None),
                   "ferramenta", in_scan))
    elif not admin:
        notes.append(["Pontos de restauração só podem ser medidos como administrador.", None])
    elif ps.get("shadow_error") is None:
        take(_item("shadow", label, 0, True, ("Nenhuma cópia de sombra neste disco.", None)))

    _ntfs(raw, nfiles, ndirs, take)

    # 3. Windows reserved storage --------------------------------------------------------
    r = raw.get("reserve") or {}
    if is_system and r.get("ShippedWithReserves") and r.get("PassedPolicy", 1) and r.get("BaseHardReserveSize"):
        hard = r["BaseHardReserveSize"] + r.get("HardReserveAdjustment", 0)
        take(_item("reserve", ("Armazenamento reservado do Windows", None), hard, False,
                   ("Desde 2019 o Windows reserva cerca de {gb} GB para que atualizações, apps e arquivos "
                    "temporários sempre tenham espaço. A parte da reserva que não está em uso conta como 'usada', "
                    "mas não aparece como arquivo nenhum. O valor mostrado é a reserva configurada, limitado ao "
                    "espaço que sobrou sem explicação.", {"gb": hard / GB}),
                   ("Dá para desligar como administrador: 'DISM /Online /Set-ReservedStorageState /State:Disabled' "
                    "(só funciona quando não há atualização em andamento). Não é recomendado com disco quase cheio, "
                    "porque as atualizações podem falhar.", None), "cuidado"))

    # 4. Whatever is left -----------------------------------------------------------------
    items.append(_item("unknown", ("Não identificado", None), remaining, False,
                       ("{n} pastas não puderam ser lidas (rode como administrador para ler quase todas). Também "
                        "entram aqui arquivos que estavam sendo gravados durante o scan e diferenças de arredondamento."
                        if not admin else
                        "{n} pastas não puderam ser lidas. Também entram aqui arquivos que estavam sendo gravados "
                        "durante o scan e diferenças de arredondamento.", {"n": denied_dirs}),
                       ("Rode o scan como administrador.", None) if not admin else "", ""))

    # Context about visible system files --------------------------------------------------
    pf = [p for p in ps.get("pagefile") or [] if (p.get("name") or "")[:2].upper() == vol]
    pagefile = None
    if pf:
        p = pf[0]
        pagefile = {"allocated": p["allocated_mb"] * MB, "current": p["current_mb"] * MB,
                    "peak": p["peak_mb"] * MB, "auto": ps.get("pagefile_auto"), "boot_time": ps.get("boot_time")}
    power = raw.get("power") or {}
    hib = {"enabled": bool(power.get("HibernateEnabled", 1)), "type": power.get("HiberFileType"),
           "ram": raw.get("ram")}

    return {"gap": gap, "items": items, "notes": [_text(n) for n in notes], "notes_src": notes, "admin": admin,
            "shadow": shadow, "pagefile": pagefile, "hibernation": hib, "ntfs_exact": bool(raw.get("ntfs"))}