"""Languages: Portuguese (source), English, Spanish.

The source strings in the code are Portuguese; ``locales.json`` maps each one to English and
Spanish (a list of ``[pt, en, es]`` triples). Templates use ``{name}`` placeholders:

    tr("{n} arquivos", n=3)   ->  "3 files" in English

The UI (web/app.js) uses the same catalog through ``/api/i18n``. The language comes from
config.json ("lang"); when it is not set, it follows the Windows display language.
"""
import ctypes
import json
import os

from .config import CONFIG_FILE, PKG_DIR

LANGS = {"en": "English", "pt": "Português", "es": "Español"}
LOCALES = {"en": "en-US", "pt": "pt-BR", "es": "es-ES"}
CATALOG_FILE = PKG_DIR / "locales.json"

_catalog = None
_lang_cache = (None, None)        # (config mtime, lang)
_forced = None                    # tests / one-off overrides


def windows_lang():
    try:
        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
    except (AttributeError, OSError):
        return "en"
    return {0x16: "pt", 0x0A: "es"}.get(lid, "en")


def lang():
    """Current UI language (cheap: re-reads config.json only when it changes)."""
    global _lang_cache
    if _forced:
        return _forced
    try:
        mt = os.path.getmtime(CONFIG_FILE)
    except OSError:
        mt = None
    if _lang_cache[0] == mt and _lang_cache[1]:
        return _lang_cache[1]
    code = None
    if mt is not None:
        try:
            code = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("lang")
        except (OSError, ValueError):
            pass
    if code not in LANGS:
        code = windows_lang()
    _lang_cache = (mt, code)
    return code


def set_forced(code):
    global _forced
    _forced = code if code in LANGS else None


def catalog():
    """{"en": {pt: en}, "es": {pt: es}}"""
    global _catalog
    if _catalog is None:
        out = {"en": {}, "es": {}}
        try:
            rows = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        for row in rows:
            if len(row) == 3:
                pt, en, es = row
                if en:
                    out["en"][pt] = en
                if es:
                    out["es"][pt] = es
        _catalog = out
    return _catalog


def table(code=None):
    code = code or lang()
    return {} if code == "pt" else catalog().get(code, {})


def tr(text, **kw):
    """Translate a Portuguese source string (or template) into the current language."""
    if not text:
        return text
    out = table().get(text, text)
    if kw:
        try:
            out = out.format(**kw)
        except (KeyError, IndexError, ValueError):
            out = text.format(**kw)
    return out


def num(x, nd=1):
    """Number with the decimal separator of the current language."""
    s = f"{x:,.{nd}f}"
    if lang() == "en":
        return s
    return s.replace(",", "\0").replace(".", ",").replace("\0", ".")


def gb(b, nd=1):
    return num(b / 1024 ** 3, nd)


def language_name(code=None):
    return {"en": "English", "pt": "Brazilian Portuguese", "es": "Spanish"}[code or lang()]
