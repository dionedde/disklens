"""NTFS change journal (USN): which files were created, grew, shrank or were deleted, and when.

The journal records every change with a timestamp but no sizes. Each agent run (every 15 min):
  1. reads the journal records written since the last run (needs admin);
  2. folds them per file (latest name, change reasons, first/last change time);
  3. opens each touched file by its NTFS id to get its current size and full path;
  4. compares with the last known size (previous run, or the latest snapshot taken before the
     change, or 0 for files created inside the window) and stores the difference.
Files that changed by >= 1 MB are stored one by one; smaller changes are summed per folder.
On the first run the whole journal is read, so recent history (often days) is available at once.
"""
import ctypes
import ctypes.wintypes as wt
import os
import sqlite3
import struct
import time

from . import db, winapi
from .i18n import tr
from .config import data_dir

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB

R_DATA_OVERWRITE = 0x00000001
R_DATA_EXTEND = 0x00000002
R_DATA_TRUNCATION = 0x00000004
R_FILE_CREATE = 0x00000100
R_FILE_DELETE = 0x00000200
R_RENAME_NEW = 0x00002000
R_STREAM_CHANGE = 0x00200000
R_CLOSE = 0x80000000
DATA_REASONS = (R_DATA_OVERWRITE | R_DATA_EXTEND | R_DATA_TRUNCATION | R_FILE_CREATE | R_FILE_DELETE
                | R_RENAME_NEW | R_STREAM_CHANGE)
FILE_ATTRIBUTE_DIRECTORY = 0x10

BIG = 1024 * 1024          # changes >= 1 MB are kept file by file
BUCKET = 900               # 15 minutes
EPOCH_DIFF = 11644473600

GENERIC_READ = 0x80000000
FILE_READ_ATTRIBUTES = 0x80
SHARE_ALL = 7
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID = wt.HANDLE(-1).value

SCHEMA = """
CREATE TABLE IF NOT EXISTS usn_state (volume TEXT PRIMARY KEY, journal_id INTEGER, next_usn INTEGER,
    since REAL, updated REAL);
CREATE TABLE IF NOT EXISTS usn_files (volume TEXT, frn INTEGER, path TEXT, alloc INTEGER, last_seen REAL,
    PRIMARY KEY (volume, frn));
CREATE TABLE IF NOT EXISTS usn_changes (volume TEXT, ts REAL, first_ts REAL, run_ts REAL, path TEXT,
    kind TEXT, delta INTEGER, size_after INTEGER, nfiles INTEGER, is_dir_agg INTEGER);
CREATE INDEX IF NOT EXISTS usn_changes_ts ON usn_changes(volume, ts);
CREATE TABLE IF NOT EXISTS usn_buckets (volume TEXT, bucket REAL, records INTEGER, created INTEGER,
    deleted INTEGER, grow INTEGER, shrink INTEGER, PRIMARY KEY (volume, bucket));
"""


def conn():
    c = sqlite3.connect(data_dir() / "usn.db", timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


# ------------------------------------------------------------------ Win32 plumbing
class FILE_ID_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("Type", wt.DWORD), ("FileId", ctypes.c_longlong),
                ("_pad", ctypes.c_longlong)]


class FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [("AllocationSize", ctypes.c_longlong), ("EndOfFile", ctypes.c_longlong),
                ("NumberOfLinks", wt.DWORD), ("DeletePending", ctypes.c_ubyte), ("Directory", ctypes.c_ubyte)]


class READ_USN_JOURNAL_DATA_V0(ctypes.Structure):
    _fields_ = [("StartUsn", ctypes.c_longlong), ("ReasonMask", wt.DWORD), ("ReturnOnlyOnClose", wt.DWORD),
                ("Timeout", ctypes.c_ulonglong), ("BytesToWaitFor", ctypes.c_ulonglong),
                ("UsnJournalID", ctypes.c_ulonglong)]


