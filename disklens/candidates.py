"""What could be removed, built from the latest snapshot.

  safe     - cache/temp folders marked "seguro": emptied permanently, recreated automatically.
  tools    - Windows' own cleanup tools, with the space each one is expected to free.
  review   - the user decides: old installers in Downloads, big old personal files, dependency
             folders of abandoned projects, likely duplicates. They go to the Recycle Bin.
  programs - installed programs (registry), removed through their own uninstaller.
  unknown  - big folders with no known rule (for the AI assistant to explain).
Every listed path is re-checked with cleaner.protection_reason() before it is shown.
"""
import hashlib
import json
import os
import re
import shlex
import sqlite3
import time
import winreg

from . import cleaner, db, winapi
from .rules import Rules

MB = 1024 ** 2
GB = 1024 ** 3
DAY = 86400

INSTALLER_EXTS = {"exe", "msi", "iso", "zip", "7z", "rar", "img", "cab", "msix", "msixbundle", "appx", "tar", "gz",
                  "tgz", "xz", "dmg"}
DEV_DIRS = {"node_modules": "dependências Node.js (npm install recria)",
            ".venv": "ambiente Python virtual (recriável com pip)",
            "venv": "ambiente Python virtual (recriável com pip)",
            ".tox": "ambientes de teste tox", "target": "build do Rust/Java (cargo build recria)",
            ".next": "build do Next.js", ".gradle": "cache do Gradle do projeto"}


def item_id(kind, key):
    return kind + ":" + hashlib.sha1(key.lower().encode("utf-8")).hexdigest()[:12]


def latest_snapshot(volume):
    for s in db.list_snapshots():
        if s["volume"] == volume and s["status"] == "done" and db.snap_path(s["id"]).exists():
            return s
    return None


def _open(s):
    con = sqlite3.connect(f"file:{db.snap_path(s['id'])}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT value FROM meta WHERE key='rules'").fetchone()
    return con, (Rules(json.loads(row[0])) if row else Rules())


BINARY_EXTS = {"dll", "exe", "sys", "pyd", "so", "node", "lib", "jar", "ocx", "mui", "dat", "pak", "db", "ldb"}


def _user_area(path):
    """Personal files: inside a user profile, but not AppData nor programs' hidden ".folders"."""
    low = path.lower()
    if not re.match(r"^[a-z]:\\users\\[^\\]+\\", low) or "\\appdata\\" in low:
        return False
    return not re.search(r"\\\.[^\\]+\\", low)          # .lmstudio, .cache, .vscode... belong to programs


# ------------------------------------------------------------------ safe + tools
def safe_items(con, rules):
    out, seen = [], []
    for r in con.execute("SELECT id, path, alloc, rule, mtime FROM dirs WHERE anchor=1 AND alloc >= ? "
                         "ORDER BY alloc DESC", (MB,)):
        info = rules.info(r["rule"])
        if not info or info["safety"] != "seguro" or info["cat"] == "lixeira":
            continue
        p = r["path"]
        if any(p.lower().startswith(s.lower().rstrip("\\") + "\\") for s in seen):
            continue                                       # already covered by a parent folder
        if not os.path.isdir(p) or cleaner.protection_reason(p, for_contents=True):
            continue
        seen.append(p)
        out.append({"id": item_id("safe", p), "kind": "safe", "action": "clean_contents", "path": p,
                    "size": r["alloc"], "label": info["label"], "desc": info["desc"], "dir": r["id"],
                    "mtime": r["mtime"]})
    return out


def _dir_alloc(con, path):
    r = con.execute("SELECT alloc FROM dirs WHERE lower(path)=lower(?)", (path,)).fetchone()
    return r[0] if r else None


