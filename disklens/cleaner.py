"""Deleting things safely.

Three ways to free space, each with its own guard:
  * clean_contents(folder): empties a cache/temp folder *permanently* (the Recycle Bin would not
    free anything). Only allowed for folders whose rule is "seguro" (recreated automatically).
  * recycle(paths): sends user-chosen files/folders to the Recycle Bin (reversible). Allowed only
    outside system areas; see protection_reason().
  * run_tool(id): Windows' own cleanup tools (DISM, Windows Update cache, Delivery Optimization,
    hibernation, old restore points, empty Recycle Bin).
Every action is written to the cleanup log in index.db.
"""
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import stat
import subprocess
import time

from . import db, winapi
from .config import APP_DIR, data_dir
from .rules import Rules

_rules = None


def rules():
    global _rules
    if _rules is None:
        _rules = Rules()
    return _rules


def effective_rule(path):
    """(rule index, matched folder) for a path: the deepest folder rule on its way up, or a file rule."""
    r = rules()
    p = os.path.normpath(path)
    name = os.path.basename(p).lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if os.path.isfile(p):
        f = r.match_file(name, ext)
        if f is not None:
            return f, p
    parts = p.rstrip("\\").split("\\")
    found, where = None, None
    for i in range(1, len(parts) + 1):
        sub = "\\".join(parts[:i]) + ("\\" if i == 1 else "")
        idx = r.match_dir(sub)
        if idx is not None:
            found, where = idx, sub
    return found, where


def _env(name, default=""):
    return os.path.normcase(os.path.normpath(os.environ.get(name, default))) if os.environ.get(name) else None


def _system_roots():
    roots = {}
    for var, why in (("WINDIR", "pasta do Windows"), ("ProgramFiles", "programas instalados"),
                     ("ProgramFiles(x86)", "programas instalados"), ("ProgramW6432", "programas instalados"),
                     ("ProgramData", "dados compartilhados de programas")):
        p = _env(var)
        if p:
            roots[p] = why
    return roots


_prog_cache = {"ts": 0, "dirs": {}}


def program_dirs():
    """{normalized install folder: program name} for installed programs (cached for 10 minutes),
    plus the Python installation DiskLens itself runs on."""
    import sys
    if time.time() - _prog_cache["ts"] > 600:
        from .candidates import installed_programs
        dirs = {}
        try:
            for p in installed_programs():
                loc = (p.get("location") or "").strip().strip('"').rstrip("\\")
                if len(loc) > 3 and os.path.isdir(loc):
                    dirs[os.path.normcase(os.path.normpath(loc))] = p["name"]
        except OSError:
            pass
        for base in {sys.base_prefix, sys.prefix}:
            dirs[os.path.normcase(os.path.normpath(base))] = "o Python usado pelo DiskLens"
        _prog_cache.update(ts=time.time(), dirs=dirs)
    return _prog_cache["dirs"]


KNOWN_USER_FOLDERS = {"desktop", "documents", "downloads", "pictures", "videos", "music", "onedrive", "appdata",
                      "favorites", "contacts", "saved games", "searches", "links", "3d objects"}


def protection_reason(path, for_contents=False):
    """Why this path must not be deleted (None = allowed). `for_contents` = emptying a folder."""
    if not path:
        return "caminho vazio"
    p = os.path.normpath(os.path.abspath(path))
    low = os.path.normcase(p)
    if len(p) <= 3:
        return "é a raiz de um disco"
    if not os.path.lexists(p):
        return "não existe mais"
    try:
        st = os.lstat(p)
    except OSError as e:
        return f"inacessível ({e.strerror})"
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & winapi.FILE_ATTRIBUTE_REPARSE_POINT and os.path.isdir(p):
        return "é um atalho do sistema (junction/link), não uma pasta real"
    if attrs & 0x4 and not for_contents:                      # FILE_ATTRIBUTE_SYSTEM
        return "é um arquivo de sistema"
    for own in (os.path.normcase(str(APP_DIR)), os.path.normcase(str(data_dir()))):
        if low == own or low.startswith(own + "\\"):
            return "faz parte do próprio DiskLens"
    name = os.path.basename(low)
    if name in ("pagefile.sys", "hiberfil.sys", "swapfile.sys"):
        return "é gerenciado pelo Windows: use a ferramenta indicada"

    idx, where = effective_rule(p)
    info = rules().info(idx)
    safety = info["safety"] if info else None
    if safety == "seguro":
        return None                                           # caches, temp, dumps... even under Windows
    if safety == "nao":
        return f"“{info['label']}”: necessário para o Windows"
    if safety == "ferramenta":
        return f"“{info['label']}”: use a ferramenta indicada em vez de apagar à mão"
    for root, why in _system_roots().items():
        if low == root or low.startswith(root + "\\"):
            return f"está dentro de {why}" + (": use Desinstalar" if why == "programas instalados" else "")
    for root, name in program_dirs().items():
        if low == root or low.startswith(root + "\\") or root.startswith(low + "\\"):
            return f"faz parte de um programa instalado ({name}): use Desinstalar"
    # whole user profile or its known folders themselves (their contents are fine)
    users = os.path.normcase(os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "Users"))
    if low == users:
        return "é a pasta de todos os usuários"
    if low.startswith(users + "\\"):
        rest = low[len(users) + 1:].split("\\")
        if len(rest) == 1:
            return "é a pasta pessoal de um usuário inteira"
        if len(rest) == 2 and rest[1] in KNOWN_USER_FOLDERS:
            return "é uma pasta padrão do Windows (apague o conteúdo, não a pasta)"
    return None


