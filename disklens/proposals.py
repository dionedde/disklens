"""Cleanup proposals and the job that executes confirmed actions.

A proposal (from an AI through MCP, or from the in-app assistant) is only a list of suggested
actions. Each action is validated when proposed *and again* right before running.
Actions:
  esvaziar    - empty a folder marked "seguro" (permanent; caches/temp)
  lixeira     - send a file/folder to the Recycle Bin (reversible)
  desinstalar - open the program's own uninstaller
  ferramenta  - run one of Windows' cleanup tools (cleaner.TOOLS)
"""
import json
import os
import shutil
import threading
import time
import uuid

from . import cleaner, winapi
from .i18n import tr
from .config import data_dir

ACTIONS = {"esvaziar", "lixeira", "desinstalar", "ferramenta"}


def validate(item):
    """Normalize one action. Returns (item, None) or (item, reason it was refused)."""
    a = str(item.get("action", "")).lower()
    it = {"action": a, "reason": str(item.get("reason", ""))[:500]}
    if a not in ACTIONS:
        return it, tr("ação desconhecida")
    if a in ("esvaziar", "lixeira"):
        p = str(item.get("path") or "")
        it["path"] = os.path.normpath(p) if p else ""
        why = cleaner.protection_reason(it["path"], for_contents=(a == "esvaziar"))
        if why:
            return it, why
        if a == "esvaziar":
            idx, _ = cleaner.effective_rule(it["path"])
            info = cleaner.rules().info(idx)
            if not info or info["safety"] != "seguro":
                return it, tr("só pastas de cache/temporários marcadas como seguras podem ser esvaziadas")
            if not os.path.isdir(it["path"]):
                return it, tr("não é uma pasta")
        try:
            it["size"] = cleaner.path_size(it["path"]) if a == "lixeira" or os.path.isdir(it["path"]) else 0
        except OSError:
            it["size"] = 0
        return it, None
    if a == "ferramenta":
        it["tool"] = str(item.get("tool") or item.get("id", "")).replace("tool:", "")
        if it["tool"] not in cleaner.TOOLS:
            return it, "ferramenta desconhecida"
        return it, None
    if a == "desinstalar":
        from .candidates import installed_programs
        pid = str(item.get("program_id") or item.get("id") or "")
        it["program_id"] = pid
        prog = next((p for p in installed_programs() if p["id"] == pid), None)
        if not prog:
            return it, tr("programa não encontrado")
        it["name"] = prog["name"]
        it["size"] = prog["size"]
        if prog["dependency"]:
            it["warning"] = "outros programas podem depender deste"
        return it, None
    return it, tr("inválido")


# ------------------------------------------------------------------ proposals storage
def _path():
    return data_dir() / "proposals.json"


def load():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(lst):
    _path().write_text(json.dumps(lst[-20:], ensure_ascii=False, indent=1), encoding="utf-8")


def add(title, items, source):
    accepted, refused = [], []
    for raw in items or []:
        it, why = validate(raw if isinstance(raw, dict) else {})
        (refused if why else accepted).append({**it, **({"refused": why} if why else {})})
    prop = {"id": uuid.uuid4().hex[:10], "ts": time.time(), "source": source, "title": str(title or "")[:200],
            "items": accepted, "refused": refused, "status": "pending"}
    lst = load()
    lst.append(prop)
    _save(lst)
    return prop


def set_status(pid, status):
    lst = load()
    for p in lst:
        if p["id"] == pid:
            p["status"] = status
    _save(lst)


# ------------------------------------------------------------------ execution job
_jobs = {}
_lock = threading.Lock()


def start_job(items, proposal_id=None):
    job = {"id": uuid.uuid4().hex[:10], "started": time.time(), "state": "running", "total": len(items),
           "done": 0, "current": "", "results": [], "freed": 0, "recycled": 0, "proposal": proposal_id}
    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise RuntimeError(tr("já existe uma limpeza em andamento"))
        _jobs[job["id"]] = job
    threading.Thread(target=_run, args=(job, items), daemon=True).start()
    return job


def get_job(jid):
    return _jobs.get(jid)


def _run(job, items):
    sysd = winapi.system_drive() + "\\"
    free_before = shutil.disk_usage(sysd).free
    for raw in items:
        it, why = validate(raw)                                   # check again right before acting
        label = it.get("path") or it.get("name") or tr(cleaner.TOOLS.get(it.get("tool", ""), {}).get("label", ""))
        job["current"] = label
        res = {"action": it["action"], "target": label, "ok": False, "bytes": 0, "message": ""}
        if why:
            res["message"] = tr("recusado: {why}", why=why)
        else:
            try:
                if it["action"] == "esvaziar":
                    freed, n, skipped = cleaner.clean_contents(it["path"])
                    res.update(ok=True, bytes=freed, message=tr("{n} arquivos apagados", n=n) +
                               (tr(", {n} em uso foram mantidos", n=skipped) if skipped else ""))
                    job["freed"] += freed
                elif it["action"] == "lixeira":
                    moved = cleaner.recycle(it["path"])
                    res.update(ok=True, bytes=moved, message=tr("enviado para a Lixeira"))
                    job["recycled"] += moved
                elif it["action"] == "ferramenta":
                    ok, msg, freed = cleaner.run_tool(it["tool"])
                    res.update(ok=ok, bytes=freed or 0, message=msg[:500])
                    if it["tool"] == "empty_recycle":
                        job["freed"] += freed or 0
                elif it["action"] == "desinstalar":
                    from .candidates import launch_uninstaller
                    launch_uninstaller(it["program_id"])
                    res.update(ok=True, message="desinstalador aberto: conclua na janela dele")
            except Exception as e:                                # keep going with the other items
                res["message"] = str(e)[:500]
        cleaner.log_action(it["action"], label, it.get("tool", ""), res["bytes"], res["ok"], res["message"])
        job["results"].append(res)
        job["done"] += 1
    job["disk_freed"] = shutil.disk_usage(sysd).free - free_before
    job["recycle_bin"] = cleaner.recycle_bin_info()
    job["state"] = "done"
    job["finished"] = time.time()
    if job.get("proposal"):
        set_status(job["proposal"], "executed")
