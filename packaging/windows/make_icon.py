#!/usr/bin/env python3
"""Draws the Windows icon (#565): viewer/static/favicon.svg as a multi-size .ico.

    python make_icon.py viewer/static/favicon.svg out.ico

The favicon is a rounded tile and one filled path, and that is all this draws: the tile's rect
(width, height, rx, fill) and the path's fill and outline (M, L, H, V, C and Z, absolute or
relative). Each size is drawn on its own, antialiased, so the 16 px icon is the favicon at 16 px
rather than a shrunk 256 px picture. Small sizes are stored as bitmaps, 256 px as PNG: the form
every Windows tool reads (the NSIS compiler included). Standard library only, so packaging needs
no image tools.
"""
import re
import struct
import sys
import zlib

SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
ROWS = 16   # sub-scanlines per pixel row


def color(hexstr):
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def path_polygons(d, steps=24):
    """The outlines of an SVG path as closed polygons of (x, y), curves flattened."""
    toks = re.findall(r"[MmLlHhVvCcZz]|-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", d)
    polys, cur, x, y, sx, sy, cmd, i = [], [], 0.0, 0.0, 0.0, 0.0, None, 0

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v

    while i < len(toks):
        if re.fullmatch(r"[A-Za-z]", toks[i]):
            cmd = toks[i]
            i += 1
        rel = cmd.islower()
        c = cmd.upper()
        if c == "Z":
            if cur:
                polys.append(cur)
            cur, x, y = [], sx, sy
            continue
        if c != "M" and not cur:
            cur = [(x, y)]   # drawing on after a Z: a new outline from where it closed
        if c == "M":
            if cur:
                polys.append(cur)
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            sx, sy, cur = x, y, [(x, y)]
            cmd = "l" if rel else "L"   # further pairs after a move are lines
        elif c == "L":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if rel else (nx, ny)
            cur.append((x, y))
        elif c == "H":
            nx = num()
            x = x + nx if rel else nx
            cur.append((x, y))
        elif c == "V":
            ny = num()
            y = y + ny if rel else ny
            cur.append((x, y))
        elif c == "C":
            p = [num() for _ in range(6)]
            if rel:
                p = [p[k] + (x if k % 2 == 0 else y) for k in range(6)]
            x0, y0 = x, y
            for s in range(1, steps + 1):
                t = s / steps
                u = 1 - t
                cur.append((u * u * u * x0 + 3 * u * u * t * p[0] + 3 * u * t * t * p[2] + t * t * t * p[4],
                            u * u * u * y0 + 3 * u * u * t * p[1] + 3 * u * t * t * p[3] + t * t * t * p[5]))
            x, y = p[4], p[5]
        else:
            raise ValueError(f"path command {cmd!r} is not drawn here")
    if cur:
        polys.append(cur)
    return polys


def rounded_rect(w, h, r, steps=16):
    import math
    pts = []
    for cx, cy, a0 in ((w - r, r, -90), (w - r, h - r, 0), (r, h - r, 90), (r, r, 180)):
        for s in range(steps + 1):
            a = math.radians(a0 + 90 * s / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return [pts]


def coverage(polys, size, scale):
    """Per-pixel coverage (0..1) of polygons (nonzero rule) scaled by `scale` into size x size."""
    edges = []
    for poly in polys:
        for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
            if y0 != y1:
                edges.append((x0 * scale, y0 * scale, x1 * scale, y1 * scale))
    cov = [[0.0] * size for _ in range(size)]
    for row in range(size):
        line = cov[row]
        for sub in range(ROWS):
            yy = row + (sub + 0.5) / ROWS
            xs = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= yy < y1) or (y1 <= yy < y0):
                    xs.append((x0 + (yy - y0) * (x1 - x0) / (y1 - y0), 1 if y1 > y0 else -1))
            xs.sort()
            wind, start = 0, 0.0
            for xc, dirn in xs:
                was = wind
                wind += dirn
                if was == 0 and wind != 0:
                    start = xc
                elif was != 0 and wind == 0:
                    a, b = max(start, 0.0), min(xc, float(size))
                    while a < b:
                        px = int(a)
                        end = min(b, px + 1.0)
                        line[px] += (end - a) / ROWS
                        a = end
    return cov


def render(svg, size):
    vb = [float(v) for v in re.search(r'viewBox="([^"]+)"', svg).group(1).split()]
    scale = size / vb[2]
    rect = re.search(r"<rect\b([^>]*)>", svg).group(1)
    attr = lambda tag, name, default=None: (re.search(rf'\b{name}="([^"]+)"', tag) or [None, default])[1]  # noqa: E731
    layers = [(rounded_rect(float(attr(rect, "width")), float(attr(rect, "height")), float(attr(rect, "rx", "0"))),
               color(attr(rect, "fill")))]
    for p in re.findall(r"<path\b([^>]*)>", svg):
        layers.append((path_polygons(attr(p, "d")), color(attr(p, "fill"))))
    rgba = [[(0.0, 0.0, 0.0, 0.0)] * size for _ in range(size)]   # premultiplied
    for polys, (r, g, b) in layers:
        cov = coverage(polys, size, scale)
        for y in range(size):
            out = rgba[y]
            for x in range(size):
                a = min(cov[y][x], 1.0)
                if a <= 0:
                    continue
                pr, pg, pb, pa = out[x]
                out[x] = (r * a + pr * (1 - a), g * a + pg * (1 - a), b * a + pb * (1 - a), a + pa * (1 - a))
    pixels = []
    for row in rgba:
        line = []
        for pr, pg, pb, pa in row:
            if pa <= 0:
                line.append((0, 0, 0, 0))
            else:
                line.append((round(pr / pa), round(pg / pa), round(pb / pa), round(pa * 255)))
        pixels.append(line)
    return pixels


def png(pixels):
    size = len(pixels)
    raw = b"".join(b"\0" + bytes(c for px in row for c in px) for row in pixels)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def dib(pixels):
    size = len(pixels)
    head = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    color_rows = b"".join(bytes(c for r, g, b, a in row for c in (b, g, r, a)) for row in reversed(pixels))
    stride = ((size + 31) // 32) * 4
    mask = bytes(stride * size)   # all opaque: the alpha channel says what is not
    return head + color_rows + mask


def main():
    svg = open(sys.argv[1], encoding="utf-8").read()
    images = [(s, png(render(svg, s)) if s >= 256 else dib(render(svg, s))) for s in SIZES]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in images:
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    out += b"".join(data for _, data in images)
    with open(sys.argv[2], "wb") as f:
        f.write(out)


if __name__ == "__main__":
    main()
