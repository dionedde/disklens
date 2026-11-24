"""Build the Windows installer: dist/DiskLens-Setup-<version>.exe

    python tools/build_installer.py [--embed path\\to\\python-3.13.x-embed-amd64.zip] [--no-iscc]

What it does:
  1. build/stage/runtime: the official embeddable Python (python.org) with pywebview and its
     dependencies copied from the project's .venv (same Python minor version, so the compiled
     parts match). python313._pth puts the app folder on sys.path.
  2. build/stage: the app itself (disklens/, launch.pyw, disklens_mcp.py, licence, READMEs) and
     DiskLens.ico, everything precompiled to .pyc.
  3. Inno Setup (ISCC.exe) packs build/stage with installer/DiskLens.iss.

The embeddable zip is not downloaded here: get it from https://www.python.org/downloads/windows/
("Windows embeddable package (64-bit)", same 3.x as .venv), check its SHA-256 on the release
page, and put it in build/cache/ (or pass --embed).
"""
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
STAGE = BUILD / "stage"
CACHE = BUILD / "cache"
DIST = ROOT / "dist"
VENV_SITE = ROOT / ".venv" / "Lib" / "site-packages"
APP_FILES = ["launch.pyw", "disklens_mcp.py", "LICENSE", "README.md", "README.pt-BR.md", "README.es.md"]
SKIP_PACKAGES = re.compile(r"^(pip|setuptools|wheel|_distutils_hack|distutils-precedence\.pth)([-_.].*)?$", re.I)

sys.path.insert(0, str(ROOT))
from disklens import __version__  # noqa: E402


def find_embed(arg):
    if arg:
        return Path(arg)
    minor = "%d.%d" % sys.version_info[:2]
    zips = sorted(CACHE.glob(f"python-{minor}.*-embed-amd64.zip"),
                  key=lambda p: [int(x) for x in re.findall(r"\d+", p.name)[:3]])
    if not zips:
        raise SystemExit(f"Put python-{minor}.x-embed-amd64.zip (python.org) in {CACHE}")
    return zips[-1]


def find_iscc():
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        for sub in ("Programs\\Inno Setup 6", "Inno Setup 6"):
            p = Path(base) / sub / "ISCC.exe"
            if p.exists():
                return p
    p = shutil.which("ISCC")
    if p:
        return Path(p)
    raise SystemExit("Inno Setup 6 not found (winget install JRSoftware.InnoSetup)")


def ignore(_dir, names):
    return [n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))]


def stage_runtime(embed):
    rt = STAGE / "runtime"
    with zipfile.ZipFile(embed) as z:
        z.extractall(rt)
    pth = next(rt.glob("python3*._pth"))
    stdlib = next(rt.glob("python3*.zip")).name
    # app folder (..) first so "import disklens" works from any working directory
    pth.write_text("\n".join([stdlib, ".", "..", "Lib\\site-packages", "import site", ""]), encoding="ascii")
    site = rt / "Lib" / "site-packages"
    site.mkdir(parents=True)
    for item in VENV_SITE.iterdir():
        if SKIP_PACKAGES.match(item.name) or item.name == "__pycache__":
            continue
        if item.is_dir():
            shutil.copytree(item, site / item.name, ignore=ignore)
        else:
            shutil.copy2(item, site / item.name)
    return rt


def stage_app():
    shutil.copytree(ROOT / "disklens", STAGE / "disklens", ignore=ignore)
    for f in APP_FILES:
        shutil.copy2(ROOT / f, STAGE / f)
    from disklens import icon
    (STAGE / "DiskLens.ico").write_bytes(icon.ico(0.62, sizes=(16, 20, 24, 32, 40, 48, 64, 128, 256)))


def check_runtime(rt):
    """Import everything with the shipped Python, from outside the app folder."""
    code = ("import disklens.server, disklens.tray, disklens.agent, disklens.scanner, disklens.mcp_server, "
            "disklens.uninstall, webview, clr_loader, sqlite3, ctypes, ssl; "
            "from disklens.tasks import _pythonw; print(_pythonw())")
    r = subprocess.run([str(rt / "python.exe"), "-c", code], capture_output=True, text=True, cwd=str(BUILD))
    if r.returncode != 0:
        raise SystemExit("runtime check failed:\n" + r.stderr)
    expected = str(rt / "pythonw.exe")
    if r.stdout.strip().lower() != expected.lower():
        raise SystemExit(f"_pythonw() points to {r.stdout.strip()}, expected {expected}")


def precompile(rt):
    py = str(rt / "python.exe")
    for target in (STAGE / "disklens", rt / "Lib" / "site-packages"):
        subprocess.run([py, "-m", "compileall", "-q", "-j", "0", str(target)], check=True)


def main():
    args = sys.argv[1:]
    embed = find_embed(args[args.index("--embed") + 1] if "--embed" in args else None)
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    print("runtime:", embed.name)
    rt = stage_runtime(embed)
    stage_app()
    check_runtime(rt)
    precompile(rt)
    size = sum(f.stat().st_size for f in STAGE.rglob("*") if f.is_file())
    print(f"staged {size / 1e6:.1f} MB in {STAGE}")
    if "--no-iscc" in args:
        return
    DIST.mkdir(exist_ok=True)
    iscc = find_iscc()
    subprocess.run([str(iscc), "/Q", f"/DAppVersion={__version__}", f"/DStageDir={STAGE}", f"/DOutDir={DIST}",
                    str(ROOT / "installer" / "DiskLens.iss")], check=True)
    out = DIST / f"DiskLens-Setup-{__version__}.exe"
    print(f"installer: {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
