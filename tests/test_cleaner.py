"""Safety guards and deletion logic, on throwaway folders only (nothing real is deleted).

Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from disklens import candidates, cleaner  # noqa: E402

WINDIR = os.environ.get("WINDIR", r"C:\Windows")
HOME = os.path.expanduser("~")


class ProtectionTest(unittest.TestCase):
    def blocked(self, p, **kw):
        return cleaner.protection_reason(p, **kw)

    def test_system_places_are_blocked(self):
        for p in ["C:\\", WINDIR, WINDIR + r"\System32", WINDIR + r"\System32\kernel32.dll",
                  os.environ["ProgramFiles"], os.environ["ProgramData"], r"C:\Users", HOME,
                  HOME + r"\Documents", HOME + r"\Downloads", HOME + r"\AppData", sys.base_prefix,
                  str(ROOT), r"C:\pagefile.sys"]:
            if os.path.lexists(p):
                self.assertIsNotNone(self.blocked(p), p)

    def test_tool_areas_are_blocked(self):
        sd = WINDIR + r"\SoftwareDistribution"
        if os.path.exists(sd):
            self.assertIn("ferramenta", self.blocked(sd))

    def test_safe_cache_is_allowed_even_under_windows(self):
        t = WINDIR + r"\Temp"
        if os.path.isdir(t):
            self.assertIsNone(self.blocked(t, for_contents=True))
        self.assertIsNone(self.blocked(os.path.join(HOME, r"AppData\Local\Temp"), for_contents=True))

    def test_missing_path(self):
        self.assertEqual(self.blocked(r"C:\nao\existe\x.bin"), "não existe mais")

    def test_installed_program_folders_are_blocked(self):
        dirs = list(cleaner.program_dirs())
        self.assertTrue(dirs)
        for loc in dirs:
            self.assertIsNotNone(self.blocked(loc), loc)

    def test_uninstall_string_parsing(self):
        f = candidates.uninstall_command
        self.assertEqual(f('"C:\\Program Files\\App\\unins000.exe" /S'), ("C:\\Program Files\\App\\unins000.exe", "/S"))
        self.assertEqual(f("MsiExec.exe /I{1234-ABCD}"), ("MsiExec.exe", "/X{1234-ABCD}"))
        self.assertEqual(f("C:\\Tools\\uninstall.exe --remove"), ("C:\\Tools\\uninstall.exe", "--remove"))


class CleanContentsTest(unittest.TestCase):
    """clean_contents on a fake cache inside %TEMP% (rule 'Temporários do usuário' = seguro)."""

    def setUp(self):
        base = os.path.join(HOME, r"AppData\Local\Temp")
        self.cache = tempfile.mkdtemp(prefix="disklens_cache_", dir=base)
        self.outside = tempfile.mkdtemp(prefix="disklens_outside_", dir=base)
        for i in range(3):
            Path(self.cache, f"f{i}.bin").write_bytes(b"x" * 1000)
        sub = Path(self.cache, "sub")
        sub.mkdir()
        Path(sub, "g.bin").write_bytes(b"y" * 500)
        ro = Path(self.cache, "readonly.bin")
        ro.write_bytes(b"z" * 10)
        os.chmod(ro, 0o444)
        Path(self.outside, "precious.txt").write_text("não apague")
        # a junction inside the cache pointing OUTSIDE it: must be left alone
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(self.cache, "link"), self.outside],
                       capture_output=True)

    def tearDown(self):
        link = os.path.join(self.cache, "link")
        if os.path.lexists(link):
            os.rmdir(link)                                     # removes the junction, not its target
        for d in (self.cache, self.outside):
            shutil.rmtree(d, onerror=lambda f, p, e: (os.chmod(p, 0o666), f(p)))

    def test_empties_cache_but_never_follows_junctions(self):
        freed, n, skipped = cleaner.clean_contents(self.cache)
        self.assertEqual(n, 5)
        self.assertEqual(freed, 3 * 1000 + 500 + 10)
        self.assertTrue(os.path.isdir(self.cache))              # the folder itself stays
        self.assertTrue(Path(self.outside, "precious.txt").exists())
        self.assertFalse(Path(self.cache, "sub").exists())

    def test_refuses_non_safe_folder(self):
        with self.assertRaises(PermissionError):
            cleaner.clean_contents(os.path.join(HOME, "Documents"))


if __name__ == "__main__":
    unittest.main()