k32 = winapi.k32
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
k32.OpenFileById.restype = wt.HANDLE
k32.OpenFileById.argtypes = [wt.HANDLE, ctypes.POINTER(FILE_ID_DESCRIPTOR), wt.DWORD, wt.DWORD,
                             ctypes.c_void_p, wt.DWORD]
k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
k32.GetFileInformationByHandleEx.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
k32.GetFinalPathNameByHandleW.argtypes = [wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]


def parse_records(raw, off=0):
    """Decode USN_RECORD_V2 entries -> (frn, parent, usn, unix_ts, reason, attrs, name)."""
    while off + 60 <= len(raw):
        reclen, major = struct.unpack_from("<IH", raw, off)
        if reclen == 0:
            break
        if major == 2:
            frn, parent, usn, ft, reason, _src, _sec, attrs, nlen, noff = \
                struct.unpack_from("<QQqqIIIIHH", raw, off + 8)
            name = raw[off + noff: off + noff + nlen].decode("utf-16-le", "replace")
            yield frn, parent, usn, ft / 1e7 - EPOCH_DIFF, reason, attrs, name
        off += reclen


class Volume:
    def __init__(self, volume):
        self.volume = volume.upper()[:2]
        self.h = k32.CreateFileW("\\\\.\\" + self.volume, GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING, 0, None)
        if self.h in (None, INVALID):
            raise PermissionError(ctypes.get_last_error(), "volume (administrador necessário)")
        self.root = k32.CreateFileW(self.volume + "\\", FILE_READ_ATTRIBUTES, SHARE_ALL, None, OPEN_EXISTING,
                                    FILE_FLAG_BACKUP_SEMANTICS, None)
        self._buf = ctypes.create_unicode_buffer(1024)
        self._dir_cache = {}

    def close(self):
        for h in (self.h, self.root):
            if h not in (None, INVALID):
                k32.CloseHandle(h)

    def query(self):
        buf = ctypes.create_string_buffer(64)
        n = wt.DWORD()
        if not k32.DeviceIoControl(self.h, FSCTL_QUERY_USN_JOURNAL, None, 0, buf, 64, ctypes.byref(n), None):
            raise OSError(ctypes.get_last_error(), "journal USN indisponível")
        jid, first, nxt = struct.unpack_from("<Qqq", buf.raw)
        return jid, first, nxt

    def read(self, start, end, jid):
        """Yield (frn, parent, usn, unix_ts, reason, attrs, name) for records in [start, end)."""
        rd = READ_USN_JOURNAL_DATA_V0(start, 0xFFFFFFFF, 0, 0, 0, jid)
        size = 1 << 20
        buf = ctypes.create_string_buffer(size)
        n = wt.DWORD()
        while rd.StartUsn < end:
            if not k32.DeviceIoControl(self.h, FSCTL_READ_USN_JOURNAL, ctypes.byref(rd), ctypes.sizeof(rd),
                                       buf, size, ctypes.byref(n), None):
                raise OSError(ctypes.get_last_error(), "falha ao ler o journal USN")
            if n.value <= 8:
                break
            raw = buf.raw[:n.value]
            nxt = struct.unpack_from("<q", raw, 0)[0]
            for rec in parse_records(raw, 8):
                if rec[2] >= end:
                    return
                yield rec
            if nxt <= rd.StartUsn:
                break
            rd.StartUsn = nxt

    def _open(self, frn):
        d = FILE_ID_DESCRIPTOR(ctypes.sizeof(FILE_ID_DESCRIPTOR), 0, ctypes.c_longlong(frn & 0xFFFFFFFFFFFFFFFF).value, 0)
        h = k32.OpenFileById(self.root, ctypes.byref(d), FILE_READ_ATTRIBUTES, SHARE_ALL, None,
                             FILE_FLAG_BACKUP_SEMANTICS)
        return None if h in (None, INVALID) else h

    def _path(self, h):
        n = k32.GetFinalPathNameByHandleW(h, self._buf, 1024, 0)
        if not n or n >= 1024:
            return None
        p = self._buf.value
        return p[4:] if p.startswith("\\\\?\\") else p

    def stat(self, frn):
        """(full path, allocated bytes) of a live file, or None if it no longer exists."""
        h = self._open(frn)
        if h is None:
            return None
        try:
            info = FILE_STANDARD_INFO()
            if not k32.GetFileInformationByHandleEx(h, 1, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            if info.Directory:
                return None
            return self._path(h), info.AllocationSize
        finally:
            k32.CloseHandle(h)

    def dir_path(self, frn):
        if frn in self._dir_cache:
            return self._dir_cache[frn]
        h = self._open(frn)
        p = None
        if h is not None:
            try:
                p = self._path(h)
            finally:
                k32.CloseHandle(h)
        self._dir_cache[frn] = p
        return p


# ------------------------------------------------------------------ snapshot baseline
class SnapshotSizes:
    """Size of a file in the latest snapshot taken before a given time (lazy, cached)."""

    def __init__(self, volume):
        self.snaps = [s for s in db.list_snapshots() if s["volume"] == volume and s["status"] == "done"
                      and db.snap_path(s["id"]).exists()]
        self._cons, self._dirs = {}, {}

    def lookup(self, path, before_ts):
        s = next((s for s in self.snaps if s["finished"] <= before_ts), None)
        if s is None or not path:
            return None
        sid = s["id"]
        if sid not in self._cons:
            c = sqlite3.connect(f"file:{db.snap_path(sid)}?mode=ro", uri=True)
            self._cons[sid] = c
            self._dirs[sid] = {p.lower(): i for p, i in c.execute("SELECT path, id FROM dirs")}
        folder, _, name = path.rpartition("\\")
        if len(folder) == 2:
            folder += "\\"
        did = self._dirs[sid].get(folder.lower())
        if did is None:
            return None
        r = self._cons[sid].execute("SELECT alloc FROM files WHERE dir=? AND name=? COLLATE NOCASE",
                                    (did, name)).fetchone()
        return r[0] if r else None

    def close(self):
        for c in self._cons.values():
            c.close()


# ------------------------------------------------------------------ collection
def collect(volume, max_records=3_000_000, vol=None):
    """Read new journal records and store size changes. Returns a short summary dict.
    `vol` can be a stand-in object with the same methods (used by the tests)."""
    volume = volume.upper()[:2]
    started = time.time()
    vol = vol or Volume(volume)
    con = conn()
    try:
        jid, first, nxt = vol.query()
        st = con.execute("SELECT * FROM usn_state WHERE volume=?", (volume,)).fetchone()
        gap = None
        if st is None:
            start = first                      # first run: the whole journal
        elif st["journal_id"] != jid or st["next_usn"] < first:
            start = first                      # journal recreated or wrapped while we were away
            gap = tr("O journal do NTFS foi reiniciado ou sobrescrito: alterações anteriores a este ponto não estão no histórico.")
        else:
            start = st["next_usn"]

        files = {}                             # frn -> [name, parent, reasons, first_ts, last_ts, created, deleted]
        buckets = {}
        n = 0
        for frn, parent, _usn, ts, reason, attrs, name in vol.read(start, nxt, jid):
            n += 1
            if n > max_records:
                break
            if attrs & FILE_ATTRIBUTE_DIRECTORY or not reason & DATA_REASONS:
                continue
            b = buckets.setdefault(int(ts // BUCKET * BUCKET), [0, 0, 0, 0, 0])
            b[0] += 1
            f = files.get(frn)
            if f is None:
                f = files[frn] = [name, parent, 0, ts, ts, False, False]
            f[0], f[1] = name, parent
            f[2] |= reason
            f[4] = ts
            if reason & R_FILE_CREATE:
                f[5] = True
                b[1] += 1
            if reason & R_FILE_DELETE:
                f[6] = True
                b[2] += 1

        known = {r["frn"]: (r["path"], r["alloc"]) for r in con.execute(
            "SELECT frn, path, alloc FROM usn_files WHERE volume=?", (volume,))} if files else {}
        snap = SnapshotSizes(volume)
        run_ts = time.time()
        rows, agg, upserts, deletes = [], {}, [], []
        churn = 0
        for frn, (name, parent, reasons, fts, lts, created, deleted) in files.items():
            prev_path, prev = known.get(frn, (None, None))
            if deleted:
                path = prev_path
                if path is None:
                    folder = vol.dir_path(parent)
                    path = (folder.rstrip("\\") + "\\" + name) if folder else None
                if prev is None and not created:
                    prev = snap.lookup(path, fts)
                deletes.append(frn)
                if created and prev is None:
                    churn += 1                 # temporary file: created and deleted between runs
                    continue
                if not prev:
                    continue
                kind, after, delta = "deleted", 0, -prev
            else:
                s = vol.stat(frn)
                if s is None or not s[0]:
                    continue
                path, after = s
                if prev is None:
                    prev = 0 if created else snap.lookup(path, fts)
                upserts.append((volume, frn, path, after, run_ts))
                if prev is None:               # existed before, size unknown: start tracking now
                    continue
                delta = after - prev
                if delta == 0:
                    continue
                kind = "new" if created and prev == 0 else ("grow" if delta > 0 else "shrink")
            bkt = buckets.setdefault(int(lts // BUCKET * BUCKET), [0, 0, 0, 0, 0])
            bkt[3 if delta > 0 else 4] += delta
            if abs(delta) >= BIG or (kind in ("new", "deleted") and abs(delta) >= BIG):
                rows.append((volume, lts, fts, run_ts, path, kind, delta, after, 1, 0))
            elif path:
                folder = path.rpartition("\\")[0]
                a = agg.setdefault(folder, [0, 0, lts, fts])
                a[0] += delta
                a[1] += 1
                a[2] = max(a[2], lts)
                a[3] = min(a[3], fts)
        for folder, (delta, cnt, lts, fts) in agg.items():
            if delta:
                rows.append((volume, lts, fts, run_ts, folder, "small", delta, None, cnt, 1))
        snap.close()

        with con:
            con.executemany("INSERT INTO usn_changes VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            con.executemany("INSERT OR REPLACE INTO usn_files VALUES (?,?,?,?,?)", upserts)
            con.executemany("DELETE FROM usn_files WHERE volume=? AND frn=?", [(volume, f) for f in deletes])
            for bk, (recs, cr, de, gr, sh) in buckets.items():
                con.execute("INSERT INTO usn_buckets VALUES (?,?,?,?,?,?,?) ON CONFLICT(volume, bucket) DO UPDATE "
                            "SET records=records+excluded.records, created=created+excluded.created, "
                            "deleted=deleted+excluded.deleted, grow=grow+excluded.grow, shrink=shrink+excluded.shrink",
                            (volume, bk, recs, cr, de, gr, sh))
            since = st["since"] if st and not gap else (min(buckets) if buckets else run_ts)
            con.execute("INSERT OR REPLACE INTO usn_state VALUES (?,?,?,?,?)", (volume, jid, nxt, since, run_ts))
            # forget files untouched for 60 days, keep detailed history for 180 days
            con.execute("DELETE FROM usn_files WHERE volume=? AND last_seen < ?", (volume, run_ts - 60 * 86400))
            con.execute("DELETE FROM usn_changes WHERE volume=? AND ts < ?", (volume, run_ts - 180 * 86400))
        if gap:
            db.add_event(run_ts, volume, "usn_gap", tr("Lacuna no journal do NTFS"), gap)
        return {"volume": volume, "records": n, "files": len(files), "changes": len(rows), "churn": churn,
                "seconds": round(time.time() - started, 2), "first_run": st is None,
                "since": since, "gap": bool(gap)}
    finally:
        vol.close()
        con.close()


# ------------------------------------------------------------------ queries for the UI
def activity(volume, t0, t1, limit=40):
    p = data_dir() / "usn.db"
    if not p.exists():
        return {"available": False}
    con = conn()
    try:
        st = con.execute("SELECT * FROM usn_state WHERE volume=?", (volume,)).fetchone()
        if not st:
            return {"available": False}
        files = [dict(r) for r in con.execute(
            "SELECT path, MAX(kind) AS kind, SUM(delta) AS delta, MAX(ts) AS ts, MIN(first_ts) AS first_ts, "
            "MAX(size_after) AS size_after, SUM(nfiles) AS nfiles, MAX(is_dir_agg) AS is_dir_agg, "
            "SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END) AS gross, "
            "(SELECT kind FROM usn_changes c2 WHERE c2.volume=usn_changes.volume AND c2.path=usn_changes.path "
            " AND c2.ts BETWEEN ? AND ? ORDER BY c2.ts DESC LIMIT 1) AS last_kind "
            "FROM usn_changes WHERE volume=? AND ts BETWEEN ? AND ? GROUP BY path, is_dir_agg",
            (t0, t1, volume, t0, t1))]
        for f in files:
            f["kind"] = f.pop("last_kind") or f["kind"]
        grow = sorted((f for f in files if f["delta"] >= BIG or (f["delta"] > 0 and f["is_dir_agg"])),
                      key=lambda f: -f["delta"])
        shrink = sorted((f for f in files if f["delta"] <= -BIG or (f["delta"] < 0 and f["is_dir_agg"])),
                        key=lambda f: f["delta"])
        transient = sorted((f for f in files if not f["is_dir_agg"] and abs(f["delta"]) < BIG
                            and f["gross"] >= BIG), key=lambda f: -f["gross"])
        folders = {}
        for f in files:
            folder = f["path"] if f["is_dir_agg"] else f["path"].rpartition("\\")[0]
            x = folders.setdefault(folder, {"path": folder, "delta": 0, "nfiles": 0, "ts": 0})
            x["delta"] += f["delta"]
            x["nfiles"] += f["nfiles"] or 1
            x["ts"] = max(x["ts"], f["ts"])
        top_folders = sorted(folders.values(), key=lambda x: -abs(x["delta"]))[:15]
        step = BUCKET if t1 - t0 <= 3 * 86400 else 3600 if t1 - t0 <= 21 * 86400 else 86400
        bk = {}
        for r in con.execute("SELECT bucket, records, created, deleted, grow, shrink FROM usn_buckets "
                             "WHERE volume=? AND bucket BETWEEN ? AND ?", (volume, t0 - BUCKET, t1)):
            k = int(r["bucket"] // step * step)
            x = bk.setdefault(k, [k, 0, 0, 0, 0, 0])
            for i, col in enumerate(("records", "created", "deleted", "grow", "shrink"), 1):
                x[i] += r[col]
        totals = {"grow": sum(b[4] for b in bk.values()), "shrink": sum(b[5] for b in bk.values()),
                  "net": sum(f["delta"] for f in files),
                  "created": sum(b[2] for b in bk.values()), "deleted": sum(b[3] for b in bk.values())}
        tracking = con.execute("SELECT MIN(run_ts) FROM usn_changes WHERE volume=?", (volume,)).fetchone()[0]
        return {"available": True, "since": st["since"], "updated": st["updated"], "step": step,
                "tracking_since": tracking or st["updated"],
                "grow": grow[:limit], "shrink": shrink[:15], "transient": transient[:15], "folders": top_folders,
                "buckets": sorted(bk.values()), "totals": totals}
    finally:
        con.close()


def selftest(out_path):
    """Run one collection (as admin) and write a readable report: used to validate the setup."""
    import json
    import traceback
    rep = {"admin": winapi.is_admin(), "backup_priv": winapi.enable_privilege("SeBackupPrivilege")}
    try:
        rep["collect"] = collect(winapi.system_drive())
        a = activity(winapi.system_drive(), time.time() - 7 * 86400, time.time(), limit=10)
        rep["top_grow"] = [(f["path"], f["delta"], f["kind"]) for f in a.get("grow", [])[:10]]
        rep["totals"] = a.get("totals")
    except Exception:
        rep["error"] = traceback.format_exc()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    import sys
    selftest(sys.argv[1] if len(sys.argv) > 1 else os.path.join(str(data_dir()), "usn_selftest.json"))
