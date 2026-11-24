"""MCP server (Model Context Protocol, stdio transport) so any MCP-capable AI client can look at
the disk data and *propose* a cleanup. There is deliberately no tool that deletes anything:
proposals appear in DiskLens' "Liberar espaço" tab and run only after the user confirms.

Client configuration (e.g. Claude Desktop, Cursor, VS Code, Gemini CLI):
  command: <python.exe>   args: ["<DiskLens folder>\\disklens_mcp.py"]
"""
import json
import os
import sqlite3
import sys
import time
import traceback

from . import __version__, candidates, cleaner, db, probes, proposals, winapi

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]


def _vol(args):
    return (args.get("volume") or winapi.system_drive()).upper()[:2]


def _mb(b):
    return round((b or 0) / 1e6, 1)


def t_overview(args):
    vol = _vol(args)
    s = candidates.latest_snapshot(vol)
    if not s:
        return {"error": f"There is no snapshot of drive {vol} yet. Ask the user to scan it in DiskLens."}
    con, rules = candidates._open(s)
    try:
        cats = {rules.categories.get(c, {}).get("label", c): _mb(a) for c, a in
                con.execute("SELECT cat, alloc FROM cat_stats ORDER BY alloc DESC")}
        row = con.execute("SELECT value FROM meta WHERE key='hidden'").fetchone()
        hidden = [{"item": i["label"], "mb": _mb(i["bytes"]), "measured": i["exact"]}
                  for i in (probes.localize(json.loads(row[0]))["items"] if row else [])]
    finally:
        con.close()
    return {"drive": vol, "snapshot": time.strftime("%Y-%m-%d %H:%M", time.localtime(s["finished"])),
            "total_gb": round(s["total"] / 1e9, 1), "used_gb": round(s["used"] / 1e9, 1),
            "free_gb": round(s["free"] / 1e9, 1), "mb_by_category": cats, "hidden_space": hidden,
            "recycle_bin_mb": _mb(cleaner.recycle_bin_info()["bytes"])}


def t_candidates(args):
    d = candidates.build(_vol(args))
    if not d.get("available"):
        return {"error": "There is no snapshot yet. Ask the user to scan the drive in DiskLens."}
    sec = args.get("section")
    out = {
        "safe": [{"path": i["path"], "mb": _mb(i["size"]), "what_it_is": i["label"], "action": "esvaziar"}
                 for i in d["safe"]],
        "tools": [{"id": i["tool"], "label": i["label"], "estimated_mb": _mb(i["size"]), "description": i["desc"],
                   "needs_admin": i["needs_admin"], "action": "ferramenta"} for i in d["tools"]],
        "review": [{"path": i["path"], "mb": _mb(i["size"]), "group": i["group"], "reason": i["reason"],
                    "action": "lixeira"} for i in d["review"]],
        "programs": [{"id": p["id"], "name": p["name"], "publisher": p["publisher"], "drive": p["drive"],
                      "mb": _mb(p["size"]), "dependency": p["dependency"], "action": "desinstalar",
                      "installed": time.strftime("%Y-%m-%d", time.localtime(p["installed"])) if p["installed"] else None}
                     for p in d["programs"][:100]],
        "unknown": [{"path": u["path"], "mb": _mb(u["size"]), "subfolders": [k["name"] for k in u["children"]]}
                    for u in d["unknown"]],
    }
    return {sec: out[sec]} if sec in out else out


def t_folder(args):
    path = os.path.normpath(str(args.get("path") or ""))
    vol = path[:2].upper()
    s = candidates.latest_snapshot(vol)
    if not s:
        return {"error": "no snapshot of this drive"}
    con, rules = candidates._open(s)
    try:
        r = con.execute("SELECT * FROM dirs WHERE lower(path)=lower(?)", (path,)).fetchone()
        if not r:
            return {"error": "folder not found in the latest snapshot"}
        info = rules.info(r["rule"])
        blocked = cleaner.protection_reason(r["path"])
        return {
            "path": r["path"], "mb": _mb(r["alloc"]), "files": r["nfiles"], "subfolders_total": r["ndirs"],
            "last_change": time.strftime("%Y-%m-%d", time.localtime(r["mtime"] or 0)),
            "rule": {"what_it_is": info["label"], "explanation": info["desc"], "safety": info["safety"]} if info else None,
            "can_go_to_recycle_bin": "no: " + blocked if blocked else "yes",
            "subfolders": [{"name": k["name"], "mb": _mb(k["alloc"])} for k in con.execute(
                "SELECT name, alloc FROM dirs WHERE parent=? ORDER BY alloc DESC LIMIT 30", (r["id"],))],
            "largest_files": [{"name": f["name"], "mb": _mb(f["alloc"]),
                               "modified": time.strftime("%Y-%m-%d", time.localtime(f["mtime"]))}
                                 for f in con.execute("SELECT name, alloc, mtime FROM files WHERE dir=? "
                                                      "ORDER BY alloc DESC LIMIT 30", (r["id"],))],
        }
    finally:
        con.close()