# ------------------------------------------------------------------ permanent: empty cache folders
def _rm_readonly(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def clean_contents(folder, min_age_seconds=0):
    """Delete everything inside a "seguro" folder. Files in use are skipped.
    Returns (bytes_freed, files_deleted, files_skipped)."""
    reason = protection_reason(folder, for_contents=True)
    if reason:
        raise PermissionError(reason)
    idx, _ = effective_rule(folder)
    info = rules().info(idx)
    if not info or info["safety"] != "seguro":
        raise PermissionError("só pastas marcadas como “seguro de limpar” podem ser esvaziadas")
    counts = [0, 0, 0]                                       # freed, deleted, skipped
    _empty_dir(folder, time.time(), min_age_seconds, counts)
    return tuple(counts)


def _is_link(entry):
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return True


def _walk_files(folder):
    """Yield DirEntry of every file below folder, never following junctions or symlinks."""
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if _is_link(e):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        yield e
        except OSError:
            continue


def _empty_dir(d, now, min_age, counts):
    try:
        entries = list(os.scandir(d))
    except OSError:
        return
    for e in entries:
        if _is_link(e):                                      # a link: leave it (and its target) alone
            continue
        try:
            if e.is_dir(follow_symlinks=False):
                _empty_dir(e.path, now, min_age, counts)
                try:
                    os.rmdir(e.path)                         # only succeeds when it became empty
                except OSError:
                    pass
                continue
            st = e.stat(follow_symlinks=False)
            if min_age and now - st.st_mtime < min_age:
                counts[2] += 1
                continue
            if st.st_file_attributes & 1:                    # read-only
                os.chmod(e.path, stat.S_IWRITE)
            os.remove(e.path)
            counts[0] += st.st_size
            counts[1] += 1
        except OSError:
            counts[2] += 1                                   # in use / no permission


# ------------------------------------------------------------------ reversible: Recycle Bin
class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", wt.LPCWSTR), ("pTo", wt.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wt.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wt.LPCWSTR)]


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 0x4, 0x10, 0x40, 0x400, 0x4000


def path_size(p):
    if os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    total = 0
    for e in _walk_files(p):
        try:
            total += e.stat(follow_symlinks=False).st_size
        except OSError:
            pass
    return total


def recycle(path):
    """Send one file/folder to the Recycle Bin. If Windows cannot recycle it (too big for the bin),
    it asks the user before deleting permanently (FOF_WANTNUKEWARNING). Returns bytes moved."""
    reason = protection_reason(path)
    if reason:
        raise PermissionError(reason)
    size = path_size(path)
    op = SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING,
                         False, None, None)
    rc = winapi.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(rc, "não foi possível mover para a Lixeira (arquivo em uso ou operação cancelada)")
    if os.path.lexists(path):
        raise OSError(0, "o item ainda existe (parte dele pode estar em uso)")
    return size


def recycle_bin_info(drive=None):
    """{'bytes': n, 'items': n} for one drive (e.g. 'C:') or all drives."""
    q = SHQUERYRBINFO(ctypes.sizeof(SHQUERYRBINFO), 0, 0)
    root = (drive.upper()[:2] + "\\") if drive else None
    if winapi.shell32.SHQueryRecycleBinW(root, ctypes.byref(q)) != 0:
        return {"bytes": 0, "items": 0}
    return {"bytes": q.i64Size, "items": q.i64NumItems}


def empty_recycle_bin(drive=None):
    before = recycle_bin_info(drive)
    root = (drive.upper()[:2] + "\\") if drive else None
    winapi.shell32.SHEmptyRecycleBinW(None, root, 0x1 | 0x2 | 0x4)   # no confirmation/progress/sound
    return before["bytes"]


# ------------------------------------------------------------------ Windows' own tools
def _run(cmd, timeout=3600):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    out = (r.stdout or b"").decode("cp850", "replace") + (r.stderr or b"").decode("cp850", "replace")
    return r.returncode, out.strip()[-2000:]


def _ps(script, timeout=1800):
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