def tool_items(con, volume, hidden):
    windir = os.environ.get("WINDIR", r"C:\Windows")
    sysd = winapi.system_drive()
    rb = cleaner.recycle_bin_info()
    est = {
        "empty_recycle": rb["bytes"],
        "wu_cache": _dir_alloc(con, os.path.join(windir, "SoftwareDistribution", "Download")) if volume == sysd else None,
        "delivery_opt": _dir_alloc(con, os.path.join(windir, r"ServiceProfiles\NetworkService\AppData\Local\Microsoft"
                                                             r"\Windows\DeliveryOptimization")) if volume == sysd else None,
        "dism": None, "restore_old": None, "hiber_reduced": None, "hiber_off": None,
    }
    if hidden:
        for it in hidden.get("items", []):
            if it["key"] == "shadow" and it["bytes"]:
                est["restore_old"] = it["bytes"]
    hib = con.execute("SELECT f.alloc FROM files f JOIN dirs d ON d.id=f.dir WHERE d.parent=0 AND "
                      "lower(f.name)='hiberfil.sys'").fetchone()
    if hib and volume == sysd:
        est["hiber_off"] = hib[0]
        est["hiber_reduced"] = hib[0] // 2
    out = []
    admin = winapi.is_admin()
    for tid, t in cleaner.TOOLS.items():
        if tid != "empty_recycle" and volume != sysd:
            continue
        if tid.startswith("hiber") and not est.get(tid):
            continue
        out.append({"id": "tool:" + tid, "kind": "tool", "action": "tool", "tool": tid, "label": t["label"],
                    "desc": t["desc"], "size": est.get(tid), "needs_admin": t["admin"] and not admin,
                    "slow": t.get("slow", False)})
    return out, rb


# ------------------------------------------------------------------ review
def review_items(con, rules, limit=60):
    now = time.time()
    out = []
    seen = set()

    def add(kind, path, size, mtime, reason, extra=None):
        if path.lower() in seen:
            return
        why = cleaner.protection_reason(path)
        if why:
            return
        seen.add(path.lower())
        it = {"id": item_id("rev", path), "kind": "review", "group": kind, "action": "recycle", "path": path,
              "size": size, "mtime": mtime, "reason": reason}
        if extra:
            it.update(extra)
        out.append(it)

    has_files = con.execute("SELECT 1 FROM files LIMIT 1").fetchone()
    if has_files:
        # a) old installers / archives in Downloads
        for r in con.execute("SELECT d.path, f.name, f.alloc, f.mtime FROM files f JOIN dirs d ON d.id=f.dir "
                             "WHERE f.alloc >= ? AND lower(d.path) LIKE '%\\downloads%' ORDER BY f.alloc DESC "
                             "LIMIT 400", (20 * MB,)):
            ext = r["name"].lower().rsplit(".", 1)[-1] if "." in r["name"] else ""
            if ext in INSTALLER_EXTS and now - r["mtime"] > 14 * DAY and re.search(r"\\users\\[^\\]+\\downloads",
                                                                                    r["path"].lower()):
                add("downloads", r["path"].rstrip("\\") + "\\" + r["name"], r["alloc"], r["mtime"],
                    "Instalador ou arquivo compactado em Downloads, parado há "
                    f"{int((now - r['mtime']) / DAY)} dias. Se já foi instalado/extraído, pode ir.")
        # b) big personal files untouched for 6 months
        for r in con.execute("SELECT d.path, f.name, f.alloc, f.mtime FROM files f JOIN dirs d ON d.id=f.dir "
                             "WHERE f.alloc >= ? AND f.mtime < ? ORDER BY f.alloc DESC LIMIT 200",
                             (500 * MB, now - 180 * DAY)):
            p = r["path"].rstrip("\\") + "\\" + r["name"]
            ext = r["name"].lower().rsplit(".", 1)[-1] if "." in r["name"] else ""
            if _user_area(p) and "\\downloads\\" not in p.lower() and ext not in BINARY_EXTS:
                add("old_big", p, r["alloc"], r["mtime"],
                    f"Arquivo grande sem alterações há {int((now - r['mtime']) / DAY)} dias.")
        # d) likely duplicates: same name and size (>= 100 MB) in personal folders
        groups = {}
        for r in con.execute("SELECT d.path, f.name, f.alloc, f.mtime FROM files f JOIN dirs d ON d.id=f.dir "
                             "WHERE f.alloc >= ? ORDER BY f.alloc DESC LIMIT 3000", (100 * MB,)):
            p = r["path"].rstrip("\\") + "\\" + r["name"]
            ext = r["name"].lower().rsplit(".", 1)[-1] if "." in r["name"] else ""
            if _user_area(p) and ext not in BINARY_EXTS:
                groups.setdefault((r["name"].lower(), r["alloc"]), []).append((r["mtime"], p, r["alloc"]))
        for (name, size), lst in groups.items():
            if len(lst) < 2:
                continue
            lst.sort(reverse=True)                       # keep the newest copy
            keep = lst[0][1]
            for mt, p, a in lst[1:]:
                add("duplicate", p, a, mt, f"Mesmo nome e tamanho de outra cópia: {keep}", {"keep": keep})
    # c) dependency folders of projects untouched for 90 days
    names = ",".join("?" * len(DEV_DIRS))
    for r in con.execute(f"SELECT path, name, alloc, mtime FROM dirs WHERE name IN ({names}) AND alloc >= ? "
                         "ORDER BY alloc DESC LIMIT 600", (*DEV_DIRS.keys(), 50 * MB)):
        low = r["path"].lower()
        parent = low.rsplit("\\", 1)[0]
        if any("\\" + n + "\\" in parent + "\\" for n in DEV_DIRS) or "\\appdata\\" in low \
                or "\\program files" in low:
            continue                                   # nested, or belongs to an installed program
        if now - (r["mtime"] or now) < 90 * DAY:
            continue
        add("dev", r["path"], r["alloc"], r["mtime"],
            f"{DEV_DIRS[r['name'].lower()].capitalize()} de um projeto sem alterações há "
            f"{int((now - r['mtime']) / DAY)} dias.")
    out.sort(key=lambda x: -x["size"])
    return out[:limit * 3]


