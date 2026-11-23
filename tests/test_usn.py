"""Tests for the NTFS journal logic that do not need admin rights.

Run: .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="disklens_test_")

from disklens import usn  # noqa: E402

MB = 1024 * 1024


def setUpModule():
    os.environ["DISKLENS_DATA"] = TMP       # never touch the real data folder


def tearDownModule():
    import gc
    os.environ.pop("DISKLENS_DATA", None)
    gc.collect()                            # release SQLite handles so Windows lets us delete the files
    shutil.rmtree(TMP, ignore_errors=True)


def record(frn, parent, usn_no, ts, reason, name, attrs=0x20):
    nb = name.encode("utf-16-le")
    reclen = (60 + len(nb) + 7) // 8 * 8
    ft = int((ts + usn.EPOCH_DIFF) * 1e7)
    head = struct.pack("<IHHQQqqIIIIHH", reclen, 2, 0, frn, parent, usn_no, ft, reason, 0, 0, attrs, len(nb), 60)
    return (head + nb).ljust(reclen, b"\0")


class FakeVolume:
    """Stand-in for usn.Volume: a scripted journal and a fake file system."""

    def __init__(self, records, live, dirs):
        self.records, self.live, self.dirs = records, live, dirs

    def query(self):
        return 77, 0, len(self.records)

    def read(self, start, end, jid):
        for i, r in enumerate(self.records):
            if start <= i < end:
                yield r

    def stat(self, frn):
        return self.live.get(frn)

    def dir_path(self, frn):
        return self.dirs.get(frn)

    def close(self):
        pass


class ParseTest(unittest.TestCase):
    def test_parse_v2_records(self):
        now = time.time()
        raw = record(10, 5, 100, now, usn.R_FILE_CREATE, "relatório.mp4") + record(11, 5, 101, now, usn.R_DATA_EXTEND, "a.log")
        recs = list(usn.parse_records(raw))
        self.assertEqual(len(recs), 2)
        frn, parent, u, ts, reason, attrs, name = recs[0]
        self.assertEqual((frn, parent, u, reason, name), (10, 5, 100, usn.R_FILE_CREATE, "relatório.mp4"))
        self.assertAlmostEqual(ts, now, delta=0.001)
        self.assertEqual(recs[1][6], "a.log")


class CollectTest(unittest.TestCase):
    def test_lifecycle(self):
        t = time.time() - 3600
        recs = [
            (1, 9, 0, t, usn.R_FILE_CREATE, 0x20, "video.mp4"),                       # new big file
            (1, 9, 1, t + 60, usn.R_DATA_EXTEND, 0x20, "video.mp4"),
            (2, 9, 2, t + 120, usn.R_FILE_CREATE, 0x20, "tmp1.tmp"),                    # temp churn
            (2, 9, 3, t + 130, usn.R_FILE_DELETE | usn.R_CLOSE, 0x20, "tmp1.tmp"),
            (3, 9, 4, t + 200, usn.R_FILE_CREATE, 0x20, "small.txt"),                   # small -> per folder
            (4, 9, 5, t + 210, usn.R_FILE_CREATE, 0x10, "subdir"),                      # directory: ignored
            (5, 9, 6, t + 220, 0x00008000, 0x20, "attrs-only.txt"),                     # BASIC_INFO only
        ]
        live = {1: (r"C:\Users\u\Videos\video.mp4", 500 * MB), 3: (r"C:\Users\u\Videos\small.txt", 4096)}
        r = usn.collect("C:", vol=FakeVolume(recs, live, {9: r"C:\Users\u\Videos"}))
        self.assertTrue(r["first_run"])
        self.assertEqual(r["churn"], 1)
        a = usn.activity("C:", t - 10, time.time() + 10)
        self.assertTrue(a["available"])
        big = {f["path"]: f for f in a["grow"] if not f["is_dir_agg"]}
        self.assertEqual(big[r"C:\Users\u\Videos\video.mp4"]["delta"], 500 * MB)
        self.assertEqual(big[r"C:\Users\u\Videos\video.mp4"]["kind"], "new")
        agg = [f for f in a["grow"] if f["is_dir_agg"]]
        self.assertEqual(agg[0]["path"], r"C:\Users\u\Videos")
        self.assertEqual(agg[0]["delta"], 4096)
        self.assertEqual(a["totals"]["grow"], 500 * MB + 4096)

        # second run: the video grows by 300 MB, then is deleted in a third run
        t2 = time.time() - 600
        recs2 = recs + [(1, 9, 7, t2, usn.R_DATA_EXTEND, 0x20, "video.mp4")]
        live[1] = (r"C:\Users\u\Videos\video.mp4", 800 * MB)
        r2 = usn.collect("C:", vol=FakeVolume(recs2, live, {9: r"C:\Users\u\Videos"}))
        self.assertFalse(r2["first_run"])
        self.assertEqual(r2["records"], 1)          # only the new record was read
        a2 = usn.activity("C:", t2 - 1, time.time() + 10)
        self.assertEqual(a2["grow"][0]["delta"], 300 * MB)
        self.assertEqual(a2["grow"][0]["kind"], "grow")

        t3 = time.time() - 60
        recs3 = recs2 + [(1, 9, 8, t3, usn.R_FILE_DELETE | usn.R_CLOSE, 0x20, "video.mp4")]
        del live[1]
        usn.collect("C:", vol=FakeVolume(recs3, live, {9: r"C:\Users\u\Videos"}))
        a3 = usn.activity("C:", t3 - 1, time.time() + 10)
        self.assertEqual(a3["shrink"][0]["delta"], -800 * MB)
        self.assertEqual(a3["shrink"][0]["kind"], "deleted")
        # over the whole period the video nets to zero; only the small file remains
        whole = usn.activity("C:", t - 10, time.time() + 10)
        self.assertEqual(whole["totals"]["grow"] + whole["totals"]["shrink"], 4096)

    def test_journal_reset_records_gap(self):
        t = time.time()
        vol = FakeVolume([(1, 9, 0, t, usn.R_FILE_CREATE, 0x20, "x.bin")], {1: (r"D:\x.bin", 2 * MB)}, {})
        usn.collect("D:", vol=vol)
        vol.query = lambda: (78, 0, 1)              # new journal id -> history gap
        r = usn.collect("D:", vol=vol)
        self.assertTrue(r["gap"])


if __name__ == "__main__":
    unittest.main()
