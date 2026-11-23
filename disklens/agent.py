"""Background agent, started every 15 min by Windows Task Scheduler (``python -m disklens.agent``).

Each run is short and cheap:
  1. sample used/free space of every fixed drive (+ pagefile, hiberfil, shadow copies);
  2. import new Windows events that explain disk growth (updates, restore points,
     installs/uninstalls, crashes);
  3. if a snapshot is due, check that the PC is free first (CPU, full-screen game/app,
     memory, battery). Busy -> record "deferred" and retry later with backoff
     (30 min, 1 h, 2 h, max 4 h). Free -> run the scan (which still pauses itself if the
     PC gets busy halfway through).
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import time
import traceback

from . import db, winapi
from .config import data_dir, load as load_config

DAY = 86400
GB = 1024 ** 3
EVENT_KINDS = ["update", "defender", "store", "restore", "install", "uninstall", "crash"]

_EVENTS_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$since = [DateTimeOffset]::FromUnixTimeSeconds(%d).LocalDateTime
$out = New-Object System.Collections.ArrayList
function ts($d) { ([DateTimeOffset]$d).ToUnixTimeSeconds() }
function grab($filter, $kind, $titleFn) {
  try {
    Get-WinEvent -FilterHashtable $filter -MaxEvents 500 -ErrorAction Stop | ForEach-Object {
      [void]$out.Add(@{ ts = ts $_.TimeCreated; kind = $kind; title = (& $titleFn $_) })
    }
  } catch {}
}
grab @{LogName='System'; ProviderName='Microsoft-Windows-WindowsUpdateClient'; Id=19; StartTime=$since} 'update' { param($e) $e.Properties[0].Value }
grab @{LogName='Application'; ProviderName='MsiInstaller'; Id=1033; StartTime=$since} 'install' { param($e) $e.Properties[0].Value }
grab @{LogName='Application'; ProviderName='MsiInstaller'; Id=1034; StartTime=$since} 'uninstall' { param($e) $e.Properties[0].Value }
grab @{LogName='Application'; ProviderName='SystemRestore'; Id=8194; StartTime=$since} 'restore' { param($e) ($e.Message -split "`n")[0] }
grab @{LogName='System'; ProviderName='Microsoft-Windows-WER-SystemErrorReporting'; Id=1001; StartTime=$since} 'crash' { param($e) 'Tela azul (BugCheck)' }
grab @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41; StartTime=$since} 'crash' { param($e) 'Desligamento inesperado' }
ConvertTo-Json -InputObject @($out) -Depth 3 -Compress
"""


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(data_dir() / "agent.log", "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# ------------------------------------------------------------------ state
def state_path():
    return data_dir() / "agent_state.json"


def load_state():
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"volumes": {}}


def save_state(st):
    from .scanner import replace_retry
    p = state_path()
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        if not replace_retry(tmp, p):
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        log(f"estado não salvo: {exc}")


# ------------------------------------------------------------------ 1. samples
def _root_file_sizes(root):
    out = {}
    try:
        with os.scandir(root) as it:
            for e in it:
                n = e.name.lower()
                if n in ("pagefile.sys", "hiberfil.sys"):
                    try:
                        out[n] = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
    except OSError:
        pass
    return out


def _shadow_used():
    """{drive: bytes} of shadow copy storage (admin only)."""
    if not winapi.is_admin():
        return {}
    from .probes import _powershell
    ps = _powershell()
    return {(s.get("drive") or "").upper(): s.get("used") for s in ps.get("shadow_storage") or []}


def sample():
    ts = time.time()
    sysd = winapi.system_drive()
    shadow = _shadow_used()
    n = 0
    for v in winapi.fixed_volumes():
        vol = v["volume"].upper()
        pf = hb = None
        if vol == sysd:
            sizes = _root_file_sizes(vol + "\\")
            pf, hb = sizes.get("pagefile.sys"), sizes.get("hiberfil.sys")
        db.add_sample(ts, vol, v["total"], v["used"], v["free"], pf, hb, shadow.get(vol))
        n += 1
    return n


# ------------------------------------------------------------------ 2. events
_STORE_ID = re.compile(r"^[0-9A-Z]{12}-")


def classify(kind, title):
    """Separate noisy daily updates (Defender definitions, Store apps) from real ones."""
    if kind == "update":
        t = title.lower()
        if "kb2267602" in t or "intelig" in t and "defender" in t or "security intelligence" in t:
            return "defender"
        if _STORE_ID.match(title):
            return "store"
    return kind


def import_events():
    since = db.last_event_ts(EVENT_KINDS)
    since = int(since + 1) if since else int(time.time() - 30 * DAY)   # first run: last 30 days
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _EVENTS_PS % since],
                           capture_output=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        evs = json.loads(r.stdout.decode("utf-8", "replace").strip() or "[]")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log(f"eventos: falha {exc}")
        return 0
    sysd = winapi.system_drive()
    if isinstance(evs, dict):
        evs = [evs]
    for e in evs:
        title = str(e.get("title") or "").strip()[:300]
        db.add_event(e["ts"], sysd, classify(e["kind"], title), title)
    return len(evs)


# ------------------------------------------------------------------ 3. load pre-check
class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


def on_battery():
    s = SYSTEM_POWER_STATUS()
    if not winapi.k32.GetSystemPowerStatus(ctypes.byref(s)):
        return False
    return s.ACLineStatus == 0 and not s.BatteryFlag & 128


