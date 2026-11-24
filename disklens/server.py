"""Local HTTP API (127.0.0.1 only) consumed by the web UI.

Every /api call must carry the random per-run token in the ``X-DiskLens-Token`` header, so
other web pages open in a browser cannot drive the app.
"""
import json
import mimetypes
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, i18n, probes, winapi
from .i18n import tr
from .config import APP_DIR, PKG_DIR, data_dir, load as load_config, save as save_config
from .rules import Rules, family
from .scanner import is_scan_running

WEB_DIR = PKG_DIR / "web"
TOKEN = secrets.token_urlsafe(24)
_rules_cache = {}
_proc_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def rules_for(con, snap_id):
    r = _rules_cache.get(snap_id)
    if r is None:
        row = con.execute("SELECT value FROM meta WHERE key='rules'").fetchone()
        r = Rules(json.loads(row[0])) if row else Rules()
        _rules_cache[snap_id] = r
    return r


def snap_con(q):
    try:
        snap_id = int(q.get("snap", ["0"])[0])
    except ValueError:
        raise ApiError(400, "snap inválido")
    s = db.get_snapshot(snap_id)
    if not s or s["status"] != "done" or not db.snap_path(snap_id).exists():
        raise ApiError(404, "snapshot não encontrado")
    con = db.open_snap(snap_id)
    return snap_id, s, con, rules_for(con, snap_id)


def qint(q, key, default=None):
    v = q.get(key, [None])[0]
    if v in (None, ""):
        return default
    try:
        return int(v)
    except ValueError:
        raise ApiError(400, f"{key} inválido")


def qstr(q, key, default=""):
    return q.get(key, [default])[0] or default


