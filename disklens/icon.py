"""DiskLens icon, drawn in code (no image files, no Pillow).

A magnifying glass whose lens is a pie chart of the disk: the coloured slice is the used space
(blue; orange above 85%; red above 95%), the light part is the free space. A dark outline keeps
it readable on light and dark taskbars.
"""
import math
import struct
import zlib

OUTLINE = (15, 23, 42)
RIM = (226, 232, 240)
HANDLE = (100, 116, 139)
FREE = (241, 245, 249)
BLUE, ORANGE, RED = (37, 99, 235), (245, 158, 11), (220, 38, 38)

# geometry in unit coordinates (0..1, y down)
CX, CY = 0.40, 0.40          # lens centre
R_OUT, R_IN = 0.37, 0.29     # rim
H0, H1, HW = (0.67, 0.67), (0.91, 0.91), 0.085   # handle segment and half width
LINE = 0.045                 # outline width


def color_for(frac):
    return RED if frac >= 0.95 else ORANGE if frac >= 0.85 else BLUE


def _seg_dist(px, py, a, b):
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _sample(x, y, frac, used):
    """Colour (r, g, b, a) of one point; layers painted back to front."""
    d = math.hypot(x - CX, y - CY)
    dh = _seg_dist(x, y, H0, H1)
    col = None
    if d <= R_OUT + LINE or dh <= HW + LINE:
        col = OUTLINE
    if dh <= HW:
        col = HANDLE
    if d <= R_OUT:
        col = RIM
    if d <= R_IN - LINE * 0.6:
        ang = (math.degrees(math.atan2(x - CX, -(y - CY))) + 360) % 360      # 0 = top, clockwise
        col = used if ang < frac * 360 else FREE
        # thin dark spokes at the slice borders (only when there are two slices)
        if 0.02 < frac < 0.98:
            for edge in (0.0, frac * 360):
                ex, ey = math.sin(math.radians(edge)), -math.cos(math.radians(edge))
                if (x - CX) * ex + (y - CY) * ey > 0 and abs((x - CX) * ey - (y - CY) * ex) < LINE * 0.35:
                    col = OUTLINE
    elif d <= R_IN:
        col = OUTLINE                         # inner edge of the rim
    if col is None:
        return None
    # glass highlight: a soft white arc in the upper-left of the lens
    if d <= R_IN and abs(d - R_IN * 0.68) < 0.03:
        ang = (math.degrees(math.atan2(x - CX, -(y - CY))) + 360) % 360
        if 280 <= ang <= 340:
            col = tuple(int(c + (255 - c) * 0.65) for c in col)
    return col


def pixels(n, frac):
    """n x n RGBA bytes, top-down, 4x4 supersampled."""
    used = color_for(frac)
    ss = 4
    out = bytearray(n * n * 4)
    for py in range(n):
        for px in range(n):
            r = g = b = a = 0
            for sy in range(ss):
                for sx in range(ss):
                    c = _sample((px + (sx + 0.5) / ss) / n, (py + (sy + 0.5) / ss) / n, frac, used)
                    if c:
                        r += c[0]
                        g += c[1]
                        b += c[2]
                        a += 1
            if a:
                i = (py * n + px) * 4
                out[i:i + 4] = bytes((r // a, g // a, b // a, 255 * a // ss ** 2))
    return out


def ico(frac, sizes=(16, 20, 24, 32, 48, 64)):
    """.ico bytes with 32-bit BGRA images."""
    images = []
    for n in sizes:
        rgba = pixels(n, frac)
        bgra = bytearray(rgba)
        bgra[0::4], bgra[2::4] = rgba[2::4], rgba[0::4]
        rows = [bytes(bgra[y * n * 4:(y + 1) * n * 4]) for y in range(n)]
        xor = b"".join(reversed(rows))                     # bottom-up
        mask = b"\0" * (((n + 31) // 32) * 4 * n)
        hdr = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(xor) + len(mask), 0, 0, 0, 0)
        images.append((n, hdr + xor + mask))
    head = struct.pack("<HHH", 0, 1, len(images))
    off = 6 + 16 * len(images)
    body = b""
    for n, data in images:
        head += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(data), off + len(body))
        body += data
    return head + body


def png(n, frac):
    """PNG bytes (for previews and the web page favicon)."""
    raw = pixels(n, frac)
    rows = b"".join(b"\0" + bytes(raw[y * n * 4:(y + 1) * n * 4]) for y in range(n))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))