def _wu_cache():
    folder = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "SoftwareDistribution", "Download")
    _run(["net", "stop", "wuauserv"], 120)
    _run(["net", "stop", "bits"], 120)
    counts = [0, 0, 0]
    try:
        _empty_dir(folder, time.time(), 0, counts)
    finally:
        _run(["net", "start", "bits"], 120)
        _run(["net", "start", "wuauserv"], 120)
    return 0, f"{counts[1]} arquivos removidos de {folder}", counts[0]


def _restore_points():
    sysd = winapi.system_drive()
    rc, out = _ps("(Get-CimInstance Win32_ShadowCopy | Where-Object { $_.VolumeName -eq "
                  f"(Get-CimInstance Win32_Volume -Filter \"DriveLetter='{sysd}'\").DeviceID }}).Count")
    try:
        n = int(out.splitlines()[-1])
    except (ValueError, IndexError):
        n = 0
    removed = 0
    while n - removed > 1:
        rc, out = _run(["vssadmin", "delete", "shadows", f"/for={sysd}", "/oldest", "/quiet"], 600)
        if rc != 0:
            break
        removed += 1
    return 0, f"{removed} ponto(s) de restauração antigo(s) removido(s); o mais recente foi mantido", None


TOOLS = {
    "empty_recycle": {"label": "Esvaziar a Lixeira", "admin": False,
                      "desc": "Apaga definitivamente tudo o que está na Lixeira de todos os discos."},
    "wu_cache": {"label": "Cache de downloads do Windows Update", "admin": True,
                 "desc": "Para o serviço do Windows Update, apaga os instaladores já baixados e religa o serviço. O Windows baixa de novo só o que ainda precisar."},
    "dism": {"label": "Limpeza de componentes do Windows (DISM)", "admin": True, "slow": True,
             "desc": "Remove versões antigas de componentes substituídas por atualizações (WinSxS). Ferramenta oficial; pode levar de 5 a 30 minutos."},
    "delivery_opt": {"label": "Cache da Otimização de Entrega", "admin": True,
                     "desc": "Apaga atualizações guardadas para compartilhar com outros PCs."},
    "restore_old": {"label": "Pontos de restauração antigos", "admin": True,
                    "desc": "Remove todos os pontos de restauração, menos o mais recente."},
    "hiber_reduced": {"label": "Reduzir o arquivo de hibernação", "admin": True,
                      "desc": "powercfg /h /type reduced: mantém a Inicialização Rápida e reduz o hiberfil.sys pela metade, mas desativa a hibernação completa."},
    "hiber_off": {"label": "Desligar a hibernação", "admin": True,
                  "desc": "powercfg /h off: apaga o hiberfil.sys inteiro. Desativa a hibernação e a Inicialização Rápida."},
}


def run_tool(tool_id):
    """Returns (ok, message, bytes_freed_or_None)."""
    if tool_id not in TOOLS:
        raise ValueError("ferramenta desconhecida")
    if TOOLS[tool_id]["admin"] and not winapi.is_admin():
        raise PermissionError("precisa rodar o DiskLens como administrador")
    if tool_id == "empty_recycle":
        return True, "Lixeira esvaziada", empty_recycle_bin()
    if tool_id == "wu_cache":
        rc, msg, freed = _wu_cache()
        return True, msg, freed
    if tool_id == "dism":
        rc, out = _run(["dism.exe", "/Online", "/Cleanup-Image", "/StartComponentCleanup"], 3 * 3600)
        return rc == 0, out, None
    if tool_id == "delivery_opt":
        rc, out = _ps("Delete-DeliveryOptimizationCache -Force")
        return rc == 0, out or "Cache da Otimização de Entrega apagado", None
    if tool_id == "restore_old":
        rc, msg, _ = _restore_points()
        return True, msg, None
    if tool_id == "hiber_reduced":
        rc, out = _run(["powercfg", "/h", "/type", "reduced"])
        return rc == 0, out or "Hibernação reduzida", None
    if tool_id == "hiber_off":
        rc, out = _run(["powercfg", "/h", "off"])
        return rc == 0, out or "Hibernação desligada", None


# ------------------------------------------------------------------ log
def log_action(action, target, mode, size, ok, message=""):
    with db.index_conn() as con:
        con.execute("CREATE TABLE IF NOT EXISTS cleanup_log (ts REAL, action TEXT, target TEXT, mode TEXT, "
                    "bytes INTEGER, ok INTEGER, message TEXT)")
        con.execute("INSERT INTO cleanup_log VALUES (?,?,?,?,?,?,?)",
                    (time.time(), action, target, mode, int(size or 0), int(bool(ok)), message[:1000]))


def history(limit=100):
    with db.index_conn() as con:
        con.execute("CREATE TABLE IF NOT EXISTS cleanup_log (ts REAL, action TEXT, target TEXT, mode TEXT, "
                    "bytes INTEGER, ok INTEGER, message TEXT)")
        return [dict(r) for r in con.execute("SELECT * FROM cleanup_log ORDER BY ts DESC LIMIT ?", (limit,))]