def unknown_folders(con, rules, limit=25):
    out = []
    for r in con.execute("SELECT id, path, alloc, mtime FROM dirs WHERE rule=-1 AND depth BETWEEN 1 AND 4 "
                         "AND alloc >= ? ORDER BY alloc DESC LIMIT 200", (300 * MB,)):
        if any(r["path"].lower().startswith(o["path"].lower() + "\\") for o in out):
            continue
        kids = [{"name": k["name"], "size": k["alloc"]} for k in con.execute(
            "SELECT name, alloc FROM dirs WHERE parent=? ORDER BY alloc DESC LIMIT 8", (r["id"],))]
        files = [{"name": f["name"], "size": f["alloc"]} for f in con.execute(
            "SELECT name, alloc FROM files WHERE dir=? ORDER BY alloc DESC LIMIT 5", (r["id"],))]
        out.append({"id": item_id("unk", r["path"]), "kind": "unknown", "path": r["path"], "size": r["alloc"],
                    "mtime": r["mtime"], "dir": r["id"], "children": kids, "files": files})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ installed programs
UNINSTALL_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]
DEPENDENCY = re.compile(r"visual c\+\+|redistributable|\.net (runtime|framework|desktop)|microsoft \.net|"
                        r"webview2|windows sdk|windows software development|directx|vcredist|driver|"
                        r"chipset|realtek|intel\(r\)|nvidia (physx|graphics|hd audio|frameview)|"
                        r"update health|microsoft edge|windows app runtime|gaming services", re.I)


