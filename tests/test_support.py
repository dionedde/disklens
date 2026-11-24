import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from disklens import support  # noqa: E402


def _bch(data):
    v = data << 10
    for i in range(14, 9, -1):
        if v >> i & 1:
            v ^= 0x537 << (i - 10)
    return ((data << 10) | v) ^ 0x5412


class SupportTest(unittest.TestCase):
    def test_qr_matrix_is_square_with_valid_format_bits(self):
        rows = support.PIX_QR
        n = len(rows)
        self.assertTrue(all(len(r) == n for r in rows))
        self.assertEqual((n - 17) % 4, 0)                 # a real QR size (21, 25, 29, ...)
        bit = lambda r, c: rows[r][c]                       # noqa: E731
        first = "".join([bit(8, c) for c in (0, 1, 2, 3, 4, 5, 7, 8)] + [bit(r, 8) for r in (7, 5, 4, 3, 2, 1, 0)])
        second = "".join([bit(n - 1 - i, 8) for i in range(7)] + [bit(8, n - 8 + i) for i in range(8)])
        valid = {_bch(d) for d in range(32)}
        self.assertIn(int(first, 2), valid)
        self.assertEqual(first, second)

    def test_pix_code_checksum(self):
        # BR Code ends with a CRC16-CCITT of everything before it
        code = support.PIX_CODE
        crc = 0xFFFF
        for ch in code[:-4].encode():
            crc ^= ch << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
                crc &= 0xFFFF
        self.assertEqual(code[-4:], f"{crc:04X}")

    def test_svg_and_links(self):
        svg = support.qr_svg()
        self.assertTrue(svg.startswith("<svg") and "<path" in svg)
        self.assertFalse(support.open_link("https://example.com"))   # only known keys open


if __name__ == "__main__":
    unittest.main()
