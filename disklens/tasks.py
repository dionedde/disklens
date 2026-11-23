"""Register / remove the DiskLens agent in Windows Task Scheduler.

The task runs ``pythonw -m disklens.agent`` every 15 minutes (and 5 min after logon), only
while the user is logged on (so it can see full-screen games), with the highest privileges
available (to measure restore points and protected folders) and below-normal priority.
Registering a task with highest privileges needs admin; from a non-admin UI we ask UAC.
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import winapi
from .config import APP_DIR, data_dir

TASK_NAME = r"DiskLens\Agente"


def _pythonw():
    """pythonw.exe of the *base* Python install, never the venv launcher.

    Some venv launchers (seen with Python 3.13.0) relaunch the console ``python.exe``, which
    pops a command-prompt window over whatever the user is doing. The agent only needs the
    standard library, so it runs straight on the base interpreter."""
    candidates = [Path(getattr(sys, "_base_executable", "") or sys.executable).with_name("pythonw.exe"),
                  Path(sys.base_prefix) / "pythonw.exe",
                  Path(sys.executable).with_name("pythonw.exe")]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def task_xml():
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    start = datetime.datetime.now().replace(microsecond=0).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>DiskLens: mede o uso do disco a cada 15 minutos e faz um retrato completo por dia, sempre esperando o computador estar livre.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT5M</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(_pythonw())}</Command>
      <Arguments>-m disklens.agent</Arguments>
      <WorkingDirectory>{escape(str(APP_DIR))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(args, elevate):
    """Run schtasks; through UAC when we are not admin. Returns True if it ran (or UAC accepted)."""
    if winapi.is_admin() or not elevate:
        r = subprocess.run(["schtasks", *args], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode == 0
    params = subprocess.list2cmdline(args)
    rc = winapi.shell32.ShellExecuteW(None, "runas", "schtasks.exe", params, None, 0)
    return rc > 32


def install():
    xml = data_dir() / "disklens_task.xml"
    xml.write_text(task_xml(), encoding="utf-16")
    return _schtasks(["/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F"], elevate=True)


def uninstall():
    return _schtasks(["/Delete", "/TN", TASK_NAME, "/F"], elevate=True)


def is_installed():
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode == 0


def run_agent_now(snapshot=False):
    """Start the agent right away. Through the scheduled task when it exists: it already has
    admin rights (journal, restore points, protected folders) and needs no UAC prompt."""
    if is_installed():
        (data_dir() / "agent_request.json").write_text(json.dumps({"snapshot": bool(snapshot)}), encoding="utf-8")
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode == 0:
            return "task"
    args = [_pythonw(), "-m", "disklens.agent"] + (["--snapshot-now"] if snapshot else [])
    subprocess.Popen(args, cwd=str(APP_DIR), creationflags=subprocess.CREATE_NO_WINDOW)
    return "process"
