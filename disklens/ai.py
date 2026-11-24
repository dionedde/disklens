"""AI assistant, provider-agnostic (standard library only).

Providers: OpenAI, Google Gemini, DeepSeek and Ollama/any OpenAI-compatible server use the
`/chat/completions` format; Anthropic uses `/messages`. API keys are stored encrypted with
Windows DPAPI (only this Windows user can decrypt them).

The model only *recommends*: it receives candidate items with ids and answers with verdicts for
those ids. Paths it may invent are ignored; nothing is deleted without the user confirming.
What is sent: paths, names and sizes of folders/files, program names. Never file contents.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import re
import time
import urllib.error
import urllib.request

from . import candidates, db
from .i18n import language_name, tr
from .config import data_dir, load as load_config, save as save_config

PROVIDERS = {
    "openai": {"label": "OpenAI (ChatGPT)", "base": "https://api.openai.com/v1", "style": "openai", "key": True,
               "model": ""},
    "google": {"label": "Google (Gemini)", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "style": "openai", "key": True, "model": ""},
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com/v1", "style": "openai", "key": True,
                 "model": "deepseek-chat"},
    "anthropic": {"label": "Anthropic (Claude)", "base": "https://api.anthropic.com/v1", "style": "anthropic",
                  "key": True, "model": "claude-opus-5-5"},
    "ollama": {"label": "Ollama (IA local, nada sai do PC)", "base": "http://localhost:11434/v1", "style": "openai",
               "key": False, "model": ""},
    "custom": {"label": "Outro serviço compatível com OpenAI", "base": "", "style": "openai", "key": False,
               "model": ""},
}


class AIError(Exception):
    pass


# ------------------------------------------------------------------ DPAPI
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt(data, protect):
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    buf = ctypes.create_string_buffer(data, len(data))
    src = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = _BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):   # UI_FORBIDDEN
        raise AIError(tr("não foi possível (des)criptografar a chave com o Windows"))
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)


def protect(text):
    return base64.b64encode(_crypt(text.encode("utf-8"), True)).decode("ascii")


def unprotect(b64):
    return _crypt(base64.b64decode(b64), False).decode("utf-8")


# ------------------------------------------------------------------ settings
def settings(public=True):
    cfg = load_config().get("ai") or {}
    prov = cfg.get("provider") or ""
    out = {"provider": prov, "model": cfg.get("model") or "", "base_url": cfg.get("base_url") or "",
           "providers": {k: {"label": tr(v["label"]), **{kk: v[kk] for kk in ("key", "model", "base")}}
                         for k, v in PROVIDERS.items()},
           "has_key": {k: bool((cfg.get("keys") or {}).get(k)) for k in PROVIDERS},
           "consented": bool(cfg.get("consented"))}
    if not public:
        out["_keys"] = cfg.get("keys") or {}
    return out


def save_settings(provider=None, model=None, base_url=None, api_key=None, consented=None, clear_key=False):
    cfg = load_config()
    ai = cfg.get("ai") or {}
    if provider is not None:
        if provider not in PROVIDERS:
            raise AIError("provedor desconhecido")
        ai["provider"] = provider
    if model is not None:
        ai["model"] = model.strip()
    if base_url is not None:
        ai["base_url"] = base_url.strip().rstrip("/")
    if consented is not None:
        ai["consented"] = bool(consented)
    p = ai.get("provider")
    if api_key:
        ai.setdefault("keys", {})[p] = protect(api_key.strip())
    if clear_key and p:
        (ai.get("keys") or {}).pop(p, None)
    cfg["ai"] = ai
    save_config(cfg)
    return settings()


def _active():
    s = settings(public=False)
    p = s["provider"]
    if not p:
        raise AIError(tr("Escolha um provedor de IA nas configurações do assistente."))
    spec = PROVIDERS[p]
    base = (s["base_url"] if p in ("custom", "ollama") and s["base_url"] else spec["base"]).rstrip("/")
    if not base:
        raise AIError(tr("Informe o endereço (URL) do serviço."))
    key = None
    if s["_keys"].get(p):
        key = unprotect(s["_keys"][p])
    elif spec["key"]:
        raise AIError(tr("Informe a chave de API de {provider}.", provider=tr(spec["label"])))
    return p, spec, base, key, s["model"] or spec["model"]


# ------------------------------------------------------------------ HTTP
def _http(url, payload=None, headers=None, timeout=180):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "User-Agent": "DiskLens", **(headers or {})},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
            msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error")
            body = msg or body
        except ValueError:
            pass
        raise AIError(tr("O serviço respondeu {code}: {body}", code=e.code, body=str(body)[:300]))
    except urllib.error.URLError as e:
        raise AIError(tr("Não foi possível conectar a {host}: {reason}", host=url.split("/")[2], reason=e.reason))
    except TimeoutError:
        raise AIError(tr("O serviço demorou demais para responder."))


def _headers(spec, key):
    if spec["style"] == "anthropic":
        return {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"} if key else {}


def list_models():
    p, spec, base, key, _ = _active()
    j = _http(base + "/models", headers=_headers(spec, key), timeout=30)
    ids = [m.get("id") for m in (j.get("data") or j.get("models") or []) if isinstance(m, dict) and m.get("id")]
    return sorted(i.replace("models/", "") for i in ids)


def chat(system, user, max_tokens=8000):
    p, spec, base, key, model = _active()
    if not model:
        raise AIError(tr("Escolha um modelo nas configurações do assistente."))
    if spec["style"] == "anthropic":
        j = _http(base + "/messages", {"model": model, "max_tokens": max_tokens, "system": system,
                                       "messages": [{"role": "user", "content": user}]}, _headers(spec, key))
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    else:
        j = _http(base + "/chat/completions", {"model": model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}]}, _headers(spec, key))
        try:
            text = j["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise AIError(tr("Resposta inesperada do serviço."))
    return text, {"provider": p, "model": model}


def extract_json(text):
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise AIError(tr("A IA não respondeu no formato esperado (JSON)."))
    try:
        return json.loads(t[a:b + 1])
    except ValueError as e:
        raise AIError(tr("A IA respondeu um JSON inválido: {error}", error=e))


# ------------------------------------------------------------------ analysis
SYSTEM = """You help a person free up disk space on a Windows computer, safely.
You receive data measured by the DiskLens program: cleanup candidates (each with an "id"), installed
programs and large folders that DiskLens does not recognize. You only RECOMMEND; the person decides and confirms.
(Field names in the data are in Portuguese: "dias_sem_alterar" = days unchanged, "motivo_detectado" = detected
reason, "dependencia" = dependency, "subpastas"/"arquivos" = subfolders/files.)

