"""Start DiskLens with no console.

  launch.pyw            open the panel (through the tray icon when it is turned on)
  launch.pyw --admin    the same, as administrator (asks UAC; used by the Start menu shortcut)
  launch.pyw --tray     tray icon only (used by "Iniciar com o Windows")
  launch.pyw --panel    the panel window itself (started by the tray)

From a clone, run it with the base Python's pythonw.exe (see DiskLens.bat): the venv's libraries
(pywebview) are added here, because some venv pythonw launchers start the console python.exe.
The Windows installer ships its own Python in runtime\\ and runs runtime\\pythonw.exe launch.pyw.
"""
import os
import site
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
site.addsitedir(os.path.join(HERE, ".venv", "Lib", "site-packages"))
sys.path.insert(0, HERE)
os.chdir(HERE)

if "--admin" in sys.argv:
    from disklens import winapi
    if not winapi.is_admin():
        winapi.relaunch_as_admin(sys.executable, f'"{os.path.abspath(__file__)}"', HERE)
        sys.exit(0)

if "--panel" in sys.argv:
    from disklens.app import main
    main(replace="--replace" in sys.argv)
else:
    from disklens.tray import main
    main(open_panel="--tray" not in sys.argv)
