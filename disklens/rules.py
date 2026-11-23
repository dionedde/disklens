"""Path rules: map a folder (or special file) to a category plus a human explanation.

Dir patterns use one wildcard set per path segment: ``?`` = one char, ``*`` = any chars
inside a segment (never crosses ``\\``). ``?:`` at the start matches any drive letter.
A folder inherits the rule of its nearest ancestor that matched a rule.
"""
import json
import re

from .config import PKG_DIR

RULES_FILE = PKG_DIR / "rules.json"


def _seg_regex(pattern):
    out = []
    for ch in pattern.lower():
        if ch == "*":
            out.append(r"[^\\]*")
        elif ch == "?":
            out.append(r"[^\\]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


class Rules:
    def __init__(self, data=None):
        if data is None:
            data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
        self.data = data
        self.categories = data["categories"]
        self.rules = []          # index -> rule dict (dirs first, then files)
        self._by_len = {}        # segment count -> [(idx, compiled regex)]
        self._names = {}
        self._exts = {}
        for r in data["dirs"]:
            idx = len(self.rules)
            self.rules.append(r)
            segs = r["pattern"].rstrip("\\").split("\\")
            rx = re.compile("^" + r"\\".join(_seg_regex(s) for s in segs) + "$")
            self._by_len.setdefault(len(segs), []).append((idx, rx))
        for r in data["files"]:
            idx = len(self.rules)
            self.rules.append(r)
            for n in r.get("names", []):
                self._names[n.lower()] = idx
            for e in r.get("exts", []):
                self._exts[e.lower()] = idx

    @property
    def named_files(self):
        return self._names

    def to_json(self):
        return json.dumps(self.data, ensure_ascii=False)

    def match_dir(self, path):
        """Rule index for this exact folder, or None. More specific rules listed later win."""
        p = path.rstrip("\\").lower()
        found = None
        for idx, rx in self._by_len.get(p.count("\\") + 1, ()):
            if rx.match(p):
                found = idx
        return found

    def match_file(self, name_lower, ext):
        idx = self._names.get(name_lower)
        if idx is None and ext:
            idx = self._exts.get(ext)
        return idx

    def category(self, idx):
        if idx is None or idx < 0:
            return "outros"
        return self.rules[idx]["cat"]

    def info(self, idx):
        if idx is None or idx < 0:
            return None
        r = self.rules[idx]
        return {"label": r["label"], "desc": r.get("desc", ""), "safety": r.get("safety", ""),
                "cat": r["cat"]}


EXT_FAMILIES = {
    "Vídeos": "mp4 mkv avi mov wmv flv webm m4v mpg mpeg ts m2ts",
    "Imagens": "jpg jpeg png gif bmp tif tiff webp heic raw cr2 nef arw psd svg ico",
    "Áudio": "mp3 wav flac aac ogg m4a wma opus",
    "Documentos": "pdf doc docx xls xlsx ppt pptx odt ods txt rtf md csv epub",
    "Instaladores e compactados": "zip rar 7z gz tar tgz bz2 xz iso msi msp exe cab esd wim appx msix msixbundle",
    "Discos virtuais": "vhd vhdx vmdk vdi qcow2 avhdx",
    "Programas e bibliotecas": "dll sys ocx mui so pyd lib a node jar",
    "Código-fonte": "py js ts tsx jsx c cpp h hpp cs java go rs rb php html css json xml yml yaml",
    "Bancos de dados e logs": "db sqlite sqlite3 mdb accdb edb ldf mdf log etl evtx ost pst",
    "Dumps e temporários": "dmp tmp temp bak old cache",
    "Modelos de IA": "gguf safetensors pt pth onnx ckpt bin",
    "Jogos (pacotes)": "pak vpk bsa ba2 forge ucas utoc",
}
EXT_TO_FAMILY = {e: fam for fam, exts in EXT_FAMILIES.items() for e in exts.split()}


def family(ext):
    if ext.startswith("#"):
        return "Dumps e temporários" if ext.endswith(".dmp") else "Memória virtual e hibernação"
    return EXT_TO_FAMILY.get(ext, "Outros" if ext else "Sem extensão")
