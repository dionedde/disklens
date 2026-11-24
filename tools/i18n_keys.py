"""List every translatable Portuguese string in DiskLens and check the catalog.

    python tools/i18n_keys.py            # report missing / unused translations
    python tools/i18n_keys.py --json     # print the keys as JSON (for translators)

Sources: t('...') calls and label constants in web/app.js, data-t attributes in web/index.html,
tr("...") calls in the Python modules, the templates in probes.py, rules.json (categories,
safety levels, rule labels and descriptions) and a few tables translated at run time.
"""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "disklens"
sys.path.insert(0, str(ROOT))

JS_CONSTS = ("AGE", "EV", "HIDLABEL", "RANGES", "ACT_RANGES", "KIND", "VERDICT", "GROUP", "ACTION_LABEL")


def _js_unescape(s):
    return s.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def js_keys():
    src = (PKG / "web" / "app.js").read_text(encoding="utf-8")
    keys = [_js_unescape(m) for m in re.findall(r"\bt\('((?:[^'\\]|\\.)*)'", src)]
    for name in JS_CONSTS:
        m = re.search(r"const %s = ([\[{].*?[\]}]);\n" % name, src, re.S)
        if not m:
            raise SystemExit(f"constant {name} not found in app.js")
        for s in re.findall(r"'((?:[^'\\]|\\.)*)'", m.group(1)):
            if re.search(r"[A-Za-zÀ-ú]{2}", s) and not s.startswith(("#", "path://")) and s not in (
                    "diamond", "triangle", "pin", "circle", "ok", "warn", "keep", "rev", "Windows Update"):
                keys.append(_js_unescape(s))
    html = (PKG / "web" / "index.html").read_text(encoding="utf-8")
    keys += re.findall(r'data-t="([^"]+)"', html)
    return keys


def py_keys():
    keys = []
    for f in sorted(PKG.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                keys.append(node.args[0].value)
            if f.name == "probes.py" and isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 2:
                first = node.elts[0]
                options = [first.body, first.orelse] if isinstance(first, ast.IfExp) else [first]
                for o in options:
                    if isinstance(o, ast.Constant) and isinstance(o.value, str) and re.search(r"[A-Za-zà-ú]{2}", o.value):
                        keys.append(o.value)
    # probes: the NTFS text is built from two pieces
    from disklens import probes  # noqa: F401
    src = (PKG / "probes.py").read_text(encoding="utf-8")
    m = re.search(r'desc = \("(.*?)"\)\n', src, re.S)
    ntfs = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(0)))
    keys.append(ntfs)
    keys.append(ntfs[:-1] + ". Valor estimado: o tamanho exato só pode ser lido como administrador.")
    # tables translated at run time
    from disklens import ai, candidates, cleaner, diff, rules, winapi
    keys += [v["label"] for v in ai.PROVIDERS.values()]
    keys += [v for k in ("label", "desc") for v in (t[k] for t in cleaner.TOOLS.values())]
    keys += ["pasta do Windows", "programas instalados", "dados compartilhados de programas"]
    keys += list(candidates.DEV_DIRS.values())
    keys += [diff.METHOD_NOTE]
    keys += list(rules.EXT_FAMILIES) + ["Outros", "Sem extensão"]
    keys += list(winapi.BUSY_STATES.values())
    return keys


def rule_keys():
    data = json.loads((PKG / "rules.json").read_text(encoding="utf-8"))
    keys = [c["label"] for c in data["categories"].values()] + list(data["safety"].values())
    for r in data["dirs"] + data["files"]:
        keys.append(r["label"])
        if r.get("desc"):
            keys.append(r["desc"])
    return keys


def all_keys():
    seen, out = set(), []
    for k in js_keys() + py_keys() + rule_keys():
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def placeholders(s):
    return sorted(set(re.findall(r"\{(\w+)\}", s)))


def check():
    rows = json.loads((PKG / "locales.json").read_text(encoding="utf-8"))
    cat = {r[0]: r for r in rows}
    keys = all_keys()
    missing = [k for k in keys if k not in cat or not cat[k][1] or not cat[k][2]]
    unused = [k for k in cat if k not in set(keys)]
    bad = [(k, r[1], r[2]) for k, r in cat.items()
           if placeholders(k) != placeholders(r[1]) or placeholders(k) != placeholders(r[2])]
    tags = [k for k, r in cat.items()
            if any(sorted(re.findall(r"</?\w+", k)) != sorted(re.findall(r"</?\w+", x)) for x in r[1:])]
    return keys, missing, unused, bad, tags


if __name__ == "__main__":
    if "--json" in sys.argv:
        print(json.dumps(all_keys(), ensure_ascii=False, indent=0))
        sys.exit(0)
    keys, missing, unused, bad, tags = check()
    print(f"{len(keys)} keys, {len(missing)} missing, {len(unused)} unused, {len(bad)} placeholder mismatches, "
          f"{len(tags)} html tag mismatches")
    for k in missing[:50]:
        print("MISSING:", k[:100])
    for k in unused[:20]:
        print("UNUSED:", k[:100])
    for k in bad[:20]:
        print("PLACEHOLDERS:", k)
    for k in tags[:20]:
        print("TAGS:", k[:100])
    sys.exit(1 if missing or bad or tags else 0)