Rules:
1. When in doubt, recommend "revisar" (review). Never recommend deleting anything Windows or a program in use depends on.
2. Items of type "safe" are caches/temporary files that are recreated automatically: usually "apagar" (delete).
3. Programs: "desinstalar" (uninstall) only with good signs they are not used (e.g. duplicates, old versions of
   something that has a newer one, games/programs unchanged for a long time). Never for dependencies
   (Visual C++, .NET, drivers, runtimes) or the system itself. Consider the "drive" field: uninstalling
   something from another drive frees nothing on the analyzed drive, unless the reason is a different one.
4. For unknown folders ("unknown"), say which program they most likely belong to and whether removing them is safe.
5. Be specific and short (1-2 sentences per item). Write "summary" and every "reason" in {language}.
6. Reply ONLY with JSON in this format, no text outside it ("verdict" values stay exactly as written):
{{"summary": "overview in 2-4 sentences, including how much can be freed safely",
 "items": [{{"id": "<received id>", "verdict": "apagar|desinstalar|manter|revisar",
            "confidence": 0.0-1.0, "reason": "why"}}]}}
Include at most 80 items, biggest first. Use only received ids."""


def _compact(data, volume):
    now = time.time()

    def age(ts):
        return int((now - ts) / 86400) if ts else None
    snap = data["snapshot"]
    out = {
        "disco": {"volume": volume, "total_gb": round(snap["total"] / 1e9, 1), "usado_gb": round(snap["used"] / 1e9, 1),
                  "livre_gb": round(snap["free"] / 1e9, 1)},
        "safe": [{"id": i["id"], "path": i["path"], "mb": round(i["size"] / 1e6), "regra": i["label"]}
                 for i in data["safe"]],
        "tools": [{"id": i["id"], "label": i["label"], "mb": round((i["size"] or 0) / 1e6), "desc": i["desc"]}
                  for i in data["tools"]],
        "review": [{"id": i["id"], "path": i["path"], "mb": round(i["size"] / 1e6), "dias_sem_alterar": age(i["mtime"]),
                    "motivo_detectado": i["reason"]} for i in data["review"]],
        "programs": [{"id": p["id"], "name": p["name"], "publisher": p["publisher"], "drive": p["drive"],
                      "mb": round((p["size"] or 0) / 1e6), "instalado_ha_dias": age(p["installed"]),
                      "dias_sem_alterar_pasta": age(p["last_change"]), "dependencia": p["dependency"]}
                     for p in data["programs"][:70]],
        "unknown": [{"id": u["id"], "path": u["path"], "mb": round(u["size"] / 1e6),
                     "subpastas": [f"{k['name']} ({round(k['size'] / 1e6)} MB)" for k in u["children"]],
                     "arquivos": [f"{k['name']} ({round(k['size'] / 1e6)} MB)" for k in u["files"]]}
                    for u in data["unknown"]],
    }
    return out


def analyze(volume):
    data = candidates.build(volume)
    if not data.get("available"):
        raise AIError(tr("Faça um scan do disco antes de pedir a análise."))
    ctx = _compact(data, volume)
    valid = {x["id"] for k in ("safe", "tools", "review", "programs", "unknown") for x in ctx[k]}
    text, meta = chat(SYSTEM.format(language=language_name()),
                      "DiskLens data (JSON):\n" + json.dumps(ctx, ensure_ascii=False))
    j = extract_json(text)
    items = []
    for it in j.get("items") or []:
        if not isinstance(it, dict) or it.get("id") not in valid:
            continue                                         # ignore anything the model invented
        v = str(it.get("verdict", "revisar")).lower()
        items.append({"id": it["id"], "verdict": v if v in ("apagar", "desinstalar", "manter", "revisar") else "revisar",
                      "confidence": max(0.0, min(1.0, float(it.get("confidence") or 0))),
                      "reason": str(it.get("reason") or "")[:500]})
    result = {"ts": time.time(), "volume": volume, "summary": str(j.get("summary") or "")[:2000],
              "items": items, **meta}
    (data_dir() / "ai_last.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def last_analysis():
    p = data_dir() / "ai_last.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def explain(volume, path):
    s = candidates.latest_snapshot(volume)
    if not s:
        raise AIError(tr("Faça um scan do disco antes."))
    con, _rules = candidates._open(s)
    try:
        r = con.execute("SELECT id, alloc, mtime FROM dirs WHERE lower(path)=lower(?)", (path,)).fetchone()
        if not r:
            raise AIError(tr("Pasta não encontrada no último snapshot."))
        kids = [f"{k['name']}/ ({round(k['alloc'] / 1e6)} MB)" for k in con.execute(
            "SELECT name, alloc FROM dirs WHERE parent=? ORDER BY alloc DESC LIMIT 20", (r["id"],))]
        files = [f"{k['name']} ({round(k['alloc'] / 1e6)} MB)" for k in con.execute(
            "SELECT name, alloc FROM files WHERE dir=? ORDER BY alloc DESC LIMIT 15", (r["id"],))]
    finally:
        con.close()
    user = (f"Folder: {path}\nSize: {round(r['alloc'] / 1e6)} MB\nLast change inside it: "
            f"{time.strftime('%Y-%m-%d', time.localtime(r['mtime'] or 0))}\nSubfolders: {', '.join(kids) or '-'}\n"
            f"Files: {', '.join(files) or '-'}\n\nWhat is this folder, which program does it most likely belong to, "
            "and is it safe to delete? If it belongs to a program, say how to remove it the right way. "
            "Answer in up to 6 sentences.")
    text, meta = chat("You are a Windows expert who helps people free up disk space safely. "
                      f"Answer in {language_name()}, directly.", user, max_tokens=1500)
    return {"text": text.strip(), **meta}
