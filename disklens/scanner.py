"""Full-volume scan -> snap_<id>.db.

Run as its own process (``python -m disklens.scanner --volume C:``) so it can drop itself to
background priority without slowing the UI. While it runs it writes ``scan_status.json``
and obeys ``scan_control.json`` ({"action": "cancel"|"force"}).
"""
import argparse
import itertools
import json
import msvcrt
import os
import queue
import shutil
import sqlite3
import sys
import threading
import time
import traceback

from . import db, probes, winapi
from .i18n import tr
from .config import data_dir, load as load_config
from .loadguard import LoadGuard
from .rules import Rules

PREFIX = "\\\\?\\"
HARDLINK_MIN = 65536      # outside WINDIR, files this big are checked for hard links
_DONE = object()


class Cancelled(Exception):
    pass


def lock_path():
    return data_dir() / "scan.lock"


def try_lock():
    """Exclusive lock held for the life of the scanner process (auto-released if it dies)."""
    f = open(lock_path(), "a+")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        f.close()
        return None


def is_scan_running():
    f = try_lock()
    if f is None:
        return True
    f.seek(0)
    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    f.close()
    return False


class Scanner:
    def __init__(self, volume, out_db, guard, rules, workers=2):
        self.volume = volume.upper().rstrip("\\")          # "C:"
        self.root = self.volume + "\\"
        self.cluster = winapi.cluster_size(self.root)
        self.guard = guard
        self.rules = rules
        self.nworkers = workers
        windir = os.environ.get("WINDIR", "")
        self.windir = (windir.lower() + "\\") if windir[:2].upper() == self.volume else None

        self.q = queue.Queue()
        self.out = queue.Queue(maxsize=4000)
        self.stop = threading.Event()
        self.ids = itertools.count(2)
        self.inodes = set()
        self.inode_lock = threading.Lock()

        # writer-side state (main thread only)
        self.dirs = {1: [0, self.volume, self.root, 0]}  # id -> [parent, name, path, depth]
        self.own = {}        # id -> (alloc, logical, nfiles, mtime, flags)
        self.ext = {}        # ext -> [alloc, logical, n]
        self.over = {}       # rule idx -> [alloc, n] (files with their own rule)
        self.nfiles = 0
        self.bytes = 0
        self.denied = 0
        self.current = self.root
        self.started = time.time()

        self.con = sqlite3.connect(out_db)
        self.con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")
        self.con.executescript(db.SNAP_SCHEMA)

    # ---------------------------------------------------------------- workers
    def _alloc(self, n):
        c = self.cluster
        return -(-n // c) * c

    def scan_dir(self, dir_id, path, depth):
        flags = 0
        files, subdirs = [], []
        own_a = own_l = nf = 0
        mt = 0.0
        ext_local, over_local = {}, {}
        is_win = self.windir is not None and (path.lower() + "\\").startswith(self.windir)
        try:
            it = os.scandir(PREFIX + path)
        except PermissionError:
            flags |= db.F_DENIED
            it = None
        except OSError:
            flags |= db.F_ERROR
            it = None
        if it is not None:
            base = path if path.endswith("\\") else path + "\\"
            with it:
                for e in it:
                    try:
                        if e.is_junction() or e.is_symlink():
                            continue        # links point elsewhere: never count twice
                        if e.is_dir(follow_symlinks=False):
                            cid = next(self.ids)
                            subdirs.append((cid, e.name))
                            self.q.put((cid, base + e.name, depth + 1))
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = st.st_file_attributes
                    size = st.st_size
                    ff = 0
                    if attrs & winapi.FILE_ATTRIBUTE_REPARSE_POINT and \
                            st.st_reparse_tag & winapi.REPARSE_NAME_SURROGATE_BIT:
                        continue
                    if attrs & (winapi.FILE_ATTRIBUTE_OFFLINE | winapi.FILE_ATTRIBUTE_RECALL_ON_OPEN
                                | winapi.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
                        alloc = 0
                        ff |= db.F_CLOUD
                    elif attrs & (winapi.FILE_ATTRIBUTE_COMPRESSED | winapi.FILE_ATTRIBUTE_SPARSE_FILE) or (
                            attrs & winapi.FILE_ATTRIBUTE_REPARSE_POINT
                            and st.st_reparse_tag == winapi.IO_REPARSE_TAG_WOF):
                        phys = winapi.compressed_size(e.path)
                        alloc = self._alloc(size if phys is None else phys)
                        ff |= db.F_COMPRESSED
                    else:
                        alloc = self._alloc(size)
                    if size >= HARDLINK_MIN or (is_win and size > 0):
                        # Hard links (WinSxS <-> System32, Edge/EdgeWebView/EdgeCore, conda pkgs,
                        # Windows containers...): count each physical file once. scandir does not
                        # report the link count, so outside Windows only files >= 64 KB are checked
                        # (one extra stat each; they hold ~98% of the duplicated bytes).
                        try:
                            s2 = os.stat(e.path)
                            if s2.st_nlink > 1:
                                ff |= db.F_HARDLINK
                                with self.inode_lock:
                                    if s2.st_ino in self.inodes:
                                        ff |= db.F_HARDLINK_DUP
                                        alloc = 0
                                    else:
                                        self.inodes.add(s2.st_ino)
                        except OSError:
                            pass
                    name = e.name
                    low = name.lower()
                    dot = low.rfind(".")
                    ext = low[dot + 1:] if 0 <= dot and len(low) - dot <= 11 else ""
                    rule = self.rules.match_file(low, ext)
                    if rule is not None and low in self.rules.named_files:
                        ext = "#" + low      # pagefile.sys etc. get their own entry in "types"
                    m = st.st_mtime
                    if m > mt:
                        mt = m
                    nf += 1
                    if rule is None:
                        own_a += alloc
                    else:
                        o = over_local.setdefault(rule, [0, 0])
                        o[0] += alloc
                        o[1] += 1
                    own_l += size
                    x = ext_local.get(ext)
                    if x is None:
                        ext_local[ext] = [alloc, size, 1]
                    else:
                        x[0] += alloc
                        x[1] += size
                        x[2] += 1
                    files.append((dir_id, name, alloc, size, m, attrs, ff, rule))
        nplain = nf - sum(v[1] for v in over_local.values())
        self.out.put((dir_id, path, flags, own_a, own_l, nf, mt, files, subdirs, ext_local, over_local,
                      nplain))

    def worker(self):
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                if self.stop.is_set() or not self.guard.wait(self.stop):
                    continue
                self.scan_dir(*item)
            except Exception:
                traceback.print_exc()
            finally:
                self.q.task_done()

    # ---------------------------------------------------------------- writer / main
    def write_status(self, state, extra=None):
        st = {"state": state, "volume": self.volume, "pid": os.getpid(),
              "files": self.nfiles, "dirs": len(self.dirs), "bytes": self.bytes,
              "denied": self.denied, "current": self.current,
              "elapsed": time.time() - self.started,
              "paused": self.guard.paused, "pause_reason": self.guard.reason,
              "cpu_other": round(self.guard.last_cpu, 1), "guard": self.guard.enabled,
              "updated": time.time()}
        if extra:
            st.update(extra)
        write_status(st)

    def check_control(self):
        p = data_dir() / "scan_control.json"
        if not p.exists():
            return
        try:
            action = json.loads(p.read_text(encoding="utf-8")).get("action")
        except (OSError, ValueError):
            action = None
        try:
            p.unlink()
        except OSError:
            pass
        if action == "cancel":
            self.stop.set()
            self.guard.shutdown()
        elif action == "force":
            self.guard.force()

    def consume(self, msg):
        dir_id, path, flags, own_a, own_l, nf, mt, files, subdirs, ext_l, over_l, nplain = msg
        self.current = path
        depth = self.dirs[dir_id][3] if dir_id in self.dirs else path.count("\\")
        for cid, name in subdirs:
            base = path if path.endswith("\\") else path + "\\"
            self.dirs[cid] = [dir_id, name, base + name, depth + 1]
        self.own[dir_id] = (own_a, own_l, nf, mt, flags, nplain)
        if flags & db.F_DENIED:
            self.denied += 1
        if files:
            self.con.executemany("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)", files)
        self.nfiles += nf
        self.bytes += own_a + sum(v[0] for v in over_l.values())
        for k, v in ext_l.items():
            x = self.ext.get(k)
            if x is None:
                self.ext[k] = v
            else:
                x[0] += v[0]; x[1] += v[1]; x[2] += v[2]
        for k, v in over_l.items():
            x = self.over.setdefault(k, [0, 0])
            x[0] += v[0]; x[1] += v[1]

    def run(self):
        self.q.put((1, self.root, 0))
        threads = [threading.Thread(target=self.worker, daemon=True) for _ in range(self.nworkers)]
        for t in threads:
            t.start()

        def joiner():
            self.q.join()
            self.out.put(_DONE)
        threading.Thread(target=joiner, daemon=True).start()

        last_status = 0.0
        last_commit = time.monotonic()
        while True:
            try:
                msg = self.out.get(timeout=0.5)
            except queue.Empty:
                msg = None
            if msg is _DONE:
                break
            if msg is not None:
                self.consume(msg)
            now = time.monotonic()
            if now - last_status >= 1.0:
                last_status = now
                self.check_control()
                self.write_status("paused" if self.guard.paused else "running")
            if now - last_commit >= 5.0:
                self.con.commit()
                last_commit = now
        for _ in threads:
            self.q.put(None)
        if self.stop.is_set():
            raise Cancelled()
        self.write_status("finishing")
        return self.finish()

    def finish(self):
        """Roll sizes up the tree, assign categories, write the folder table."""
        dirs, own, rules = self.dirs, self.own, self.rules
        order = sorted(dirs, key=lambda i: dirs[i][3])          # parents before children
        tot_a, tot_l, tot_n, tot_d, tot_m = {}, {}, {}, {}, {}
        for i in order:
            a, l, n, m, _, _ = own.get(i, (0, 0, 0, 0.0, 0, 0))
            tot_a[i], tot_l[i], tot_n[i], tot_d[i], tot_m[i] = a, l, n, 0, m
        # own_a excludes files with their own rule; add them back to the folder totals
        cur = self.con.execute("SELECT dir, SUM(alloc) FROM files WHERE rule IS NOT NULL GROUP BY dir")
        for d, s in cur:
            if d in tot_a:
                tot_a[d] += s
        for i in reversed(order):
            p = dirs[i][0]
            if p in tot_a:
                tot_a[p] += tot_a[i]; tot_l[p] += tot_l[i]; tot_n[p] += tot_n[i]
                tot_d[p] += tot_d[i] + 1
                if tot_m[i] > tot_m[p]:
                    tot_m[p] = tot_m[i]

        eff_rule, anchor_of = {}, {}
        anchor_alloc = {}
        cat_stats = {}
        rows = []
        for i in order:
            parent, name, path, depth = dirs[i]
            r = rules.match_dir(path)
            if r is not None or i == 1:
                eff_rule[i] = -1 if r is None else r
                anchor_of[i] = i
                is_anchor = 1
            else:
                eff_rule[i] = eff_rule[parent]
                anchor_of[i] = anchor_of[parent]
                is_anchor = 0
            own_a, _, own_n, _, flags, nplain = own.get(i, (0, 0, 0, 0.0, 0, 0))
            anchor_alloc[anchor_of[i]] = anchor_alloc.get(anchor_of[i], 0) + own_a
            c = rules.category(eff_rule[i])
            cs = cat_stats.setdefault(c, [0, 0])
            cs[0] += own_a
            cs[1] += nplain
            rows.append([i, parent, name, path, depth, tot_a[i], tot_l[i], own_a, tot_n[i], tot_d[i],
                         own_n, tot_m[i], eff_rule[i], is_anchor, 0, flags])
        for row in rows:
            row[14] = anchor_alloc.get(row[0], 0) if row[13] else 0
        for ridx, (a, n) in self.over.items():
            c = rules.category(ridx)
            cs = cat_stats.setdefault(c, [0, 0])
            cs[0] += a
            cs[1] += n
        self.con.executemany("INSERT INTO dirs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.con.executemany("INSERT INTO ext_stats VALUES (?,?,?,?)",
                             [(k, v[0], v[1], v[2]) for k, v in self.ext.items()])
        self.con.executemany("INSERT INTO cat_stats VALUES (?,?,?)",
                             [(k, v[0], v[1]) for k, v in cat_stats.items()])
        self.con.executemany("INSERT INTO meta VALUES (?,?)", [
            ("rules", rules.to_json()), ("volume", self.volume), ("cluster", str(self.cluster)),
            ("hardlinks", "2"),
        ])
        self.con.commit()
        self.write_status("finishing", {"step": tr("Criando índices")})
        self.con.executescript(db.SNAP_INDEXES)
        self.con.commit()
        self.con.close()
        return {"scanned": tot_a[1], "nfiles": tot_n[1], "ndirs": len(dirs), "denied": self.denied}


def measure_hidden(volume, snap_db, used, res):
    """Explain used-minus-scanned with probes (shadow copies, reserve, NTFS metadata...)."""
    con = sqlite3.connect(snap_db)
    row = con.execute("SELECT alloc, flags FROM dirs WHERE parent=1 AND lower(name)=?",
                      ("system volume information",)).fetchone()
    con.close()
    svi = row[0] if row and not row[1] & db.F_DENIED else 0
    try:
        raw = probes.collect(volume)
    except Exception as exc:          # probes are best effort; never lose a scan over them
        raw = {"volume": volume, "admin": winapi.is_admin(), "ps": {}, "error": str(exc)}
    return probes.reconcile(raw, used, res["scanned"], res["nfiles"], res["ndirs"], svi, res["denied"],
                            volume == winapi.system_drive())


def _hidden_summary(h):
    """Compact per-snapshot numbers kept in index.db for the future timeline."""
    out = {it["key"]: it["bytes"] for it in h["items"]}
    if h.get("pagefile"):
        out["pagefile_alloc"] = h["pagefile"]["allocated"]
    return out


def replace_retry(src, dst, tries=20):
    """os.replace that tolerates a reader holding dst open for a moment (Windows sharing)."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    return False


def write_status(st):
    """Progress file read by the UI. Best effort: never let it break a scan."""
    p = data_dir() / "scan_status.json"
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        if not replace_retry(tmp, p):
            tmp.unlink(missing_ok=True)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="DiskLens scanner")
    ap.add_argument("--volume", default=winapi.system_drive())
    ap.add_argument("--force", action="store_true", help="não pausar quando o PC estiver ocupado")
    args = ap.parse_args(argv)

    lock = try_lock()
    if lock is None:
        print(tr("Outro scan já está em andamento."), file=sys.stderr)
        return 2
    winapi.set_background_mode()
    cfg = load_config()
    try:
        (data_dir() / "scan_control.json").unlink()
    except OSError:
        pass

    volume = args.volume.upper().rstrip("\\")
    admin = winapi.is_admin()
    backup = winapi.enable_privilege("SeBackupPrivilege") if admin else False
    snap_id = db.create_snapshot(volume, admin)
    final = db.snap_path(snap_id)
    tmp = final.with_suffix(".partial")
    if tmp.exists():
        tmp.unlink()
    guard = LoadGuard(cpu_limit=cfg["cpu_limit"], mem_limit=cfg["mem_limit"], enabled=not args.force)
    guard.start()
    scanner = Scanner(volume, tmp, guard, Rules(), workers=cfg["workers"])
    try:
        res = scanner.run()
        du = shutil.disk_usage(volume + "\\")
        scanner.write_status("finishing", {"step": tr("Medindo áreas ocultas do sistema")})
        hidden = measure_hidden(volume, tmp, du.used, res)
        hidden["backup_privilege"] = backup
        con = sqlite3.connect(tmp)
        con.execute("INSERT INTO meta VALUES ('hidden', ?)", (json.dumps(hidden, ensure_ascii=False),))
        con.commit()
        con.close()
        os.replace(tmp, final)
        db.update_snapshot(snap_id, status="done", finished=time.time(), total=du.total, used=du.used,
                           free=du.free, scanned=res["scanned"], nfiles=res["nfiles"],
                           ndirs=res["ndirs"], denied=res["denied"],
                           seconds_paused=guard.paused_seconds,
                           hidden=json.dumps(_hidden_summary(hidden), ensure_ascii=False))
        db.snapshot_cats(db.get_snapshot(snap_id))
        pf = (hidden.get("pagefile") or {}).get("allocated")
        db.add_sample(time.time(), volume, du.total, du.used, du.free, pf)
        db.compact_old_snapshots(volume, snap_id)
        scanner.write_status("done", {"snap_id": snap_id})
        return 0
    except Cancelled:
        scanner.con.close()
        db.update_snapshot(snap_id, status="cancelled", finished=time.time())
        scanner.write_status("cancelled")
        tmp.unlink(missing_ok=True)
        return 1
    except Exception as exc:
        traceback.print_exc()
        db.update_snapshot(snap_id, status="error", finished=time.time(), note=str(exc)[:500])
        write_status({"state": "error", "volume": volume, "error": str(exc), "updated": time.time()})
        try:
            scanner.con.close()
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return 1
    finally:
        guard.shutdown()


if __name__ == "__main__":
    sys.exit(main())
