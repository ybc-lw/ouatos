#!/usr/bin/env python3
"""Pre-renders anti-aliased glyph bitmaps so the live installer needs no font rasteriser.

Output: installer/font.bin  (zlib-compressed: JSON header + concatenated 8-bit alpha bitmaps)
Fonts: DejaVu Sans / Sans Bold / Sans Mono (free licence, see LICENSE-DejaVu.txt)
"""
import json
import os
import struct
import sys
import zlib

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = '/usr/share/fonts/truetype/dejavu/'
FILES = {'sr': 'DejaVuSans.ttf', 'sb': 'DejaVuSans-Bold.ttf', 'mr': 'DejaVuSansMono.ttf'}
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'installer'))
from fontspec import NAMED, SCALES, CHARS


def main(out):
    wanted = sorted({(kind, round(base * s)) for kind, base in NAMED.values() for s in SCALES})
    header, blob = {}, bytearray()
    for kind, px in wanted:
        font = ImageFont.truetype(FONT_DIR + FILES[kind], px)
        asc, desc = font.getmetrics()
        glyphs = {}
        for ch in CHARS:
            adv = int(round(font.getlength(ch)))
            im = Image.new('L', (px * 4, px * 4), 0)
            ImageDraw.Draw(im).text((px, px * 2), ch, font=font, fill=255, anchor='ls')
            bb = im.getbbox()
            if bb is None:
                glyphs[ch] = [adv, 0, 0, 0, 0, 0]
                continue
            g = im.crop(bb)
            off = len(blob)
            blob += g.tobytes()
            glyphs[ch] = [adv, bb[0] - px, bb[1] - px * 2, g.width, g.height, off]
        header['%s%d' % (kind, px)] = dict(asc=asc, desc=desc, glyphs=glyphs)
    hj = json.dumps(header, separators=(',', ':')).encode()
    raw = b'OFNT1' + struct.pack('<I', len(hj)) + hj + bytes(blob)
    with open(out, 'wb') as f:
        f.write(zlib.compress(raw, 9))
    print('font atlas: %d font sizes, %d glyphs each, %.1f KiB compressed' % (
        len(header), len(CHARS), os.path.getsize(out) / 1024))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'installer', 'font.bin'))