def _val(k, name):
    try:
        return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def installed_programs(con=None):
    progs, seen = [], set()
    dir_sizes = {}
    if con is not None:
        def lookup(path):
            if not path:
                return None
            p = path.strip().strip('"').rstrip("\\")
            if p.lower() in dir_sizes:
                return dir_sizes[p.lower()]
            r = con.execute("SELECT alloc, mtime FROM dirs WHERE lower(path)=lower(?)", (p,)).fetchone()
            dir_sizes[p.lower()] = (r[0], r[1]) if r else None
            return dir_sizes[p.lower()]
    else:
        lookup = lambda p: None  # noqa: E731
    for hive, key in UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hive, key)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    sub = winreg.EnumKey(root, i)
                    k = winreg.OpenKey(root, sub)
                except OSError:
                    continue
                with k:
                    name = _val(k, "DisplayName")
                    unin = _val(k, "UninstallString")
                    if not name or not unin or _val(k, "SystemComponent") == 1 or _val(k, "ParentKeyName") \
                            or (_val(k, "ReleaseType") or "") in ("Update", "Hotfix", "Security Update"):
                        continue
                    ver = _val(k, "DisplayVersion") or ""
                    dedup = (name.lower(), ver)
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                    loc = _val(k, "InstallLocation") or ""
                    icon = (_val(k, "DisplayIcon") or "").split(",")[0].strip('"')
                    measured = lookup(loc) or (lookup(os.path.dirname(icon)) if icon.lower().endswith(".exe") else None)
                    est = (_val(k, "EstimatedSize") or 0) * 1024
                    date = _val(k, "InstallDate") or ""
                    ts = None
                    if re.fullmatch(r"\d{8}", str(date)):
                        try:
                            ts = time.mktime(time.strptime(str(date), "%Y%m%d"))
                        except ValueError:
                            ts = None
                    progs.append({
                        "id": item_id("prog", f"{hive}\\{key}\\{sub}"), "kind": "program", "name": name,
                        "version": ver, "publisher": _val(k, "Publisher") or "", "installed": ts,
                        "size": measured[0] if measured else est, "size_measured": bool(measured),
                        "last_change": measured[1] if measured else None, "location": loc,
                        "drive": (loc or icon)[:2].upper() if re.match(r"^[a-z]:", (loc or icon), re.I) else "",
                        "dependency": bool(DEPENDENCY.search(name)),
                        "scope": "usuário" if hive == winreg.HKEY_CURRENT_USER else "todos",
                        "_reg": (hive, key, sub),
                    })
    progs.sort(key=lambda p: -(p["size"] or 0))
    return progs


def uninstall_command(uninstall_string):
    """Split a registry UninstallString into (exe, args). MsiExec /I (repair) becomes /X (remove)."""
    s = uninstall_string.strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        exe, args = s[1:end], s[end + 1:].strip()
    else:
        m = re.match(r"(.+?\.exe)\s*(.*)$", s, re.I)
        exe, args = (m.group(1), m.group(2)) if m else (s, "")
    if os.path.basename(exe).lower() == "msiexec.exe":
        args = re.sub(r"/I(\{)", r"/X\1", args, flags=re.I)
    return exe, args


def launch_uninstaller(prog_id):
    """Start the program's own uninstaller (it shows its own window and asks for admin itself)."""
    for p in installed_programs():
        if p["id"] == prog_id:
            hive, key, sub = p["_reg"]
            with winreg.OpenKey(hive, key + "\\" + sub) as k:
                exe, args = uninstall_command(_val(k, "UninstallString"))
            rc = winapi.shell32.ShellExecuteW(None, "open", exe, args or None, None, 1)
            if rc <= 32:
                raise OSError(rc, "não foi possível abrir o desinstalador")
            return p["name"]
    raise ValueError("programa não encontrado")


# ------------------------------------------------------------------ all together
def build(volume):
    s = latest_snapshot(volume)
    if not s:
        return {"available": False}
    con, rules = _open(s)
    try:
        hidden_row = con.execute("SELECT value FROM meta WHERE key='hidden'").fetchone()
        hidden = json.loads(hidden_row[0]) if hidden_row else None
        tools, rb = tool_items(con, volume, hidden)
        progs = installed_programs(con)
        for p in progs:
            p.pop("_reg", None)
        return {"available": True, "snapshot": s, "safe": safe_items(con, rules), "tools": tools,
                "review": review_items(con, rules), "programs": progs,
                "unknown": unknown_folders(con, rules), "recycle_bin": rb, "admin": winapi.is_admin(),
                "safety": rules.data.get("safety", {})}
    finally:
        con.close()
