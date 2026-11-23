"""MCP server (Model Context Protocol, stdio transport) so any MCP-capable AI client can look at
the disk data and *propose* a cleanup. There is deliberately no tool that deletes anything:
proposals appear in DiskLens' "Liberar espaço" tab and run only after the user confirms.

Client configuration (e.g. Claude Desktop, Cursor, VS Code, Gemini CLI):
  command: <python.exe>   args: ["<pasta do DiskLens>\\disklens_mcp.py"]
"""
import json
import os
import sqlite3
import sys
import time
import traceback

from . import __version__, candidates, cleaner, db, proposals, winapi

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]


def _vol(args):
    return (args.get("volume") or winapi.system_drive()).upper()[:2]


def _mb(b):
    return round((b or 0) / 1e6, 1)


def t_overview(args):
    vol = _vol(args)
    s = candidates.latest_snapshot(vol)
    if not s:
        return {"erro": f"Ainda não há snapshot do disco {vol}. Peça ao usuário para escanear no DiskLens."}
    con, rules = candidates._open(s)
    try:
        cats = {rules.categories.get(c, {}).get("label", c): _mb(a) for c, a in
                con.execute("SELECT cat, alloc FROM cat_stats ORDER BY alloc DESC")}
        row = con.execute("SELECT value FROM meta WHERE key='hidden'").fetchone()
        hidden = [{"item": i["label"], "mb": _mb(i["bytes"]), "medido": i["exact"]}
                  for i in (json.loads(row[0])["items"] if row else [])]
    finally:
        con.close()
    return {"disco": vol, "snapshot": time.strftime("%d/%m/%Y %H:%M", time.localtime(s["finished"])),
            "total_gb": round(s["total"] / 1e9, 1), "usado_gb": round(s["used"] / 1e9, 1),
            "livre_gb": round(s["free"] / 1e9, 1), "mb_por_categoria": cats, "espaco_oculto": hidden,
            "lixeira_mb": _mb(cleaner.recycle_bin_info()["bytes"])}


def t_candidates(args):
    d = candidates.build(_vol(args))
    if not d.get("available"):
        return {"erro": "Ainda não há snapshot. Peça ao usuário para escanear no DiskLens."}
    sec = args.get("section")
    out = {
        "safe": [{"path": i["path"], "mb": _mb(i["size"]), "o_que_e": i["label"], "acao": "esvaziar"} for i in d["safe"]],
        "tools": [{"id": i["tool"], "label": i["label"], "mb_estimado": _mb(i["size"]), "descricao": i["desc"],
                   "precisa_admin": i["needs_admin"], "acao": "ferramenta"} for i in d["tools"]],
        "review": [{"path": i["path"], "mb": _mb(i["size"]), "grupo": i["group"], "motivo": i["reason"],
                    "acao": "lixeira"} for i in d["review"]],
        "programs": [{"id": p["id"], "name": p["name"], "publisher": p["publisher"], "drive": p["drive"],
                      "mb": _mb(p["size"]), "dependencia": p["dependency"], "acao": "desinstalar",
                      "instalado": time.strftime("%d/%m/%Y", time.localtime(p["installed"])) if p["installed"] else None}
                     for p in d["programs"][:100]],
        "unknown": [{"path": u["path"], "mb": _mb(u["size"]), "subpastas": [k["name"] for k in u["children"]]}
                    for u in d["unknown"]],
    }
    return {sec: out[sec]} if sec in out else out


def t_folder(args):
    path = os.path.normpath(str(args.get("path") or ""))
    vol = path[:2].upper()
    s = candidates.latest_snapshot(vol)
    if not s:
        return {"erro": "sem snapshot deste disco"}
    con, rules = candidates._open(s)
    try:
        r = con.execute("SELECT * FROM dirs WHERE lower(path)=lower(?)", (path,)).fetchone()
        if not r:
            return {"erro": "pasta não encontrada no último snapshot"}
        info = rules.info(r["rule"])
        return {
            "path": r["path"], "mb": _mb(r["alloc"]), "arquivos": r["nfiles"], "subpastas_total": r["ndirs"],
            "ultima_alteracao": time.strftime("%d/%m/%Y", time.localtime(r["mtime"] or 0)),
            "regra": {"o_que_e": info["label"], "explicacao": info["desc"], "seguranca": info["safety"]} if info else None,
            "pode_ir_para_lixeira": cleaner.protection_reason(r["path"]) or "sim",
            "subpastas": [{"name": k["name"], "mb": _mb(k["alloc"])} for k in con.execute(
                "SELECT name, alloc FROM dirs WHERE parent=? ORDER BY alloc DESC LIMIT 30", (r["id"],))],
            "maiores_arquivos": [{"name": f["name"], "mb": _mb(f["alloc"]),
                                  "alterado": time.strftime("%d/%m/%Y", time.localtime(f["mtime"]))}
                                 for f in con.execute("SELECT name, alloc, mtime FROM files WHERE dir=? "
                                                      "ORDER BY alloc DESC LIMIT 30", (r["id"],))],
        }
    finally:
        con.close()


