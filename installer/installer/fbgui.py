"""A tiny dependency-free GUI toolkit that draws straight to the Linux framebuffer.

* Screen      - back buffer + /dev/fb0 (or a virtual screen for tests), any 16/24/32 bpp layout
* FontSet     - pre-rendered anti-aliased glyph atlas (see build/gen_font.py)
* EvdevInput  - keyboard (US layout), mouse, USB tablets and touchpads via /dev/input/event*
* UI          - immediate-mode widgets: button, text field, checkbox, option row, progress bar
"""
import fcntl
import glob
import json
import mmap
import os
import select
import struct
import time
import zlib

from fontspec import NAMED, SCALES

# ------------------------------------------------------------------ theme
BG = (15, 19, 25)
SIDEBAR = (11, 14, 19)
CARD = (25, 31, 40)
CARD_HI = (32, 40, 52)
BORDER = (46, 56, 70)
FIELD = (12, 16, 22)
TEXT = (231, 236, 243)
MUTED = (135, 148, 163)
ACCENT = (64, 132, 222)
ACCENT_HI = (88, 154, 238)
ACCENT_DIM = (40, 70, 112)
OK = (63, 185, 80)
WARN = (232, 184, 70)
DANGER = (240, 88, 80)
DANGER_DIM = (88, 34, 34)
WHITE = (255, 255, 255)


def mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


# ------------------------------------------------------------------ fonts
class Font:
    def __init__(self, key, meta, blob):
        self.key, self.asc, self.desc = key, meta['asc'], meta['desc']
        self.height = self.asc + self.desc
        self.g = meta['glyphs']
        self.blob = blob
        self._adv = {ch: v[0] for ch, v in self.g.items()}

    def adv(self, ch):
        return self._adv.get(ch) if ch in self._adv else self._adv['?']

    def width(self, s):
        a = self._adv
        q = a['?']
        return sum(a.get(c, q) for c in s)

    def glyph(self, ch):
        v = self.g.get(ch) or self.g['?']
        adv, left, top, w, h, off = v
        return adv, left, top, w, h, (self.blob[off:off + w * h] if w else b'')


class FontSet:
    def __init__(self, path):
        raw = zlib.decompress(open(path, 'rb').read())
        assert raw[:5] == b'OFNT1'
        hl = struct.unpack_from('<I', raw, 5)[0]
        self.meta = json.loads(raw[9:9 + hl])
        self.blob = memoryview(raw)[9 + hl:]
        self.cache = {}

    def get(self, name, scale):
        kind, base = NAMED[name]
        key = '%s%d' % (kind, round(base * scale))
        f = self.cache.get(key)
        if f is None:
            f = self.cache[key] = Font(key, self.meta[key], self.blob)
        return f


# ------------------------------------------------------------------ screen
FBIOGET_VSCREENINFO, FBIOGET_FSCREENINFO = 0x4600, 0x4602