def t_search(args):
    vol = _vol(args)
    s = candidates.latest_snapshot(vol)
    if not s:
        return {"error": "no snapshot"}
    con, _ = candidates._open(s)
    where, params = ["f.alloc >= ?"], [int(float(args.get("min_size_mb") or 10) * 1e6)]
    if args.get("name_contains"):
        where.append("f.name LIKE ?")
        params.append("%" + str(args["name_contains"]).replace("%", "") + "%")
    if args.get("extension"):
        where.append("lower(f.name) LIKE ?")
        params.append("%." + str(args["extension"]).lower().lstrip("."))
    if args.get("older_than_days"):
        where.append("f.mtime < ?")
        params.append(time.time() - float(args["older_than_days"]) * 86400)
    if args.get("under_path"):
        pre = os.path.normpath(str(args["under_path"])).rstrip("\\") + "\\"
        where.append("(lower(d.path) = lower(?) OR lower(substr(d.path,1,?)) = lower(?))")
        params += [pre.rstrip("\\"), len(pre), pre]
    lim = max(1, min(200, int(args.get("limit") or 50)))
    try:
        rows = con.execute("SELECT d.path, f.name, f.alloc, f.mtime FROM files f JOIN dirs d ON d.id=f.dir WHERE "
                           + " AND ".join(where) + " ORDER BY f.alloc DESC LIMIT ?", (*params, lim)).fetchall()
    finally:
        con.close()
    return {"files": [{"path": r["path"].rstrip("\\") + "\\" + r["name"], "mb": _mb(r["alloc"]),
                       "modified": time.strftime("%Y-%m-%d", time.localtime(r["mtime"]))} for r in rows]}


def t_changes(args):
    from . import usn
    hours = max(1, min(24 * 30, float(args.get("hours") or 24)))
    a = usn.activity(_vol(args), time.time() - hours * 3600, time.time(), limit=40)
    if not a.get("available"):
        return {"error": "The NTFS journal has not been read yet (automatic collection must be on)."}
    return {"grew": [{"path": f["path"], "mb": _mb(f["delta"]), "kind": f["kind"]} for f in a["grow"]],
            "folders": [{"path": f["path"], "mb": _mb(f["delta"])} for f in a["folders"]],
            "total_grown_mb": _mb(a["totals"]["grow"]), "total_freed_mb": _mb(-a["totals"]["shrink"])}


def t_propose(args):
    prop = proposals.add(args.get("title"), args.get("items"), source="mcp")
    return {"proposal": prop["id"], "accepted": len(prop["items"]),
            "refused": [{"item": r.get("path") or r.get("program_id") or r.get("tool"), "reason": r["refused"]}
                        for r in prop["refused"]],
            "estimated_mb": _mb(sum(i.get("size") or 0 for i in prop["items"])),
            "next_step": "The proposal is now in DiskLens' 'Free up space' tab. Nothing was deleted: "
                         "the user must review and confirm it there."}


S_VOL = {"volume": {"type": "string", "description": "Drive letter, e.g. C: (default: system drive)"}}
TOOLS = [
    ("get_disk_overview", "Drive summary: usage by category, hidden space (Windows reserve, metadata) and Recycle Bin.",
     {"type": "object", "properties": S_VOL}, t_overview),
    ("list_cleanup_candidates", "Cleanup candidates found by DiskLens: safe caches (safe), Windows tools (tools), "
     "personal files to review (review), installed programs (programs) and large unknown folders (unknown).",
     {"type": "object", "properties": {**S_VOL, "section": {"type": "string",
                                                             "enum": ["safe", "tools", "review", "programs", "unknown"]}}},
     t_candidates),
    ("get_folder_details", "Folder details: size, what it is (if known), subfolders and largest files.",
     {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, t_folder),
    ("search_files", "Find large files by name, extension, age or folder.",
     {"type": "object", "properties": {**S_VOL, "name_contains": {"type": "string"}, "extension": {"type": "string"},
                                       "min_size_mb": {"type": "number"}, "older_than_days": {"type": "number"},
                                       "under_path": {"type": "string"}, "limit": {"type": "integer"}}}, t_search),
    ("get_recent_changes", "What grew on the drive in the last N hours, file by file (NTFS journal).",
     {"type": "object", "properties": {**S_VOL, "hours": {"type": "number"}}}, t_changes),
    ("propose_cleanup", "Propose a cleanup for the user to confirm in DiskLens (it does NOT delete anything). "
     "Actions: 'esvaziar' (empty a safe cache/temp folder; give path), 'lixeira' (send a personal file/folder to "
     "the Recycle Bin; give path), 'desinstalar' (uninstall; give program_id from list_cleanup_candidates), "
     "'ferramenta' (run a Windows tool; give tool: " + ", ".join(cleaner.TOOLS) + "). Windows and program "
     "folders are refused automatically.",
     {"type": "object", "required": ["title", "items"], "properties": {
         "title": {"type": "string", "description": "Proposal summary"},
         "items": {"type": "array", "items": {"type": "object", "required": ["action", "reason"], "properties": {
             "action": {"type": "string", "enum": sorted(proposals.ACTIONS)}, "path": {"type": "string"},
             "program_id": {"type": "string"}, "tool": {"type": "string"},
             "reason": {"type": "string", "description": "Why it is safe/useful, for the user to read"}}}}}},
     t_propose),
]
_BY_NAME = {t[0]: t for t in TOOLS}


def handle(msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        v = params.get("protocolVersion")
        return {"protocolVersion": v if v in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {}},
                "serverInfo": {"name": "disklens", "version": __version__},
                "instructions": "Disk usage data measured by DiskLens. Use propose_cleanup to suggest cleanups; "
                                "the user confirms them in the app. Reply in the user's language."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        if name not in _BY_NAME:
            raise KeyError(f"unknown tool: {name}")
        try:
            result = _BY_NAME[name][3](params.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    if mid is None:
        return None                                          # notification (e.g. notifications/initialized)
    raise KeyError(f"unsupported method: {method}")


def main():
    stdin = sys.stdin.buffer
    out = sys.stdout.buffer
    sys.stdout = sys.stderr                                  # stray prints must never corrupt the protocol
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = None
        try:
            result = handle(msg)
            if msg.get("id") is not None:
                resp = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except KeyError as e:
            if msg.get("id") is not None:
                resp = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": str(e)}}
        except Exception as e:
            traceback.print_exc()
            if msg.get("id") is not None:
                resp = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            out.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            out.flush()


if __name__ == "__main__":
    main()