def machine_free(cfg, seconds=60, interval=5):
    """Watch the PC for `seconds`. Returns (True, None) or (False, reason)."""
    if on_battery():
        return False, "Notebook funcionando na bateria"
    limit = cfg["cpu_limit"]
    samples = []
    prev = winapi.system_times()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(interval)
        busy_ui = winapi.user_busy_reason()
        if busy_ui:
            return False, busy_ui
        idle, kern, user = winapi.system_times()
        total = (kern - prev[1]) + (user - prev[2])
        busy = total - (idle - prev[0])
        prev = (idle, kern, user)
        if total > 0:
            samples.append(busy / total * 100)
    mem = winapi.memory_load()
    if mem >= cfg["mem_limit"]:
        return False, f"Memória quase cheia ({mem}%)"
    if samples:
        avg = sum(samples) / len(samples)
        over = sum(1 for s in samples if s > limit) / len(samples)
        if avg > limit or over >= 0.5:
            return False, f"Processador ocupado ({avg:.0f}% em média por {seconds}s)"
    return True, None


def backoff_seconds(deferrals):
    return min(4 * 3600, 1800 * 2 ** max(0, deferrals - 1))


# ------------------------------------------------------------------ main
def snapshot_due(vol, cfg, vstate, now):
    last = next((s for s in db.list_snapshots() if s["volume"] == vol and s["status"] == "done"), None)
    if last and now - (last["finished"] or 0) < cfg["snapshot_hours"] * 3600:
        return False
    return now >= vstate.get("next_attempt", 0)


def run(force_snapshot=False, precheck_seconds=60):
    cfg = load_config()
    st = load_state()
    now = time.time()
    st["last_run"] = now
    try:
        n = sample()
        log(f"amostra: {n} discos")
    except Exception:
        log("amostra falhou:\n" + traceback.format_exc())
    try:
        n = import_events()
        if n:
            log(f"eventos importados: {n}")
    except Exception:
        log("eventos falharam:\n" + traceback.format_exc())
    st.setdefault("volumes", {})
    if winapi.is_admin():
        from . import usn
        winapi.enable_privilege("SeBackupPrivilege")
        for vol in cfg.get("volumes") or [winapi.system_drive()]:
            try:
                r = usn.collect(vol)
                st.setdefault("usn", {})[vol.upper()[:2]] = r
                log(f"{vol}: journal NTFS: {r['records']} registros, {r['files']} arquivos, "
                    f"{r['changes']} mudanças em {r['seconds']}s" + (" (primeira leitura)" if r["first_run"] else ""))
            except Exception as exc:
                st.setdefault("usn", {})[vol.upper()[:2]] = {"error": str(exc)}
                log(f"{vol}: journal NTFS falhou: {exc}")
    save_state(st)

    from .scanner import is_scan_running, main as scan_main
    for vol in cfg.get("volumes") or [winapi.system_drive()]:
        vol = vol.upper()[:2]
        vs = st["volumes"].setdefault(vol, {})
        if not force_snapshot and not snapshot_due(vol, cfg, vs, now):
            continue
        if is_scan_running():
            log(f"{vol}: scan já em andamento")
            continue
        ok, reason = (True, None) if force_snapshot else machine_free(cfg, precheck_seconds)
        if not ok:
            vs["deferrals"] = vs.get("deferrals", 0) + 1
            vs["next_attempt"] = time.time() + backoff_seconds(vs["deferrals"])
            vs.setdefault("pending_since", now)
            vs["last_reason"] = reason
            db.add_event(time.time(), vol, "deferred", "Snapshot adiado", reason)
            log(f"{vol}: snapshot adiado ({reason}); nova tentativa em "
                f"{backoff_seconds(vs['deferrals']) // 60} min")
            save_state(st)
            continue
        log(f"{vol}: iniciando snapshot")
        vs["running_since"] = time.time()
        save_state(st)
        code = scan_main(["--volume", vol])
        vs.pop("running_since", None)
        if code == 0:
            for k in ("deferrals", "next_attempt", "pending_since", "last_reason"):
                vs.pop(k, None)
            vs["last_snapshot"] = time.time()
            removed = db.prune(vol)
            log(f"{vol}: snapshot concluído" + (f"; {removed} snapshots antigos removidos" if removed else ""))
        else:
            vs["next_attempt"] = time.time() + 1800
            log(f"{vol}: snapshot terminou com código {code}")
        save_state(st)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="DiskLens agent")
    ap.add_argument("--snapshot-now", action="store_true", help="faz o snapshot já, sem esperar o intervalo")
    ap.add_argument("--precheck", type=int, default=60, help="segundos observando a carga antes do snapshot")
    args = ap.parse_args(argv)
    if sys.stderr is None or "pythonw" in sys.executable.lower():
        f = open(data_dir() / "agent.log", "a", encoding="utf-8")
        sys.stdout = sys.stderr = f
    snapshot_now = args.snapshot_now
    req = data_dir() / "agent_request.json"      # "Snapshot agora" sent through the scheduled task
    if req.exists():
        try:
            snapshot_now = snapshot_now or bool(json.loads(req.read_text(encoding="utf-8")).get("snapshot"))
        except (OSError, ValueError):
            pass
        req.unlink(missing_ok=True)
    try:
        return run(snapshot_now, args.precheck)
    except Exception:
        log("erro:\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