class Screen:
    def __init__(self, w, h, bpp, stride, fmt, sink=None):
        """fmt = ((r_off, r_len), (g_off, g_len), (b_off, b_len))"""
        self.w, self.h, self.bpp, self.stride = w, h, bpp // 8, stride
        (self.ro, self.rl), (self.go, self.gl), (self.bo, self.bl) = fmt
        self.buf = bytearray(stride * h)
        self.sink = sink                      # mmap of the framebuffer, or None
        self._pix, self._lut, self._glyphs, self._corners = {}, {}, {}, {}
        self.cursor = None                    # (x, y, w, h) of the pointer currently drawn on the fb
        self._shadow = None                   # copy of what the fb shows (without the pointer): lets flush() write only changes
        self.cursor_enabled = False

    @classmethod
    def open_fb(cls, path='/dev/fb0'):
        fd = os.open(path, os.O_RDWR)
        try:
            var = fcntl.ioctl(fd, FBIOGET_VSCREENINFO, bytes(160))
            fix = fcntl.ioctl(fd, FBIOGET_FSCREENINFO, bytes(128))
            xres, yres, _xv, _yv, _xo, _yo, bpp, _gray = struct.unpack_from('<8I', var, 0)
            ro, rl, _ = struct.unpack_from('<3I', var, 32)
            go, gl, _ = struct.unpack_from('<3I', var, 44)
            bo, bl, _ = struct.unpack_from('<3I', var, 56)
            line = struct.unpack_from('<I', fix, 48)[0]
            if (bpp not in (16, 24, 32) or line < xres * bpp // 8 or _xo or _yo or
                    any(bits < 1 or offset + bits > bpp for offset, bits in ((ro,rl),(go,gl),(bo,bl)))):
                raise OSError('unsupported framebuffer: %dx%d %dbpp stride %d' % (xres, yres, bpp, line))
            mm = mmap.mmap(fd, line * yres, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            return cls(xres, yres, bpp, line, ((ro, rl), (go, gl), (bo, bl)), sink=mm)
        finally:
            os.close(fd)

    @classmethod
    def virtual(cls, w, h):
        return cls(w, h, 32, w * 4, ((16, 8), (8, 8), (0, 8)))

    # -------------------------------------------------- pixels
    def pix(self, c):
        b = self._pix.get(c)
        if b is None:
            v = sum(((channel * ((1 << bits) - 1) + 127) // 255) << offset
                    for channel, offset, bits in zip(c, (self.ro, self.go, self.bo), (self.rl, self.gl, self.bl)))
            b = self._pix[c] = v.to_bytes(self.bpp, 'little')
        return b

    def fill(self, x, y, w, h, c):
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, self.w), min(y + h, self.h)
        if x1 <= x0 or y1 <= y0:
            return
        p, n = self.pix(c), (x1 - x0) * self.bpp
        if x0 == 0 and x1 == self.w and self.stride == n:
            self.buf[y0 * self.stride:y1 * self.stride] = p * ((x1 - x0) * (y1 - y0))
            return
        row = p * (x1 - x0)
        o = y0 * self.stride + x0 * self.bpp
        buf, st = self.buf, self.stride
        for _ in range(y1 - y0):
            buf[o:o + n] = row
            o += st

    def blit_row(self, x, y, data):
        """data: pixel bytes for one row."""
        if y < 0 or y >= self.h:
            return
        bpp = self.bpp
        n = len(data) // bpp
        if x < 0:
            data, n, x = data[-x * bpp:], n + x, 0
        if x + n > self.w:
            n = self.w - x
            data = data[:n * bpp]
        if n <= 0:
            return
        o = y * self.stride + x * bpp
        self.buf[o:o + n * bpp] = data

    # -------------------------------------------------- shapes
    def _corner(self, r, c, bg):
        key = (r, c, bg)
        v = self._corners.get(key)
        if v is None:
            ss = 4
            fg, bgp = self.pix(c), self.pix(bg)
            rows = []
            for i in range(r):
                left = bytearray()
                lead = 0
                for j in range(r):
                    inside = 0
                    for sy in range(ss):
                        for sx in range(ss):
                            px, py = j + (sx + 0.5) / ss, i + (sy + 0.5) / ss
                            if (px - r) ** 2 + (py - r) ** 2 <= r * r:
                                inside += 1
                    cov = inside / (ss * ss)
                    if cov <= 0 and lead == j:
                        lead += 1
                    left += fg if cov >= 1 else bgp if cov <= 0 else self.pix(mix(bg, c, cov))
                right = bytearray()
                for j in range(r - 1, -1, -1):
                    right += left[j * self.bpp:(j + 1) * self.bpp]
                rows.append((bytes(left), bytes(right), lead))
            v = self._corners[key] = rows
        return v

    def rrect(self, x, y, w, h, r, c, bg, overlay=False):
        """Rounded rectangle.  overlay=True leaves fully transparent corner pixels untouched."""
        r = min(int(r), w // 2, h // 2)
        if w <= 0 or h <= 0:
            return
        if r <= 1:
            return self.fill(x, y, w, h, c)
        mid = self.pix(c) * (w - 2 * r)
        bpp = self.bpp
        for i, (lft, rgt, lead) in enumerate(self._corner(r, c, bg)):
            if overlay and lead:
                row = lft[lead * bpp:] + mid + rgt[:len(rgt) - lead * bpp]
                rx = x + lead
            else:
                row, rx = lft + mid + rgt, x
            self.blit_row(rx, y + i, row)
            self.blit_row(rx, y + h - 1 - i, row)
        self.fill(x, y + r, w, h - 2 * r, c)

    def frame(self, x, y, w, h, r, fill, border, bg, t=1):
        self.rrect(x, y, w, h, r, border, bg)
        self.rrect(x + t, y + t, w - 2 * t, h - 2 * t, max(r - t, 0), fill, border, overlay=True)

    # -------------------------------------------------- text
    def _glyph(self, font, ch, fg, bg):
        key = (font.key, ch, fg, bg)
        v = self._glyphs.get(key)
        if v is None:
            adv, left, top, w, h, bm = font.glyph(ch)
            rows = []
            if w:
                lk = (fg, bg)
                lut = self._lut.get(lk)
                if lut is None:
                    lut = self._lut[lk] = [self.pix(mix(bg, fg, a / 255)) for a in range(256)]
                for r in range(h):
                    rows.append(b''.join([lut[a] for a in bm[r * w:(r + 1) * w]]))
            v = self._glyphs[key] = (adv, left, top, rows)
        return v

    def text(self, x, y, s, font, fg, bg):
        """Draw with the top of the line box at y on a flat `bg`. Returns the width drawn."""
        base, cx = y + font.asc, x
        for ch in s:
            adv, left, top, rows = self._glyph(font, ch, fg, bg)
            gy = base + top
            for i, row in enumerate(rows):
                self.blit_row(cx + left, gy + i, row)
            cx += adv
        return cx - x

    # -------------------------------------------------- output
    def flush(self):
        """Push the back buffer to the framebuffer, writing only the rows that changed.

        Writing only changes keeps redraws fast on slow (virtualised) framebuffers and, importantly, means the
        mouse pointer is not wiped and repainted on every caret blink / hover change."""
        sink = self.sink
        if sink is None:
            self.cursor = None
            return
        buf, st, h = self.buf, self.stride, self.h
        sh = self._shadow
        if sh is None:
            sink[0:len(buf)] = buf
            self._shadow = bytearray(buf)
            self.cursor = None
        else:
            runs, start = [], None
            for y in range(h):
                o = y * st
                if buf[o:o + st] != sh[o:o + st]:
                    if start is None:
                        start = y
                elif start is not None:
                    runs.append((start, y))
                    start = None
            if start is not None:
                runs.append((start, h))
            for a, b in runs:
                sink[a * st:b * st] = buf[a * st:b * st]
                sh[a * st:b * st] = buf[a * st:b * st]
            if self.cursor is not None:
                _x, cy, _w, ch = self.cursor
                if any(a < cy + ch and b > cy for a, b in runs):      # a changed row overwrote the pointer
                    self.cursor = None
        if self.cursor is None:
            self._draw_cursor()

    def refresh_cursor(self):
        """Idempotently repaint the pointer (repairs it if anything else wrote over it)."""
        if self.cursor_enabled:
            self._draw_cursor()

    def set_cursor(self, x, y):
        if not self.cursor_enabled:
            return
        self._erase_cursor()
        self._cx, self._cy = x, y
        self._draw_cursor()

    _cx = _cy = 0

    def _sprite_rect(self):
        k = self.cursor_scale
        return self._cx, self._cy, len(CURSOR[0]) * k, len(CURSOR) * k

    cursor_scale = 1

    def _erase_cursor(self):
        if self.cursor is None or self.sink is None:
            self.cursor = None
            return
        x, y, w, h = self.cursor
        x0, x1 = max(x, 0), min(x + w, self.w)
        for yy in range(max(y, 0), min(y + h, self.h)):
            o = yy * self.stride + x0 * self.bpp
            n = (x1 - x0) * self.bpp
            if n > 0:
                self.sink[o:o + n] = self.buf[o:o + n]
        self.cursor = None

    def _draw_cursor(self):
        if not self.cursor_enabled or self.sink is None:
            return
        k = self.cursor_scale
        x, y, w, h = self._sprite_rect()
        white, black = self.pix((255, 255, 255)), self.pix((0, 0, 0))
        for j, line in enumerate(CURSOR):
            for ky in range(k):
                yy = y + j * k + ky
                if not 0 <= yy < self.h:
                    continue
                for i, ch in enumerate(line):
                    if ch == '.':
                        continue
                    p = white if ch == '#' else black
                    xx = x + i * k
                    x0, x1 = max(xx, 0), min(xx + k, self.w)
                    if x1 > x0:
                        o = yy * self.stride + x0 * self.bpp
                        self.sink[o:o + (x1 - x0) * self.bpp] = p * (x1 - x0)
        self.cursor = (x, y, w, h)

    def to_ppm(self):
        """Raw RGB dump of the back buffer (used by tests to make screenshots)."""
        out = bytearray()
        for y in range(self.h):
            row = self.buf[y * self.stride:y * self.stride + self.w * self.bpp]
            for x in range(self.w):
                v = int.from_bytes(row[x * self.bpp:(x + 1) * self.bpp], 'little')
                out += bytes(((v >> offset) & ((1 << bits) - 1)) * 255 // ((1 << bits) - 1)
                             for offset, bits in ((self.ro,self.rl), (self.go,self.gl), (self.bo,self.bl)))
        return bytes(out)


CURSOR = [
    'X...........', 'XX..........', 'X#X.........', 'X##X........', 'X###X.......', 'X####X......',
    'X#####X.....', 'X######X....', 'X#######X...', 'X########X..', 'X#####XXXXX.', 'X##X##X.....',
    'X#X X##X....', 'XX  X##X....', 'X    X##X...', '     X##X...', '      X##X..', '      X##X..',
    '       XX...',
]
CURSOR = [l.replace(' ', '.') for l in CURSOR]


# ------------------------------------------------------------------ keyboard map (US)
def _build_keymap():
    km = {}
    for codes, lo, hi in ((range(2, 14), '1234567890-=', '!@#$%^&*()_+'),
                          (range(16, 28), 'qwertyuiop[]', 'QWERTYUIOP{}'),
                          (range(30, 41), "asdfghjkl;'", 'ASDFGHJKL:"'),
                          (range(44, 54), 'zxcvbnm,./', 'ZXCVBNM<>?')):
        for c, a, b in zip(codes, lo, hi):
            km[c] = (a, b)
    km[41] = ('`', '~')
    km[43] = ('\\', '|')
    km[57] = (' ', ' ')
    for c, d in ((71, '7'), (72, '8'), (73, '9'), (75, '4'), (76, '5'), (77, '6'),
                 (79, '1'), (80, '2'), (81, '3'), (82, '0'), (83, '.'), (74, '-'), (78, '+'), (55, '*'), (98, '/')):
        km[c] = (d, d)
    return km


KEYMAP = _build_keymap()
SPECIAL = {1: 'esc', 14: 'backspace', 15: 'tab', 28: 'enter', 96: 'enter', 111: 'delete', 102: 'home', 107: 'end',
           104: 'pageup', 109: 'pagedown', 103: 'up', 108: 'down', 105: 'left', 106: 'right'}
SHIFTS, CTRLS, ALTS, CAPS = (42, 54), (29, 97), (56, 100), 58
EVIOCGNAME = lambda n: (2 << 30) | (n << 16) | (0x45 << 8) | 0x06
EVIOCGBIT = lambda ev, n: (2 << 30) | (n << 16) | (0x45 << 8) | (0x20 + ev)
EVIOCGABS = lambda a: (2 << 30) | (24 << 16) | (0x45 << 8) | (0x40 + a)
_EV = struct.Struct('=qqHHi')


def _bits(fd, ev, nbytes=96):
    try:
        return int.from_bytes(fcntl.ioctl(fd, EVIOCGBIT(ev, nbytes), bytes(nbytes)), 'little')
    except OSError:
        return 0


class _Dev:
    def __init__(self, fd, path, name, roles, absinfo=None):
        self.fd, self.path, self.name, self.roles, self.abs = fd, path, name, roles, absinfo or {}
        self.ax = self.ay = None
        self.touch_t = None
        self.moved = 0
        self.btn_touch = False
        self.tools = set()
        self.touching = False


class EvdevInput:
    """Turns /dev/input/event* into ('key', name, char, mods) / ('move', x, y) / ('down'|'up', btn) events."""

    def __init__(self, w, h, scale=1.0):
        self.w, self.h, self.devs = w, h, {}
        self.x, self.y = w // 2, h // 2
        self.shift = self.ctrl = self.alt = self.caps = False
        self.accel = max(1.0, scale * 0.8)
        self.last_scan = 0.0
        self.has_pointer = False
        self.moved_hint = False
        self.scan()

    def scan(self):
        self.last_scan = time.time()
        for path in glob.glob('/dev/input/event*'):
            if path in self.devs:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            try:
                name = fcntl.ioctl(fd, EVIOCGNAME(128), bytes(128)).split(b'\0')[0].decode('utf-8', 'replace')
                ev = _bits(fd, 0, 4)
                key, rel, ab = _bits(fd, 1), _bits(fd, 2, 4), _bits(fd, 3, 8)
                roles, absinfo, absval = set(), {}, {}
                if ev & 2 and key & (1 << 30) and key & (1 << 28):          # KEY_A and KEY_ENTER
                    roles.add('kbd')
                if ev & 4 and rel & 3 == 3 and key & (1 << 0x110):          # REL_X+REL_Y and BTN_LEFT
                    roles.add('mouse')
                pointer_keys = (1 << 0x110) | (1 << 0x14A) | (1 << 0x145) | (1 << 0x140) | (1 << 0x14B)   # LEFT, TOUCH, TOOL_FINGER, TOOL_PEN, STYLUS
                if ev & 8 and ab & 3 == 3 and key & pointer_keys:
                    for a in (0, 1):
                        v = struct.unpack('<6i', fcntl.ioctl(fd, EVIOCGABS(a), bytes(24)))
                        absinfo[a] = (v[1], v[2])
                        absval[a] = v[0]
                    roles.add('pad' if key & (1 << 0x145) else 'tablet')
                if not roles:
                    os.close(fd)
                    continue
                dev = self.devs[path] = _Dev(fd, path, name, roles, absinfo)
                if roles - {'kbd'}:
                    self.has_pointer = True
                if 'tablet' in roles and absval:                             # start where the pointer really is
                    for a, val in absval.items():
                        lo, hi = absinfo[a]
                        frac = min(max((val - lo) / max(1, hi - lo), 0.0), 1.0)
                        if a == 0:
                            self.x = int(frac * (self.w - 1))
                        else:
                            self.y = int(frac * (self.h - 1))
                    self.moved_hint = True
            except OSError:
                os.close(fd)

    def pointer_names(self):
        return [d.name for d in self.devs.values() if d.roles - {'kbd'}]

    def fds(self):
        return [d.fd for d in self.devs.values()]

    def poll(self, timeout):
        if time.time() - self.last_scan > 2.0:
            self.scan()
        fds = self.fds()
        if not fds:
            time.sleep(min(timeout, 0.1))
            return []
        try:
            r, _, _ = select.select(fds, [], [], timeout)
        except (OSError, ValueError):
            return []
        out = []
        for d in list(self.devs.values()):
            if d.fd not in r:
                continue
            try:
                data = os.read(d.fd, _EV.size * 64)
            except BlockingIOError:
                continue
            except OSError:                     # unplugged
                os.close(d.fd)
                del self.devs[d.path]
                continue
            out += self._parse(d, data)
        return out

    def _parse(self, d, data):
        out, dx, dy = [], 0.0, 0.0
        moved = False
        def flush_motion():
            nonlocal dx, dy, moved
            if moved:
                self.x = int(min(max(self.x + dx * self.accel, 0), self.w - 1))
                self.y = int(min(max(self.y + dy * self.accel, 0), self.h - 1))
                out.append(('move', self.x, self.y))
                dx = dy = 0.0
                moved = False
        for i in range(0, len(data) - _EV.size + 1, _EV.size):
            _s, _u, typ, code, val = _EV.unpack_from(data, i)
            if typ in (0, 1) or (typ == 2 and code == 8):
                flush_motion()
            if typ == 1:                                                   # ---- EV_KEY
                if code in SHIFTS:
                    self.shift = val != 0
                elif code in CTRLS:
                    self.ctrl = val != 0
                elif code in ALTS:
                    self.alt = val != 0
                elif code == CAPS:
                    if val == 1:
                        self.caps = not self.caps
                elif code == 0x110:                                        # BTN_LEFT
                    out.append(('down' if val else 'up', 1))
                elif code == 0x14A and 'tablet' in d.roles:                # BTN_TOUCH on a touchscreen / tablet = click
                    out.append(('down' if val else 'up', 1))
                elif code in (0x14A, 0x145, 0x14D, 0x14E, 0x14F) and 'pad' in d.roles:
                    # touchpad contact: BTN_TOUCH and/or BTN_TOOL_FINGER/DOUBLETAP/TRIPLETAP/QUADTAP
                    if code == 0x14A:
                        d.btn_touch = bool(val)
                    elif val:
                        d.tools.add(code)
                    else:
                        d.tools.discard(code)
                    out += self._pad_contact(d)
                elif val in (1, 2) and 'kbd' in d.roles:
                    ev = self._key(code)
                    if ev:
                        out.append(ev)
            elif typ == 2:                                                 # ---- EV_REL
                if code == 0:
                    dx += val
                    moved = True
                elif code == 1:
                    dy += val
                    moved = True
                elif code == 8:
                    out.append(('scroll', -val))
            elif typ == 3 and code in (0, 1):                              # ---- EV_ABS
                lo, hi = d.abs.get(code, (0, 1))
                span = max(1, hi - lo)
                if 'tablet' in d.roles:                                    # absolute pointer: map to screen
                    frac = min(max((val - lo) / span, 0.0), 1.0)
                    if code == 0:
                        self.x = int(frac * (self.w - 1))
                    else:
                        self.y = int(frac * (self.h - 1))
                    out.append(('move', self.x, self.y))
                elif 'pad' in d.roles:                                     # touchpad: convert to relative motion
                    if d.tools - {0x145}:                                   # 2+ fingers (DOUBLETAP/TRIPLETAP...): a gesture, not pointer motion
                        d.ax = d.ay = None
                        continue
                    prev = d.ax if code == 0 else d.ay
                    if code == 0:
                        d.ax = val
                    else:
                        d.ay = val
                    if prev is not None:
                        delta = (val - prev) * (self.w / 2.5) / span
                        d.moved += abs(val - prev) * 100 // span
                        if code == 0:
                            dx += delta
                        else:
                            dy += delta
                        moved = True
        if moved:
            self.x = int(min(max(self.x + dx * self.accel, 0), self.w - 1))
            self.y = int(min(max(self.y + dy * self.accel, 0), self.h - 1))
            out.append(('move', self.x, self.y))
        return out

    def _pad_contact(self, d):
        """Track finger contact on a touchpad: reset motion on every change, tap-to-click on a quick touch."""
        out = []
        active = d.btn_touch or bool(d.tools)
        d.ax = d.ay = None                                      # contact changed: never carry a delta across it
        if active and not d.touching:
            d.touching, d.touch_t, d.moved = True, time.time(), 0
        elif not active and d.touching:
            d.touching = False
            if d.touch_t is not None and time.time() - d.touch_t < 0.25 and d.moved < 6:
                out += [('down', 1), ('up', 1)]                 # tap to click
            d.touch_t = None
        return out

    def _key(self, code):
        mods = (self.shift, self.ctrl, self.alt)
        if code in SPECIAL:
            return ('key', SPECIAL[code], '', mods)
        if code in KEYMAP:
            lo, hi = KEYMAP[code]
            up = self.shift != (self.caps and lo.isalpha())
            return ('key', 'char', hi if up else lo, mods)
        return None


# ------------------------------------------------------------------ text editing state
class Field:
    def __init__(self, text=''):
        self.text, self.cur = text, len(text)

    def insert(self, s):
        self.text = self.text[:self.cur] + s + self.text[self.cur:]
        self.cur += len(s)

    def key(self, name, ch, mods):
        t, c = self.text, self.cur
        if name == 'char' and ch and not mods[1]:
            self.insert(ch)
        elif name == 'backspace' and c:
            self.text, self.cur = t[:c - 1] + t[c:], c - 1
        elif name == 'delete':
            self.text = t[:c] + t[c + 1:]
        elif name == 'left':
            self.cur = max(0, c - 1)
        elif name == 'right':
            self.cur = min(len(t), c + 1)
        elif name == 'home':
            self.cur = 0
        elif name == 'end':
            self.cur = len(t)
        elif name == 'char' and mods[1] and ch.lower() == 'u':
            self.text, self.cur = t[c:], 0
        else:
            return False
        return True


# ------------------------------------------------------------------ UI
class UI:
    def __init__(self, screen, fonts, scale):
        self.scr, self.fonts, self.scale = screen, fonts, scale
        self.focus = None
        self.hover = None
        self.pressed = None
        self.prev, self.cur = [], []
        self.lw, self.lh = screen.w / scale, screen.h / scale
        screen.cursor_scale = max(1, int(round(scale)))

    # -------------------------------------------------- helpers
    def S(self, n):
        return int(round(n * self.scale))

    def font(self, name):
        return self.fonts.get(name, self.scale)

    def begin(self, bg=BG):
        self.cur = []
        self.scr.fill(0, 0, self.scr.w, self.scr.h, bg)

    def end(self):
        self.prev = self.cur
        ids = [w[0] for w in self.prev if w[3]]
        if self.focus not in ids:
            self.focus = ids[0] if ids else None

    def reg(self, wid, rect, kind, enabled=True, obj=None, x0=0):
        self.cur.append((wid, rect, kind, enabled, obj, x0))

    # -------------------------------------------------- text
    def text(self, x, y, s, font='body', color=TEXT, bg=BG):
        return self.scr.text(x, y, s, self.font(font), color, bg)

    def width(self, s, font='body'):
        return self.font(font).width(s)

    def lineh(self, font='body'):
        return int(self.font(font).height * 1.25)

    def wrap(self, s, font, maxw):
        f, lines = self.font(font), []
        for para in s.split('\n'):
            cur = ''
            for word in para.split(' '):
                t = (cur + ' ' + word) if cur else word
                if f.width(t) <= maxw or not cur:
                    cur = t
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        return lines

    def paragraph(self, x, y, s, maxw, font='body', color=TEXT, bg=BG):
        """Returns the y below the last line."""
        for ln in self.wrap(s, font, maxw):
            self.text(x, y, ln, font, color, bg)
            y += self.lineh(font)
        return y

    def ellipsize(self, s, font, maxw):
        f = self.font(font)
        if f.width(s) <= maxw:
            return s
        while s and f.width(s + '…') > maxw:
            s = s[:-1]
        return s + '…'

    # -------------------------------------------------- widgets
    def card(self, x, y, w, h, fill=CARD, border=BORDER, bg=BG):
        self.scr.frame(x, y, w, h, self.S(10), fill, border, bg)

    def button(self, wid, x, y, w, h, label, kind='normal', enabled=True):
        focused = self.focus == wid and enabled
        hover = self.hover == wid and enabled
        pressed = self.pressed == wid and hover
        if kind == 'primary':
            fill = ACCENT_DIM if not enabled else (ACCENT_HI if hover else ACCENT)
            fg, edge = (WHITE if enabled else MUTED), ACCENT_HI
        elif kind == 'danger':
            fill = DANGER_DIM if not enabled else ((255, 110, 100) if hover else DANGER)
            fg, edge = (WHITE if enabled else MUTED), (255, 130, 120)
        else:
            fill = CARD_HI if hover else CARD
            fg, edge = (TEXT if enabled else MUTED), BORDER
        if pressed:
            fill = mix(fill, (0, 0, 0), 0.25)
        bg = BG
        r = self.S(8)
        if focused:
            self.scr.rrect(x - self.S(3), y - self.S(3), w + self.S(6), h + self.S(6), r + self.S(3), WHITE if kind != 'normal' else ACCENT_HI, bg)
            self.scr.rrect(x - self.S(1), y - self.S(1), w + self.S(2), h + self.S(2), r + self.S(1), bg, WHITE, overlay=True)
        self.scr.frame(x, y, w, h, r, fill, edge, bg)
        f = self.font('bodyb')
        tw = f.width(label)
        self.scr.text(x + (w - tw) // 2, y + (h - f.height) // 2, label, f, fg, fill)
        self.reg(wid, (x, y, w, h), 'button', enabled)

    def field(self, wid, x, y, w, st, password=False, placeholder='', label=None, error=False):
        """Text field; returns total height used (label + box)."""
        used = 0
        if label:
            self.text(x, y, label, 'small', MUTED, CARD)
            used = self.lineh('small') + self.S(2)
        h = self.S(38)
        focused = self.focus == wid
        border = DANGER if error else (ACCENT_HI if focused else BORDER)
        self.scr.frame(x, y + used, w, h, self.S(7), FIELD, border, CARD, t=max(1, self.S(1)) + (1 if focused else 0))
        f = self.font('body')
        pad = self.S(11)
        shown = ('•' * len(st.text)) if password else st.text
        inner = w - 2 * pad
        start = 0
        curtxt = shown[:st.cur]
        while start < st.cur and f.width(curtxt[start:]) > inner - self.S(3):
            start += 1
        vis = shown[start:]
        while vis and f.width(vis) > inner:
            vis = vis[:-1]
        ty = y + used + (h - f.height) // 2
        if vis:
            self.scr.text(x + pad, ty, vis, f, TEXT, FIELD)
        elif placeholder:
            self.scr.text(x + pad, ty, placeholder, f, MUTED, FIELD)
        if focused and int(time.time() * 2) % 2 == 0:
            cx = x + pad + f.width(shown[start:st.cur])
            self.scr.fill(cx, ty, max(1, self.S(2)), f.height, TEXT)
        self.reg(wid, (x, y + used, w, h), 'field', True, (st, password), x + pad - f.width(shown[:start]))
        return used + h

    def checkbox(self, wid, x, y, label, checked, enabled=True, bg=CARD):
        focused = self.focus == wid and enabled
        hover = self.hover == wid and enabled
        s = self.S(20)
        if focused:
            self.scr.rrect(x - self.S(3), y - self.S(3), s + self.S(6), s + self.S(6), self.S(8), ACCENT_HI, bg)
        border = ACCENT_HI if (focused or hover) else BORDER
        fill = ACCENT if checked else FIELD
        self.scr.frame(x, y, s, s, self.S(5), fill, ACCENT_HI if checked else border, ACCENT_HI if focused else bg)
        if checked:
            f = self.font('bodyb')
            self.scr.text(x + (s - f.width('✓')) // 2, y + (s - f.height) // 2, '✓', f, WHITE, fill)
        f = self.font('body')
        tw = self.scr.text(x + s + self.S(10), y + (s - f.height) // 2, label, f, TEXT if enabled else MUTED, bg)
        self.reg(wid, (x - self.S(4), y - self.S(4), s + self.S(18) + tw, s + self.S(8)), 'check', enabled)
        return s + self.S(10) + tw

    def option(self, wid, x, y, w, h, title, sub, selected, enabled=True, right=''):
        focused, hover = self.focus == wid and enabled, self.hover == wid and enabled
        fill = mix(CARD, ACCENT, 0.18) if selected else (CARD_HI if hover else CARD)
        border = ACCENT_HI if selected else (WHITE if focused else BORDER)
        self.scr.frame(x, y, w, h, self.S(10), fill, border, BG, t=max(1, self.S(1)) + (1 if focused or selected else 0))
        rs = self.S(10)                                   # radio
        cx, cy = x + self.S(20), y + h // 2
        self.scr.rrect(cx - rs, cy - rs, 2 * rs, 2 * rs, rs, ACCENT_HI if selected else BORDER if enabled else CARD_HI, fill)
        self.scr.rrect(cx - rs + self.S(2), cy - rs + self.S(2), 2 * rs - self.S(4), 2 * rs - self.S(4), rs - self.S(2), fill, ACCENT_HI if selected else BORDER, overlay=True)
        if selected:
            self.scr.rrect(cx - self.S(5), cy - self.S(5), self.S(10), self.S(10), self.S(5), ACCENT_HI, fill)
        tx = x + self.S(46)
        maxw = w - self.S(46) - self.S(16) - (self.width(right, 'bodyb') + self.S(12) if right else 0)
        tf = self.font('bodyb')
        sf = self.font('small')
        ty = y + (h - (tf.height + sf.height + self.S(4))) // 2
        self.scr.text(tx, ty, self.ellipsize(title, 'bodyb', maxw), tf, TEXT if enabled else MUTED, fill)
        self.scr.text(tx, ty + tf.height + self.S(4), self.ellipsize(sub, 'small', maxw), sf, MUTED if enabled else mix(MUTED, fill, 0.35), fill)
        if right:
            rw = self.width(right, 'bodyb')
            self.scr.text(x + w - rw - self.S(16), y + (h - tf.height) // 2, right, tf, TEXT if enabled else MUTED, fill)
        self.reg(wid, (x, y, w, h), 'option', enabled)

    def progress(self, x, y, w, h, frac, color=ACCENT, bg=BG):
        self.scr.rrect(x, y, w, h, h // 2, BORDER, bg)
        self.scr.rrect(x + 1, y + 1, w - 2, h - 2, (h - 2) // 2, FIELD, BORDER, overlay=True)
        fw = int((w - 2) * max(0.0, min(1.0, frac)))
        if fw > 0:
            self.scr.rrect(x + 1, y + 1, max(fw, h - 2), h - 2, (h - 2) // 2, color, FIELD)

    # -------------------------------------------------- events
    def _hit(self, x, y):
        for w in reversed(self.prev):
            rx, ry, rw, rh = w[1]
            if w[3] and rx <= x < rx + rw and ry <= y < ry + rh:
                return w
        return None

    def mouse_move(self, x, y):
        h = self._hit(x, y)
        nh = h[0] if h else None
        changed = nh != self.hover
        self.hover = nh
        return changed

    def mouse_down(self, x, y):
        h = self._hit(x, y)
        if not h:
            return False
        self.focus, self.pressed = h[0], h[0]
        if h[2] == 'field':
            st, password = h[4]
            f = self.font('body')
            shown = ('•' * len(st.text)) if password else st.text
            rel = x - h[5]
            c = 0
            while c < len(shown) and f.width(shown[:c + 1]) - f.adv(shown[c]) // 2 < rel:
                c += 1
            st.cur = c
        return True

    def mouse_up(self, x, y):
        h = self._hit(x, y)
        pressed, self.pressed = self.pressed, None
        if h and h[0] == pressed and h[2] in ('button', 'check', 'option'):
            return h[0]
        return None

    def focus_step(self, d):
        ids = [w[0] for w in self.prev if w[3]]
        if not ids:
            return
        i = ids.index(self.focus) if self.focus in ids else -1
        self.focus = ids[(i + d) % len(ids)]

    def focused_widget(self):
        for w in self.prev:
            if w[0] == self.focus:
                return w
        return None
