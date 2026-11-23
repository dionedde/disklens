"""Locations of app data. Data goes to the non-system fixed drive with most free space
(to avoid filling the system drive), chosen once and remembered in config.json."""
import json
import os
import shutil
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PKG_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"

DEFAULTS = {
    "data_dir": None,
    "cpu_limit": 60,       # % of CPU used by other programs that pauses the scan
    "mem_limit": 90,       # % memory load that pauses the scan
    "workers": 2,          # scanning threads (kept low on purpose)
    "snapshot_hours": 24,  # automatic snapshot interval
    "volumes": None,       # drives with automatic snapshots (None = system drive)
    "tray": False,         # notification-area icon (turned on in the Timeline tab)
}


def load():
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if not cfg.get("data_dir"):
        cfg["data_dir"] = str(_pick_data_dir())
        save(cfg)
    return cfg


def save(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _pick_data_dir():
    from . import winapi
    sysdrive = winapi.system_drive()
    best = None
    for v in winapi.fixed_volumes():
        if v["volume"].upper() == sysdrive:
            continue
        if best is None or v["free"] > best["free"]:
            best = v
    if best and best["free"] > 5 * 1024 ** 3:
        return Path(best["volume"] + "\\") / "DiskLens" / "data"
    return APP_DIR / "data"


def data_dir():
    d = Path(os.environ.get("DISKLENS_DATA") or load()["data_dir"])   # env override for tests
    d.mkdir(parents=True, exist_ok=True)
    return d
