import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disklens import uninstall  # noqa: E402


class PurgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = self.root / "app"
        self.app.mkdir()
        self.cfg = self.app / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def purge(self):
        with mock.patch.object(uninstall, "APP_DIR", self.app), mock.patch.object(uninstall, "CONFIG_FILE", self.cfg):
            uninstall.purge()

    def test_removes_data_folder_and_config(self):
        data = self.root / "DiskLens" / "data"
        data.mkdir(parents=True)
        (data / "index.db").write_bytes(b"x")
        self.cfg.write_text(json.dumps({"data_dir": str(data)}), encoding="utf-8")
        self.purge()
        self.assertFalse(data.exists())
        self.assertFalse((self.root / "DiskLens").exists())      # empty parent removed too
        self.assertFalse(self.cfg.exists())

    def test_keeps_a_folder_that_is_not_disklens_data(self):
        other = self.root / "Documents"
        other.mkdir()
        (other / "important.txt").write_text("keep")
        self.cfg.write_text(json.dumps({"data_dir": str(other)}), encoding="utf-8")
        self.purge()
        self.assertTrue((other / "important.txt").exists())

    def test_only_this_copy(self):
        with mock.patch.object(uninstall, "APP_DIR", self.app):
            self.assertTrue(uninstall._here(f'"{self.app}\\runtime\\pythonw.exe" "{self.app}\\launch.pyw" --tray'))
            self.assertFalse(uninstall._here(r'"C:\Python313\pythonw.exe" "D:\other\launch.pyw" --tray'))


if __name__ == "__main__":
    unittest.main()
