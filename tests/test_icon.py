"""Icon drawing (no Windows UI needed)."""
import struct
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from disklens import icon  # noqa: E402


class IconTest(unittest.TestCase):
    def test_ico_structure(self):
        data = icon.ico(0.5, sizes=(16, 32))
        _, kind, count = struct.unpack_from("<HHH", data)
        self.assertEqual((kind, count), (1, 2))
        for i, n in enumerate((16, 32)):
            w, h, _, _, planes, bpp, size, off = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
            self.assertEqual((w, h, planes, bpp), (n, n, 1, 32))
            self.assertEqual(off + size <= len(data), True)

    def test_colours_follow_usage(self):
        self.assertEqual(icon.color_for(0.5), icon.BLUE)
        self.assertEqual(icon.color_for(0.9), icon.ORANGE)
        self.assertEqual(icon.color_for(0.97), icon.RED)

    def test_png_and_transparent_corners(self):
        p = icon.png(32, 0.7)
        self.assertTrue(p.startswith(b"\x89PNG"))
        px = icon.pixels(32, 0.7)
        self.assertEqual(px[3], 0)                       # top-left corner transparent
        self.assertGreater(px[(16 * 32 + 13) * 4 + 3], 200)   # inside the lens opaque


if __name__ == "__main__":
    unittest.main()