def t_search(args):
    vol = _vol(args)
    s = candidates.latest_snapshot(vol)
    if not s:
        return {"erro": "sem snapshot"}
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
    return {"arquivos": [{"path": r["path"].rstrip("\\") + "\\" + r["name"], "mb": _mb(r["alloc"]),
                          "alterado": time.strftime("%d/%m/%Y", time.localtime(r["mtime"]))} for r in rows]}


def t_changes(args):
    from . import usn
    hours = max(1, min(24 * 30, float(args.get("hours") or 24)))
    a = usn.activity(_vol(args), time.time() - hours * 3600, time.time(), limit=40)
    if not a.get("available"):
        return {"erro": "O journal do NTFS ainda não foi lido (precisa da coleta automática ativa)."}
    return {"cresceram": [{"path": f["path"], "mb": _mb(f["delta"]), "tipo": f["kind"]} for f in a["grow"]],
            "pastas": [{"path": f["path"], "mb": _mb(f["delta"])} for f in a["folders"]],
            "total_cresceu_mb": _mb(a["totals"]["grow"]), "total_liberado_mb": _mb(-a["totals"]["shrink"])}


def t_propose(args):
    prop = proposals.add(args.get("title"), args.get("items"), source="mcp")
    return {"proposta": prop["id"], "aceitos": len(prop["items"]),
            "recusados": [{"item": r.get("path") or r.get("program_id") or r.get("tool"), "motivo": r["refused"]}
                          for r in prop["refused"]],
            "mb_estimados": _mb(sum(i.get("size") or 0 for i in prop["items"])),
            "proximo_passo": "A proposta apareceu na aba 'Liberar espaço' do DiskLens. Nada foi apagado: "
                             "o usuário precisa revisar e confirmar lá."}


S_VOL = {"volume": {"type": "string", "description": "Letra do disco, ex.: C: (padrão: disco do sistema)"}}
TOOLS = [
    ("get_disk_overview", "Resumo do disco: uso por categoria, espaço oculto (reserva do Windows, metadados) e Lixeira.",
     {"type": "object", "properties": S_VOL}, t_overview),
    ("list_cleanup_candidates", "Candidatos à limpeza encontrados pelo DiskLens: caches seguros (safe), ferramentas do "
     "Windows (tools), arquivos pessoais para revisar (review), programas instalados (programs) e pastas grandes "
     "desconhecidas (unknown).",
     {"type": "object", "properties": {**S_VOL, "section": {"type": "string",
                                                             "enum": ["safe", "tools", "review", "programs", "unknown"]}}},
     t_candidates),
    ("get_folder_details", "Detalhes de uma pasta: tamanho, o que ela é (se conhecida), subpastas e maiores arquivos.",
     {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}, t_folder),
    ("search_files", "Procura arquivos grandes por nome, extensão, idade ou pasta.",
     {"type": "object", "properties": {**S_VOL, "name_contains": {"type": "string"}, "extension": {"type": "string"},
                                       "min_size_mb": {"type": "number"}, "older_than_days": {"type": "number"},
                                       "under_path": {"type": "string"}, "limit": {"type": "integer"}}}, t_search),
    ("get_recent_changes", "O que cresceu no disco nas últimas N horas, arquivo a arquivo (journal do NTFS).",
     {"type": "object", "properties": {**S_VOL, "hours": {"type": "number"}}}, t_changes),
    ("propose_cleanup", "Propõe uma limpeza para o usuário confirmar no DiskLens (NÃO apaga nada). Ações: "
     "'esvaziar' (pasta de cache/temporários segura, informe path), 'lixeira' (arquivo/pasta pessoal, informe path), "
     "'desinstalar' (informe program_id de list_cleanup_candidates), 'ferramenta' (informe tool: "
     + ", ".join(cleaner.TOOLS) + "). Pastas do Windows e de programas são recusadas automaticamente.",
     {"type": "object", "required": ["title", "items"], "properties": {
         "title": {"type": "string", "description": "Resumo da proposta"},
         "items": {"type": "array", "items": {"type": "object", "required": ["action", "reason"], "properties": {
             "action": {"type": "string", "enum": sorted(proposals.ACTIONS)}, "path": {"type": "string"},
             "program_id": {"type": "string"}, "tool": {"type": "string"},
             "reason": {"type": "string", "description": "Por que é seguro/útil, para o usuário ler"}}}}}},
     t_propose),
]
_BY_NAME = {t[0]: t for t in TOOLS}


def handle(msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        v = params.get("protocolVersion")
        return {"protocolVersion": v if v in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {}},
                "serverInfo": {"name": "disklens", "version": __version__},
                "instructions": "Dados de uso do disco medidos pelo DiskLens. Use propose_cleanup para sugerir "
                                "limpezas; o usuário confirma no aplicativo. Responda ao usuário em português."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        if name not in _BY_NAME:
            raise KeyError(f"ferramenta desconhecida: {name}")
        try:
            result = _BY_NAME[name][3](params.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Erro: {e}"}], "isError": True}
    if mid is None:
        return None                                          # notification (e.g. notifications/initialized)
    raise KeyError(f"método não suportado: {method}")


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