# ------------------------------------------------------------------------------ handlers
def api_state(q, body):
    status = None
    p = data_dir() / "scan_status.json"
    if p.exists():
        for _ in range(3):           # the scanner may be swapping the file this instant
            try:
                status = json.loads(p.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                time.sleep(0.05)
    return {"admin": winapi.is_admin(), "volumes": winapi.fixed_volumes(),
            "system_drive": winapi.system_drive(), "snapshots": db.list_snapshots(),
            "running": is_scan_running(), "scan": status, "data_dir": str(data_dir())}


def api_scan(q, body):
    volume = (body.get("volume") or winapi.system_drive()).upper()[:2]
    if volume not in {v["volume"] for v in winapi.fixed_volumes()}:
        raise ApiError(400, tr("volume inválido"))
    with _proc_lock:
        if is_scan_running():
            raise ApiError(409, tr("Já existe um scan em andamento"))
        args = [sys.executable, "-m", "disklens.scanner", "--volume", volume]
        if body.get("force"):
            args.append("--force")
        log = open(data_dir() / "scanner.log", "ab")
        subprocess.Popen(args, cwd=str(APP_DIR), stdout=log, stderr=log,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.8)
    return {"ok": True}


def api_control(q, body):
    action = body.get("action")
    if action not in ("cancel", "force"):
        raise ApiError(400, tr("ação inválida"))
    (data_dir() / "scan_control.json").write_text(json.dumps({"action": action}), encoding="utf-8")
    return {"ok": True}


def api_elevate(q, body):
    from .tasks import _pythonw
    ok = winapi.relaunch_as_admin(_pythonw(), f'"{APP_DIR / "launch.pyw"}"', str(APP_DIR))
    if ok:
        threading.Timer(1.5, lambda: __import__("os")._exit(0)).start()
    return {"ok": ok}


def _cat_meta(rules):
    return {k: dict(v) for k, v in rules.categories.items()}


def api_overview(q, body):
    snap_id, s, con, rules = snap_con(q)
    with con:
        cats = {r["cat"]: {"alloc": r["alloc"], "n": r["n"]}
                for r in con.execute("SELECT * FROM cat_stats")}
        tops = {}
        for r in con.execute("SELECT id, path, anchor_alloc, rule FROM dirs WHERE anchor=1 "
                             "AND anchor_alloc>0 ORDER BY anchor_alloc DESC LIMIT 600"):
            c = rules.category(r["rule"])
            lst = tops.setdefault(c, [])
            if len(lst) < 8:
                info = rules.info(r["rule"])
                lst.append({"kind": "dir", "id": r["id"], "path": r["path"], "alloc": r["anchor_alloc"],
                            "label": info["label"] if info else tr("Outras pastas")})
        for r in con.execute("SELECT f.rowid AS fid, f.name, f.alloc, f.rule, d.path, d.id AS did "
                             "FROM files f JOIN dirs d ON d.id=f.dir WHERE f.rule IS NOT NULL "
                             "ORDER BY f.alloc DESC LIMIT 40"):
            c = rules.category(r["rule"])
            lst = tops.setdefault(c, [])
            lst.append({"kind": "file", "id": r["did"], "fid": r["fid"],
                        "path": r["path"].rstrip("\\") + "\\" + r["name"], "alloc": r["alloc"],
                        "label": rules.info(r["rule"])["label"]})
        for c in tops:
            tops[c].sort(key=lambda x: -x["alloc"])
            tops[c] = tops[c][:8]
        denied = [dict(r) for r in con.execute(
            "SELECT id, path FROM dirs WHERE flags & 1 ORDER BY depth LIMIT 15")]
        row = con.execute("SELECT value FROM meta WHERE key='hidden'").fetchone()
        hidden = json.loads(row[0]) if row else None
        # what can be freed safely (caches, temp, recycle bin): rules marked "seguro"
        reclaim, total = [], 0
        for r in con.execute("SELECT id, path, anchor_alloc, rule FROM dirs WHERE anchor=1 "
                             "AND anchor_alloc > 1048576 ORDER BY anchor_alloc DESC"):
            info = rules.info(r["rule"])
            if info and info["safety"] == "seguro":
                total += r["anchor_alloc"]
                if len(reclaim) < 10:
                    reclaim.append({"id": r["id"], "path": r["path"], "alloc": r["anchor_alloc"],
                                    "label": info["label"]})
    return {"snapshot": s, "categories": _cat_meta(rules), "cat_stats": cats, "tops": tops,
            "denied": denied, "safety": rules.safety, "hidden": probes.localize(hidden),
            "reclaim": {"total": total, "items": reclaim}}


def _node(r, rules):
    return {"id": r["id"], "name": r["name"], "value": r["alloc"], "cat": rules.category(r["rule"]),
            "mtime": r["mtime"], "nfiles": r["nfiles"], "ndirs": r["ndirs"], "kind": "dir",
            "denied": bool(r["flags"] & db.F_DENIED)}


def _children(con, rules, dir_row, levels, limit, min_frac, root_value):
    out = []
    shown = 0
    rows = con.execute("SELECT * FROM dirs WHERE parent=? AND alloc>0 ORDER BY alloc DESC LIMIT ?",
                       (dir_row["id"], limit)).fetchall()
    for r in rows:
        if r["alloc"] < root_value * min_frac:
            break
        n = _node(r, rules)
        shown += r["alloc"]
        if levels > 1 and r["ndirs"]:
            n["children"] = _children(con, rules, r, levels - 1, limit, min_frac, root_value)
        out.append(n)
    files_value = dir_row["alloc"] - (sum_child := _child_sum(con, dir_row["id"]))
    rest = sum_child - shown
    if files_value > 0:
        # color loose files by the category of the biggest ones (e.g. pagefile.sys at C:\)
        top = con.execute("SELECT name, alloc, rule FROM files WHERE dir=? ORDER BY alloc DESC LIMIT 3",
                          (dir_row["id"],)).fetchall()
        rule = top[0]["rule"] if top and top[0]["rule"] is not None else dir_row["rule"]
        out.append({"id": dir_row["id"], "name": tr("(arquivos soltos nesta pasta)"), "value": files_value,
                    "kind": "files", "cat": rules.category(rule), "mtime": dir_row["mtime"],
                    "top": [{"name": t["name"], "alloc": t["alloc"]} for t in top]})
    if rest > 0:
        out.append({"id": dir_row["id"], "name": tr("(pastas menores)"), "value": rest, "kind": "rest",
                    "cat": rules.category(dir_row["rule"]), "mtime": dir_row["mtime"]})
    return out


def _child_sum(con, dir_id):
    return con.execute("SELECT COALESCE(SUM(alloc),0) FROM dirs WHERE parent=?", (dir_id,)).fetchone()[0]


def api_tree(q, body):
    snap_id, s, con, rules = snap_con(q)
    with con:
        dir_id = qint(q, "dir", 1)
        levels = max(1, min(4, qint(q, "levels", 2)))
        r = con.execute("SELECT * FROM dirs WHERE id=?", (dir_id,)).fetchone()
        if not r:
            raise ApiError(404, tr("pasta não encontrada"))
        node = _node(r, rules)
        node["children"] = _children(con, rules, r, levels, 60, 0.002, max(r["alloc"], 1))
    return {"tree": node, "categories": _cat_meta(rules)}


def _crumbs(con, dir_id):
    out = []
    while dir_id:
        r = con.execute("SELECT id, parent, name, anchor, rule FROM dirs WHERE id=?", (dir_id,)).fetchone()
        if not r:
            break
        out.append(dict(r))
        dir_id = r["parent"]
    return list(reversed(out))


def api_dir(q, body):
    snap_id, s, con, rules = snap_con(q)
    with con:
        dir_id = qint(q, "dir", 1)
        r = con.execute("SELECT * FROM dirs WHERE id=?", (dir_id,)).fetchone()
        if not r:
            raise ApiError(404, tr("pasta não encontrada"))
        crumbs = _crumbs(con, dir_id)
        anchor_path = None
        for c in reversed(crumbs):
            if c["anchor"]:
                anchor_path = con.execute("SELECT path FROM dirs WHERE id=?", (c["id"],)).fetchone()[0]
                break
        d = dict(r)
        d["cat"] = rules.category(r["rule"])
        subdirs = [_node(x, rules) | {"own": x["own_alloc"]} for x in con.execute(
            "SELECT * FROM dirs WHERE parent=? ORDER BY alloc DESC LIMIT 300", (dir_id,))]
        files = []
        for f in con.execute("SELECT rowid AS fid, * FROM files WHERE dir=? ORDER BY alloc DESC LIMIT 300",
                             (dir_id,)):
            ridx = f["rule"] if f["rule"] is not None else r["rule"]
            files.append({"fid": f["fid"], "name": f["name"], "alloc": f["alloc"], "logical": f["logical"],
                          "mtime": f["mtime"], "flags": f["flags"], "cat": rules.category(ridx),
                          "rule": rules.info(f["rule"]) if f["rule"] is not None else None})
    return {"dir": d, "rule": rules.info(r["rule"]), "anchor_path": anchor_path,
            "crumbs": [{"id": c["id"], "name": c["name"]} for c in crumbs],
            "subdirs": subdirs, "files": files, "files_listed": not s["compacted"]}


def api_files(q, body):
    snap_id, s, con, rules = snap_con(q)
    where, args = ["f.alloc >= ?"], [qint(q, "min", 0)]
    name = qstr(q, "q").strip()
    if name:
        where.append("f.name LIKE ? ESCAPE '\\'")
        args.append("%" + name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    ext = qstr(q, "ext").strip().lower().lstrip(".")
    if ext:
        where.append("lower(f.name) LIKE ? ESCAPE '\\'")
        args.append("%." + ext.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
    cat = qstr(q, "cat")
    if cat:
        idxs = [i for i in range(len(rules.rules)) if rules.category(i) == cat]
        if cat == "outros":
            idxs.append(-1)
        where.append(f"COALESCE(f.rule, d.rule) IN ({','.join('?' * len(idxs)) or 'NULL'})")
        args.extend(idxs)
    under = qint(q, "under")
    if under and under != 1:
        p = con.execute("SELECT path FROM dirs WHERE id=?", (under,)).fetchone()
        if p:
            prefix = p[0].rstrip("\\") + "\\"
            where.append("(d.id=? OR substr(d.path,1,?)=?)")
            args.extend([under, len(prefix), prefix])
    limit = max(1, min(2000, qint(q, "limit", 500)))
    sql = ("SELECT f.rowid AS fid, f.name, f.alloc, f.logical, f.mtime, f.flags, f.rule AS frule, "
           "d.id AS did, d.path, d.rule AS drule FROM files f JOIN dirs d ON d.id=f.dir "
           f"WHERE {' AND '.join(where)} ORDER BY f.alloc DESC LIMIT ?")
    with con:
        rows = con.execute(sql, (*args, limit)).fetchall()
    out = []
    for r in rows:
        ridx = r["frule"] if r["frule"] is not None else r["drule"]
        out.append({"fid": r["fid"], "name": r["name"], "alloc": r["alloc"], "logical": r["logical"],
                    "mtime": r["mtime"], "flags": r["flags"], "dir": r["did"], "path": r["path"],
                    "cat": rules.category(ridx)})
    return {"files": out, "categories": _cat_meta(rules), "files_listed": not s["compacted"]}


def api_types(q, body):
    snap_id, s, con, rules = snap_con(q)
    with con:
        rows = [dict(r) for r in con.execute("SELECT * FROM ext_stats ORDER BY alloc DESC")]
    fams = {}
    for r in rows:
        r["family"] = family(r["ext"])
        f = fams.setdefault(r["family"], {"family": r["family"], "alloc": 0, "n": 0})
        f["alloc"] += r["alloc"]
        f["n"] += r["n"]
    return {"families": sorted(fams.values(), key=lambda x: -x["alloc"]), "exts": rows[:300]}


def api_open(q, body):
    """Reveal a folder/file from the snapshot in Explorer (path resolved server-side)."""
    snap_id, s, con, rules = snap_con({"snap": [str(body.get("snap", 0))]})
    with con:
        r = con.execute("SELECT path FROM dirs WHERE id=?", (int(body.get("dir", 0)),)).fetchone()
        if not r:
            raise ApiError(404, tr("não encontrado"))
        path = r[0]
        fid = body.get("fid")
        if fid is not None:
            f = con.execute("SELECT name FROM files WHERE rowid=? AND dir=?",
                            (int(fid), int(body["dir"]))).fetchone()
            if f:
                winapi.reveal_in_explorer(path.rstrip("\\") + "\\" + f[0], select=True)
                return {"ok": True}
    winapi.reveal_in_explorer(path, select=False)
    return {"ok": True}


def _agent_status():
    from . import agent, tasks
    cfg = load_config()
    st = agent.load_state()
    return {"installed": tasks.is_installed(), "state": st, "admin": winapi.is_admin(),
            "settings": {k: cfg.get(k) for k in ("cpu_limit", "mem_limit", "snapshot_hours", "volumes")},
            "volumes_default": [winapi.system_drive()], "tray": _tray_status(cfg)}


def _tray_status(cfg=None):
    from . import instance
    cfg = cfg or load_config()
    return {"enabled": bool(cfg.get("tray")), "running": instance.running(instance.TRAY_MUTEX),
            "autostart": instance.autostart_enabled()}


def api_tray(q, body):
    """Turn the notification-area icon on/off (and "start with Windows")."""
    from . import instance
    cfg = load_config()
    if "enabled" in body:
        on = bool(body["enabled"])
        cfg["tray"] = on
        save_config(cfg)
        if on:
            instance.set_autostart(bool(body.get("autostart", True)))
            if not instance.running(instance.TRAY_MUTEX):
                instance.start_tray(open_panel=False)
                for _ in range(30):
                    if instance.running(instance.TRAY_MUTEX):
                        break
                    time.sleep(0.1)
        else:
            instance.set_autostart(False)
            h = instance.tray_window()
            if h:
                instance.u32.PostMessageW(h, instance.MSG_QUIT, 0, 0)
                for _ in range(30):
                    if not instance.running(instance.TRAY_MUTEX):
                        break
                    time.sleep(0.1)
    elif "autostart" in body:
        instance.set_autostart(bool(body["autostart"]) and bool(cfg.get("tray")))
    return _tray_status(cfg)


def api_timeline(q, body):
    volume = qstr(q, "volume", winapi.system_drive()).upper()[:2]
    days = max(1, min(3650, qint(q, "days", 365)))
    since = time.time() - days * 86400
    snaps = []
    for s in db.list_snapshots():
        if s["volume"] != volume or s["status"] != "done" or (s["finished"] or 0) < since:
            continue
        snaps.append({"id": s["id"], "finished": s["finished"], "used": s["used"], "free": s["free"],
                      "total": s["total"], "scanned": s["scanned"], "admin": s["admin"],
                      "cats": db.snapshot_cats(s), "hidden": json.loads(s["hidden"]) if s.get("hidden") else None})
    snaps.reverse()
    with db.index_conn() as con:
        samples = [list(r) for r in con.execute(
            "SELECT ts, used, free, pagefile, hiberfil, shadow FROM samples WHERE volume=? AND ts>=? ORDER BY ts",
            (volume, since))]
        events = [dict(r) for r in con.execute(
            "SELECT ts, kind, title, detail FROM events WHERE ts>=? AND (volume=? OR volume IS NULL) ORDER BY ts",
            (since, volume))]
    if len(samples) > 4000:                      # keep the chart light
        step = len(samples) // 4000 + 1
        samples = samples[::step] + [samples[-1]]
    from .rules import Rules
    return {"volume": volume, "snapshots": snaps, "samples": samples, "events": events,
            "categories": Rules().categories, "agent": _agent_status()}


def api_activity(q, body):
    from . import usn
    volume = qstr(q, "volume", winapi.system_drive()).upper()[:2]
    t1 = float(q.get("to", [time.time()])[0] or time.time())
    t0 = float(q.get("from", [t1 - 86400])[0] or t1 - 86400)
    r = usn.activity(volume, t0, t1, limit=max(5, min(200, qint(q, "limit", 40))))
    r["from"], r["to"] = t0, t1
    st = __import__("disklens.agent", fromlist=["load_state"]).load_state()
    r["last_error"] = (st.get("usn", {}).get(volume) or {}).get("error")
    return r


# ------------------------------------------------------------------ cleanup (Liberar espaço)
def api_cleanup(q, body):
    from . import ai, candidates, cleaner, proposals
    volume = qstr(q, "volume", winapi.system_drive()).upper()[:2]
    d = candidates.build(volume)
    last = ai.last_analysis()
    d["ai"] = last if last and last.get("volume") == volume else None
    d["proposals"] = [p for p in proposals.load() if p["status"] == "pending"]
    d["history"] = cleaner.history(15)
    d["tools_meta"] = cleaner.tools()
    return d


def api_cleanup_run(q, body):
    from . import proposals
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise ApiError(400, tr("nada selecionado"))
    try:
        job = proposals.start_job(items, body.get("proposal"))
    except RuntimeError as e:
        raise ApiError(409, str(e))
    return {"job": job["id"]}


def api_cleanup_job(q, body):
    from . import proposals
    j = proposals.get_job(qstr(q, "id"))
    if not j:
        raise ApiError(404, tr("tarefa não encontrada"))
    return j


def api_cleanup_check(q, body):
    from . import cleaner
    path = qstr(q, "path")
    return {"path": path, "blocked": cleaner.protection_reason(path)}


def api_open_path(q, body):
    import os
    path = os.path.normpath(str(body.get("path") or ""))
    if not path or not os.path.lexists(path):
        raise ApiError(404, tr("não existe mais"))
    winapi.reveal_in_explorer(path, select=True)
    return {"ok": True}


def api_recycle(q, body):
    from . import cleaner
    return cleaner.recycle_bin_info()


def api_proposal_dismiss(q, body):
    from . import proposals
    proposals.set_status(str(body.get("id")), "dismissed")
    return {"ok": True}


def api_ai_settings(q, body):
    from . import ai
    if body:
        try:
            return ai.save_settings(provider=body.get("provider"), model=body.get("model"),
                                    base_url=body.get("base_url"), api_key=body.get("api_key") or None,
                                    consented=body.get("consented"), clear_key=bool(body.get("clear_key")))
        except ai.AIError as e:
            raise ApiError(400, str(e))
    return ai.settings()


def _ai_call(fn, *args):
    from . import ai
    try:
        return fn(*args)
    except ai.AIError as e:
        raise ApiError(400, str(e))


def api_ai_models(q, body):
    from . import ai
    return {"models": _ai_call(ai.list_models)}


def api_ai_analyze(q, body):
    from . import ai
    return _ai_call(ai.analyze, (body.get("volume") or winapi.system_drive()).upper()[:2])


def api_ai_explain(q, body):
    from . import ai
    path = str(body.get("path") or "")
    return _ai_call(ai.explain, path[:2].upper(), path)


def api_mcp_config(q, body):
    from .tasks import _pythonw
    exe = _pythonw()
    py = exe[:-len("pythonw.exe")] + "python.exe" if exe.lower().endswith("pythonw.exe") else exe
    script = str(APP_DIR / "disklens_mcp.py")
    snippet = {"mcpServers": {"disklens": {"command": py, "args": [script]}}}
    return {"command": py, "args": [script], "json": json.dumps(snippet, indent=2, ensure_ascii=False)}


def api_diff(q, body):
    from . import diff
    try:
        return diff.compare(qint(q, "a"), qint(q, "b"))
    except ValueError as e:
        raise ApiError(400, str(e))


def api_agent(q, body):
    from . import tasks
    action = body.get("action")
    if action == "install":
        ok = tasks.install()
        if ok:
            tasks.run_agent_now()
        return {"ok": ok, "elevated": not winapi.is_admin()}
    if action == "uninstall":
        return {"ok": tasks.uninstall(), "elevated": not winapi.is_admin()}
    if action == "run":
        how = tasks.run_agent_now(snapshot=bool(body.get("snapshot")))
        return {"ok": True, "via": how}
    raise ApiError(400, "ação inválida")


def api_agent_status(q, body):
    return _agent_status()


def api_i18n(q, body):
    """Language, the list of languages and the translation table for the UI."""
    code = i18n.lang()
    return {"lang": code, "locale": i18n.LOCALES[code], "langs": i18n.LANGS, "table": i18n.table(code)}


def api_support(q, body):
    from . import support
    return dict(support.info(), qr_svg=support.qr_svg())


def api_open_link(q, body):
    from . import support
    if not support.open_link(str(body.get("key") or "")):
        raise ApiError(400, tr("link desconhecido"))
    return {"ok": True}


def api_settings(q, body):
    cfg = load_config()
    if "lang" in body:
        cfg["lang"] = body["lang"] if body["lang"] in i18n.LANGS else None
        save_config(cfg)
        return api_i18n(q, {})
    if "cpu_limit" in body:
        cfg["cpu_limit"] = max(20, min(95, int(body["cpu_limit"])))
    if "mem_limit" in body:
        cfg["mem_limit"] = max(50, min(98, int(body["mem_limit"])))
    if "snapshot_hours" in body:
        cfg["snapshot_hours"] = max(1, min(24 * 30, int(body["snapshot_hours"])))
    if "volumes" in body:
        valid = {v["volume"] for v in winapi.fixed_volumes()}
        vols = [v.upper()[:2] for v in body["volumes"] or [] if v.upper()[:2] in valid]
        cfg["volumes"] = vols or None
    save_config(cfg)
    return _agent_status()


ROUTES = {
    ("GET", "/api/timeline"): api_timeline,
    ("GET", "/api/diff"): api_diff,
    ("GET", "/api/activity"): api_activity,
    ("GET", "/api/cleanup"): api_cleanup,
    ("POST", "/api/cleanup/run"): api_cleanup_run,
    ("GET", "/api/cleanup/job"): api_cleanup_job,
    ("GET", "/api/cleanup/check"): api_cleanup_check,
    ("GET", "/api/recycle"): api_recycle,
    ("POST", "/api/open_path"): api_open_path,
    ("POST", "/api/proposals/dismiss"): api_proposal_dismiss,
    ("GET", "/api/ai/settings"): api_ai_settings,
    ("POST", "/api/ai/settings"): api_ai_settings,
    ("GET", "/api/ai/models"): api_ai_models,
    ("POST", "/api/ai/analyze"): api_ai_analyze,
    ("POST", "/api/ai/explain"): api_ai_explain,
    ("GET", "/api/mcp/config"): api_mcp_config,
    ("POST", "/api/agent"): api_agent,
    ("GET", "/api/agent"): api_agent_status,
    ("POST", "/api/settings"): api_settings,
    ("GET", "/api/i18n"): api_i18n,
    ("GET", "/api/support"): api_support,
    ("POST", "/api/open_link"): api_open_link,

    ("POST", "/api/tray"): api_tray,
    ("GET", "/api/state"): api_state,
    ("POST", "/api/scan"): api_scan,
    ("POST", "/api/scan/control"): api_control,
    ("POST", "/api/elevate"): api_elevate,
    ("GET", "/api/overview"): api_overview,
    ("GET", "/api/tree"): api_tree,
    ("GET", "/api/dir"): api_dir,
    ("GET", "/api/files"): api_files,
    ("GET", "/api/types"): api_types,
    ("POST", "/api/open"): api_open,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DiskLens"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            if self.headers.get("X-DiskLens-Token") != TOKEN:
                return self._send(403, {"error": tr("token inválido")})
            fn = ROUTES.get((method, u.path))
            if not fn:
                return self._send(404, {"error": tr("rota desconhecida")})
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n))
                    except ValueError:
                        return self._send(400, {"error": tr("json inválido")})
            try:
                return self._send(200, fn(parse_qs(u.query), body))
            except ApiError as e:
                return self._send(e.status, {"error": str(e)})
            except Exception as e:  # keep the UI alive and show the error
                import traceback
                traceback.print_exc()
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        if method != "GET":
            return self._send(405, {"error": tr("método não permitido")})
        rel = u.path.lstrip("/") or "index.html"
        f = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in f.parents or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/?t={TOKEN}"
