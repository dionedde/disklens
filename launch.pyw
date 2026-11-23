"""Start DiskLens with no console.

  launch.pyw            open the panel (through the tray icon when it is turned on)
  launch.pyw --tray     tray icon only (used by "Iniciar com o Windows")
  launch.pyw --panel    the panel window itself (started by the tray)

Run with the base Python's pythonw.exe (see DiskLens.bat). The venv's libraries (pywebview)
are added here, because some venv pythonw launchers start the console python.exe instead.
"""
import os
import site
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
site.addsitedir(os.path.join(HERE, ".venv", "Lib", "site-packages"))
sys.path.insert(0, HERE)
os.chdir(HERE)

if "--panel" in sys.argv:
    from disklens.app import main
    main(replace="--replace" in sys.argv)
else:
    from disklens.tray import main
    main(open_panel="--tray" not in sys.argv)
