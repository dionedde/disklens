"""SQLite storage.

* ``index.db`` lists every snapshot (one row per scan).
* ``snap_<id>.db`` holds one scan: the folder tree with sizes (kept for history) and the full
  file list (kept only for the newest snapshot of each volume; older ones are compacted).
"""
import json
import sqlite3
import time

from .config import data_dir

SNAP_SCHEMA = """
CREATE TABLE dirs (
    id INTEGER PRIMARY KEY, parent INTEGER, name TEXT, path TEXT, depth INTEGER,
    alloc INTEGER, logical INTEGER, own_alloc INTEGER, nfiles INTEGER, ndirs INTEGER,
    own_files INTEGER, mtime REAL, rule INTEGER, anchor INTEGER, anchor_alloc INTEGER,
    flags INTEGER
);
CREATE TABLE files (
    dir INTEGER, name TEXT, alloc INTEGER, logical INTEGER, mtime REAL,
    attrs INTEGER, flags INTEGER, rule INTEGER
);
CREATE TABLE ext_stats (ext TEXT PRIMARY KEY, alloc INTEGER, logical INTEGER, n INTEGER);
CREATE TABLE cat_stats (cat TEXT PRIMARY KEY, alloc INTEGER, n INTEGER);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

SNAP_INDEXES = """
CREATE INDEX dirs_parent ON dirs(parent, alloc DESC);
CREATE INDEX dirs_anchor ON dirs(anchor, anchor_alloc DESC);
CREATE INDEX files_dir ON files(dir, alloc DESC);
CREATE INDEX files_alloc ON files(alloc DESC);
"""

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, volume TEXT, started REAL, finished REAL,
    status TEXT, total INTEGER, used INTEGER, free INTEGER, scanned INTEGER,
    nfiles INTEGER, ndirs INTEGER, denied INTEGER, admin INTEGER, compacted INTEGER DEFAULT 0,
    seconds_paused REAL DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS samples (
    ts REAL, volume TEXT, total INTEGER, used INTEGER, free INTEGER,
    pagefile INTEGER, hiberfil INTEGER, shadow INTEGER
);
CREATE INDEX IF NOT EXISTS samples_vol_ts ON samples(volume, ts);
CREATE TABLE IF NOT EXISTS events (
    ts REAL, volume TEXT, kind TEXT, title TEXT, detail TEXT,
    UNIQUE(ts, kind, title)
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""

KEEP_FILES_MIN = 10 * 1024 * 1024   # older snapshots keep only files >= 10 MB (for "new big files")

# flags
F_DENIED = 1        # folder could not be read
F_ERROR = 2
F_HARDLINK = 4      # file has several names (counted once)
F_HARDLINK_DUP = 8  # this name is a duplicate of an already-counted file -> 0 bytes
F_CLOUD = 16        # online-only (OneDrive etc.): takes no space
F_COMPRESSED = 32   # NTFS/WOF compressed or sparse: real size < logical size


def index_conn():
    con = sqlite3.connect(data_dir() / "index.db", timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(INDEX_SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
    for col in ("hidden", "cats"):                 # added in phases 2 and 3
        if col not in cols:
            con.execute(f"ALTER TABLE snapshots ADD COLUMN {col} TEXT")
    return con


def add_sample(ts, volume, total, used, free, pagefile=None, hiberfil=None, shadow=None):
    with index_conn() as con:
        con.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?)",
                    (ts, volume, total, used, free, pagefile, hiberfil, shadow))


def add_event(ts, volume, kind, title, detail=""):
    with index_conn() as con:
        con.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)", (ts, volume, kind, title, detail))


def last_event_ts(kinds):
    with index_conn() as con:
        r = con.execute(f"SELECT MAX(ts) FROM events WHERE kind IN ({','.join('?' * len(kinds))})",
                        kinds).fetchone()
        return r[0]


def snapshot_cats(s):
    """Category totals of a snapshot (cached in index.db; backfilled for old snapshots)."""
    if s.get("cats"):
        return json.loads(s["cats"])
    p = snap_path(s["id"])
    if s["status"] != "done" or not p.exists():
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    cats = {r[0]: r[1] for r in c.execute("SELECT cat, alloc FROM cat_stats")}
    c.close()
    update_snapshot(s["id"], cats=json.dumps(cats))
    return cats


def prune(volume, keep_days=90):
    """Older than keep_days: keep one snapshot per ISO week (the last one)."""
    cutoff = time.time() - keep_days * 86400
    with index_conn() as con:
        rows = con.execute("SELECT id, finished FROM snapshots WHERE volume=? AND status='done' "
                           "AND finished < ? ORDER BY finished", (volume, cutoff)).fetchall()
        failed = con.execute("SELECT id FROM snapshots WHERE status IN ('error','cancelled') AND started < ?",
                             (time.time() - 7 * 86400,)).fetchall()
    weeks = {}
    for r in rows:
        weeks[time.strftime("%G-%V", time.localtime(r["finished"]))] = r["id"]
    keep = set(weeks.values())
    drop = [r["id"] for r in rows if r["id"] not in keep] + [r["id"] for r in failed]
    for sid in drop:
        try:
            snap_path(sid).unlink(missing_ok=True)
        except OSError:
            continue
        with index_conn() as con:
            con.execute("DELETE FROM snapshots WHERE id=?", (sid,))
    return len(drop)


def snap_path(snap_id):
    return data_dir() / f"snap_{snap_id}.db"


def open_snap(snap_id):
    p = snap_path(snap_id)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def create_snapshot(volume, admin):
    with index_conn() as con:
        cur = con.execute("INSERT INTO snapshots(volume, started, status, admin) VALUES (?,?,?,?)",
                          (volume, time.time(), "running", int(admin)))
        return cur.lastrowid


def update_snapshot(snap_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with index_conn() as con:
        con.execute(f"UPDATE snapshots SET {cols} WHERE id=?", (*fields.values(), snap_id))


def list_snapshots():
    with index_conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM snapshots ORDER BY id DESC")]


def get_snapshot(snap_id):
    with index_conn() as con:
        r = con.execute("SELECT * FROM snapshots WHERE id=?", (snap_id,)).fetchone()
        return dict(r) if r else None


def compact_old_snapshots(volume, keep_id):
    """Shrink older snapshots of this volume: keep folder sizes and only files >= 10 MB."""
    with index_conn() as con:
        rows = con.execute("SELECT id FROM snapshots WHERE volume=? AND status='done' "
                           "AND compacted=0 AND id<>?", (volume, keep_id)).fetchall()
    for r in rows:
        p = snap_path(r["id"])
        if not p.exists():
            continue
        try:
            c = sqlite3.connect(p)
            c.execute("DELETE FROM files WHERE alloc < ?", (KEEP_FILES_MIN,))
            c.commit()
            c.execute("VACUUM")
            c.close()
            update_snapshot(r["id"], compacted=1)
        except sqlite3.Error:
            pass
