"""Compare two snapshots of the same drive: what grew, what shrank, and why.

The folder list is reduced to "explainers": the most specific folders that account for a
change. A folder whose growth is mostly inside one child is replaced by that child; growth
spread over many small children stays on the parent ("demais conteúdos"). Listed amounts never
overlap, so they add up to the total change.
"""
import json
import sqlite3

from . import db
from .rules import Rules

MB = 1024 ** 2
GB = 1024 ** 3


def _parent(p):
    if p.endswith(":\\"):
        return None
    par = p.rsplit("\\", 1)[0]
    return par + "\\" if par.endswith(":") else par


def _load(snap_id):
    con = sqlite3.connect(f"file:{db.snap_path(snap_id)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rules(con):
    row = con.execute("SELECT value FROM meta WHERE key='rules'").fetchone()
    return Rules(json.loads(row[0])) if row else Rules()


def _hidden(con):
    row = con.execute("SELECT value FROM meta WHERE key='hidden'").fetchone()
    return json.loads(row[0]) if row else None


def _hardlink_method(con):
    """'2' = hard links deduplicated on the whole volume; absent = only under WINDIR (older scans)."""
    row = con.execute("SELECT value FROM meta WHERE key='hardlinks'").fetchone()
    return row[0] if row else "1"


METHOD_NOTE = ("Atenção: o snapshot mais antigo foi feito antes de uma correção na contagem. Arquivos com "
               "vários nomes (hard links, como os do Edge, do Edge WebView e do Conda) eram somados mais de "
               "uma vez fora da pasta do Windows, então parte da diferença nas pastas é só a correção, não "
               "espaço que mudou de verdade. O espaço usado do disco é medido pelo Windows e não é afetado.")

FILES_SUFFIX ="\\*"     # pseudo-node "loose files of this folder" ('*' is never part of a real name)


def _sizes(dirs):
    """path -> size, plus a pseudo-node per folder for the files directly inside it."""
    out = {}
    child_sum = {}
    for p, (alloc, _rule, _id, _own) in dirs.items():
        out[p] = alloc
        par = _parent(p)
        if par is not None:
            child_sum[par] = child_sum.get(par, 0) + alloc
    for p, (alloc, _rule, _id, _own) in dirs.items():
        loose = alloc - child_sum.get(p, 0)      # includes files with their own rule (pagefile.sys)
        if loose > 0:
            out[p.rstrip("\\") + FILES_SUFFIX] = loose
    return out


def explainers(a_dirs, b_dirs, total_delta, limit=30):
    sa, sb = _sizes(a_dirs), _sizes(b_dirs)
    deltas = {}
    for p in set(sa) | set(sb):
        d = sb.get(p, 0) - sa.get(p, 0)
        if d:
            deltas[p] = d
    thr = max(64 * MB, abs(total_delta) * 0.02)
    out = []
    for sign in (1, -1):                       # growth and shrinkage are explained separately
        covered = {}
        cands = [p for p, d in deltas.items() if d * sign >= thr]
        for p in sorted(cands, key=lambda x: -len(x.rstrip("\\").split("\\"))):   # deepest first
            d = deltas[p]
            cov = covered.get(p, 0)
            if cov * sign >= 0.6 * d * sign:   # a few listed sub-items already explain it
                continue
            amount = d - cov
            if amount * sign < thr:
                continue
            is_files = p.endswith(FILES_SUFFIX)
            real = p[:-len(FILES_SUFFIX)] if is_files else p
            if is_files and len(real) == 2:
                real += "\\"
            out.append({"path": real, "kind": "files" if is_files else "dir", "delta": amount, "total_delta": d,
                        "partial": cov != 0, "before": sa.get(p, 0), "after": sb.get(p, 0),
                        "new": p not in sa, "gone": p not in sb})
            q = _parent(p)
            while q is not None:
                covered[q] = covered.get(q, 0) + amount
                q = _parent(q)
    out.sort(key=lambda x: -abs(x["delta"]))
    return out[:limit]


def _big_files(con):
    if not con.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        return None
    return {r[0].rstrip("\\") + "\\" + r[1]: (r[2], r[3]) for r in con.execute(
        "SELECT d.path, f.name, f.alloc, f.mtime FROM files f JOIN dirs d ON d.id=f.dir WHERE f.alloc >= ?",
        (db.KEEP_FILES_MIN,))}


def compare(a_id, b_id):
    sa, sb = db.get_snapshot(a_id), db.get_snapshot(b_id)
    if not sa or not sb or sa["status"] != "done" or sb["status"] != "done":
        raise ValueError("snapshot inválido")
    if sa["volume"] != sb["volume"]:
        raise ValueError("os snapshots são de discos diferentes")
    if sa["finished"] > sb["finished"]:
        sa, sb = sb, sa
    ca, cb = _load(sa["id"]), _load(sb["id"])
    rules_b, rules_a = _rules(cb), _rules(ca)
    try:
        q = "SELECT path, alloc, rule, id, own_alloc FROM dirs"
        a_dirs = {r[0]: (r[1], r[2], r[3], r[4]) for r in ca.execute(q)}
        b_dirs = {r[0]: (r[1], r[2], r[3], r[4]) for r in cb.execute(q)}
        used_delta = sb["used"] - sa["used"]
        items = explainers(a_dirs, b_dirs, used_delta)
        for it in items:
            src, rules = (b_dirs, rules_b) if it["path"] in b_dirs else (a_dirs, rules_a)
            ridx = src[it["path"]][1]
            info = rules.info(ridx)
            it["cat"] = rules.category(ridx)
            it["label"] = info["label"] if info else None
            it["safety"] = info["safety"] if info else None
            it["dir_b"] = b_dirs[it["path"]][2] if it["path"] in b_dirs else None
            if it["kind"] == "files" and it["dir_b"]:
                top = cb.execute("SELECT name, alloc, rule FROM files WHERE dir=? ORDER BY alloc DESC LIMIT 3",
                                 (it["dir_b"],)).fetchall()
                it["top_files"] = [{"name": t[0], "alloc": t[1]} for t in top]
                if top and top[0][2] is not None:          # e.g. pagefile.sys
                    finfo = rules_b.info(top[0][2])
                    it["cat"], it["label"], it["safety"] = finfo["cat"], finfo["label"], finfo["safety"]

        cats_a, cats_b = db.snapshot_cats(sa) or {}, db.snapshot_cats(sb) or {}
        cat_delta = {c: cats_b.get(c, 0) - cats_a.get(c, 0) for c in set(cats_a) | set(cats_b)}
        ha, hb = _hidden(ca), _hidden(cb)
        method_changed = _hardlink_method(ca) != _hardlink_method(cb)
        hidden_delta = []
        if ha and hb:
            ia = {i["key"]: i for i in ha["items"]}
            for i in hb["items"]:
                if i["in_scan"]:
                    continue
                before = ia.get(i["key"], {}).get("bytes", 0) if not ia.get(i["key"], {}).get("in_scan") else 0
                hidden_delta.append({"key": i["key"], "label": i["label"], "delta": i["bytes"] - before})

        fa, fb = _big_files(ca), _big_files(cb)
        files = None
        if fa is not None and fb is not None:
            grown = []
            for p, (size, mt) in fb.items():
                before = fa.get(p, (0, 0))[0]
                if size - before >= db.KEEP_FILES_MIN:
                    grown.append({"path": p, "before": before, "after": size, "delta": size - before,
                                  "new": p not in fa, "mtime": mt})
            removed = [{"path": p, "before": s, "after": 0, "delta": -s, "new": False, "mtime": mt}
                       for p, (s, mt) in fa.items() if p not in fb]
            grown.sort(key=lambda x: -x["delta"])
            removed.sort(key=lambda x: x["delta"])
            files = {"grown": grown[:25], "removed": removed[:10]}
    finally:
        ca.close()
        cb.close()

    with db.index_conn() as con:
        events = [dict(r) for r in con.execute(
            "SELECT * FROM events WHERE ts BETWEEN ? AND ? AND (volume=? OR volume IS NULL) ORDER BY ts",
            (sa["finished"], sb["finished"], sa["volume"]))]
        samples = [dict(r) for r in con.execute(
            "SELECT ts, used, free, pagefile FROM samples WHERE volume=? AND ts BETWEEN ? AND ? ORDER BY ts",
            (sa["volume"], sa["started"], sb["finished"]))]
    jumps = []
    for x, y in zip(samples, samples[1:]):
        d = y["used"] - x["used"]
        if abs(d) >= 256 * MB:
            jumps.append({"from": x["ts"], "to": y["ts"], "delta": d,
                          "pagefile": (y["pagefile"] or 0) - (x["pagefile"] or 0)})
    jumps.sort(key=lambda j: -abs(j["delta"]))

    from . import usn
    try:
        activity = usn.activity(sa["volume"], sa["finished"], sb["finished"], limit=25)
    except Exception:
        activity = {"available": False}

    return {"a": sa, "b": sb, "activity": activity, "used_delta": used_delta, "free_delta": sb["free"] - sa["free"],
            "scanned_delta": sb["scanned"] - sa["scanned"], "items": items, "cat_delta": cat_delta,
            "hidden_delta": hidden_delta, "files": files, "events": events, "jumps": jumps[:8],
            "categories": rules_b.categories, "method_note": METHOD_NOTE if method_changed else None,
            "summary": summarize(sa, sb, used_delta, items, hidden_delta)
            + (" " + METHOD_NOTE if method_changed else "")}


def _fmt(b):
    v = abs(b)
    s = f"{v / GB:.1f} GB" if v >= GB else f"{v / MB:.0f} MB"
    return s.replace(".", ",")


def summarize(sa, sb, used_delta, items, hidden_delta):
    if abs(used_delta) < 64 * MB:
        head = "O espaço usado ficou praticamente igual"
    else:
        head = (f"O espaço usado {'aumentou' if used_delta > 0 else 'diminuiu'} {_fmt(used_delta)}")
    parts = []
    sign = 1 if used_delta >= 0 else -1
    movers = [i for i in items if i["delta"] * sign > 0][:3]
    for i in movers:
        name = i["label"] or i["path"].rstrip("\\").rsplit("\\", 1)[-1]
        if i["kind"] == "files" and i.get("top_files") and not i["label"]:
            name = i["top_files"][0]["name"]
        parts.append(f"{name} ({'+' if i['delta'] > 0 else '−'}{_fmt(i['delta'])})")
    for h in hidden_delta:
        if abs(h["delta"]) >= 256 * MB and h["delta"] * sign > 0:
            parts.append(f"{h['label'].lower()} ({'+' if h['delta'] > 0 else '−'}{_fmt(h['delta'])})")
    if parts:
        return f"{head}. Principais responsáveis: " + "; ".join(parts) + "."
    return head + "."
