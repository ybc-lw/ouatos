"""Pointer behaviour: flicker-free flush, repair, clamping, touchpad contact handling."""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, '..', 'installer'), HERE]
import fbgui, engine, installer
from fbgui import EvdevInput, _Dev, _EV
res = []
def check(c, m): print(('  PASS  ' if c else '  FAIL  ') + m); res.append(bool(c))

class Sink(bytearray):
    written = 0
    def __setitem__(self, k, v):
        self.written += (k.stop - k.start) if isinstance(k, slice) else 1
        super().__setitem__(k, v)

W, H = 1024, 768
scr = fbgui.Screen.virtual(W, H); scr.sink = Sink(len(scr.buf)); scr.cursor_enabled = True
fonts = fbgui.FontSet(os.path.join(HERE, '..', 'installer', 'font.bin'))
info = engine.load_media_info('/tmp/fakemedia'); env = engine.Env(media='/tmp/fakemedia', uefi=True)
wiz = installer.Wizard(scr, fonts, 1.25, env, info, disks_fn=lambda: [], power_fn=lambda a: None)
bpp, white = scr.bpp, scr.pix((255, 255, 255))
def sprite_ok(x, y):
    k = scr.cursor_scale
    return all(scr.sink[(y + j * k) * scr.stride + (x + i * k) * bpp:][:bpp] == white
               for j, l in enumerate(fbgui.CURSOR) for i, c in enumerate(l) if c == '#')
def sink_matches_buf_outside_cursor(x, y):
    k = scr.cursor_scale; cw, ch = len(fbgui.CURSOR[0]) * k, len(fbgui.CURSOR) * k
    for row in range(H):
        a, b = row * scr.stride, (row + 1) * scr.stride
        if y <= row < y + ch:
            if scr.sink[a:a + x * bpp] != scr.buf[a:a + x * bpp] or scr.sink[a + (x + cw) * bpp:b] != scr.buf[a + (x + cw) * bpp:b]: return False
        elif scr.sink[a:b] != scr.buf[a:b]: return False
    return True

wiz.goto('computer'); wiz.frame(); wiz.handle(('move', 700, 500))
check(sprite_ok(700, 500) and sink_matches_buf_outside_cursor(700, 500), 'pointer drawn; framebuffer == back buffer everywhere else')
full = len(scr.buf)
scr.sink.written = 0; time.sleep(0.51); redraw = wiz.tick(); wiz.frame()
check(redraw and scr.sink.written < full * 0.05, 'caret-blink redraw wrote only %d bytes (%.2f%% of the %d byte frame)' % (scr.sink.written, 100 * scr.sink.written / full, full))
check(sprite_ok(700, 500) and sink_matches_buf_outside_cursor(700, 500), 'pointer untouched by the blink redraw')
cur_before = scr.cursor
scr.sink.written = 0; wiz.frame()
check(scr.sink.written == 0, 'redrawing an identical frame writes nothing')
# a redraw that overlaps the pointer must repaint it
bx = [w for w in wiz.ui.prev if w[0] == 'next'][0][1]
wiz.handle(('move', bx[0] + 20, bx[1] + 10)); wiz.handle(('move', bx[0] + 22, bx[1] + 11))
x, y = bx[0] + 22, bx[1] + 11
for _ in range(6): wiz.ui.hover = 'next' if wiz.ui.hover != 'next' else None; wiz.frame()
check(sprite_ok(x, y), 'pointer repainted when hover redraws overlap it')
# something stomps on the pointer: refresh repairs it
for r in range(y, y + 12):
    scr.sink[r * scr.stride + x * bpp:r * scr.stride + (x + 30) * bpp] = bytes(30 * bpp)
check(not sprite_ok(x, y), '(corrupted the pointer on purpose)')
scr.refresh_cursor(); check(sprite_ok(x, y), 'refresh_cursor() repairs a damaged pointer')
# page change -> big redraw -> pointer still present and correct
wiz.goto('account'); wiz.frame(); check(sprite_ok(x, y) and sink_matches_buf_outside_cursor(x, y), 'pointer correct after a full page change')

# ---- input: absolute pointer clamping, touchpad contact handling
def ev(t, c, v): return _EV.pack(1, 0, t, c, v)
inp = EvdevInput.__new__(EvdevInput)
inp.w, inp.h, inp.devs, inp.x, inp.y = W, H, {}, 500, 400
inp.shift = inp.ctrl = inp.alt = inp.caps = False; inp.accel = 1.0
tab = _Dev(-1, 'x', 'tablet', {'tablet'}, {0: (0, 32767), 1: (0, 32767)})
out = inp._parse(tab, ev(3, 0, 40000) + ev(3, 1, -50) + ev(0, 0, 0))
check(all(0 <= e[1] < W and 0 <= e[2] < H for e in out if e[0] == 'move'), 'out-of-range tablet values are clamped on screen: %s' % (out[-1:],))
pad = _Dev(-1, 'p', 'pad', {'pad'}, {0: (0, 1000), 1: (0, 600)})
inp.x, inp.y = 100, 100
inp._parse(pad, ev(1, 0x145, 1) + ev(3, 0, 100) + ev(3, 1, 100) + ev(0, 0, 0))            # finger down (TOOL_FINGER only!)
inp._parse(pad, ev(3, 0, 130) + ev(3, 1, 100) + ev(0, 0, 0))
inp._parse(pad, ev(1, 0x145, 0) + ev(0, 0, 0))                                              # lift (no BTN_TOUCH at all)
x_after_lift = inp.x
out = inp._parse(pad, ev(1, 0x145, 1) + ev(3, 0, 900) + ev(3, 1, 500) + ev(0, 0, 0))        # touch down far away
check(inp.x == x_after_lift and not [e for e in out if e[0] == 'move' and e[1] != x_after_lift], 'touchpad lift via BTN_TOOL_FINGER only: no pointer jump on the next touch')
out = inp._parse(pad, ev(3, 0, 940) + ev(3, 1, 500) + ev(0, 0, 0))
check(inp.x > x_after_lift, 'motion resumes normally after the new touch')
inp._parse(pad, ev(1, 0x14D, 1) + ev(1, 0x145, 0))                                          # second finger: DOUBLETAP gesture
px = inp.x; inp._parse(pad, ev(3, 0, 300) + ev(3, 1, 100) + ev(0, 0, 0)); inp._parse(pad, ev(3, 0, 600) + ev(0, 0, 0))
check(inp.x == px, 'two-finger gestures do not move the pointer')
print('\nRESULT:', 'ALL POINTER TESTS PASSED' if all(res) else 'FAILURES: %d' % res.count(False))
sys.exit(0 if all(res) else 1)
