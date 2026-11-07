import os, struct, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, '..', 'installer')]
import fbgui
from fbgui import EvdevInput, _Dev, _EV, Field
res = []
def check(c, m): print(('  PASS  ' if c else '  FAIL  ') + m); res.append(bool(c))
def ev(t, c, v): return _EV.pack(1, 0, t, c, v)
def feed(inp, dev, *evs): return inp._parse(dev, b''.join(evs))
EV_KEY, EV_REL, EV_ABS, EV_SYN = 1, 2, 3, 0

check(_EV.size == 24, 'struct input_event is 24 bytes on x86_64')
inp = EvdevInput.__new__(EvdevInput)
inp.w, inp.h, inp.devs = 1000, 800, {}
inp.x, inp.y = 500, 400; inp.shift = inp.ctrl = inp.alt = inp.caps = False; inp.accel = 1.0
kbd = _Dev(-1, '/dev/input/event0', 'kbd', {'kbd'})
def keys(*codes):
    out = []
    for c in codes: out += feed(inp, kbd, ev(EV_KEY, c, 1), ev(EV_KEY, c, 0), ev(EV_SYN, 0, 0))
    return [(e[1], e[2]) for e in out if e[0] == 'key']
check(keys(35, 18, 38, 38, 24) == [('char', c) for c in 'hello'], 'typing "hello" (US layout keycodes)')
feed(inp, kbd, ev(EV_KEY, 42, 1)); out = keys(30, 2, 12, 41); feed(inp, kbd, ev(EV_KEY, 42, 0))
check(out == [('char', 'A'), ('char', '!'), ('char', '_'), ('char', '~')], 'Shift: A ! _ ~')
feed(inp, kbd, ev(EV_KEY, 58, 1), ev(EV_KEY, 58, 0)); out = keys(30, 2); feed(inp, kbd, ev(EV_KEY, 58, 1), ev(EV_KEY, 58, 0))
check(out == [('char', 'A'), ('char', '1')], 'Caps Lock affects letters only')
check(keys(28, 1, 15, 14, 111, 103, 108, 105, 106, 102, 107) == [(n, '') for n in ('enter', 'esc', 'tab', 'backspace', 'delete', 'up', 'down', 'left', 'right', 'home', 'end')], 'special keys')
check(keys(57, 52, 53, 39, 40) == [('char', c) for c in ' ./;\''], 'space and punctuation')
r = feed(inp, kbd, ev(EV_KEY, 30, 2))        # autorepeat (value 2)
check(r and r[0][0] == 'key', 'key autorepeat events are delivered')
# Field editing
f = Field('abc'); f.cur = 1
f.key('char', 'X', (False, False, False)); f.key('backspace', '', (False, False, False)); f.key('end', '', (False, False, False)); f.key('char', 'Z', (False, False, False))
check((f.text, f.cur) == ('abcZ', 4), 'Field editing (insert, backspace, end)')
# mouse
mouse = _Dev(-1, '/dev/input/event1', 'mouse', {'mouse'})
out = feed(inp, mouse, ev(EV_REL, 0, 30), ev(EV_REL, 1, -20), ev(EV_SYN, 0, 0))
check(out == [('move', 530, 380)], 'relative mouse motion %s' % out)
out = feed(inp, mouse, ev(EV_REL, 0, 5000), ev(EV_REL, 1, 5000), ev(EV_SYN, 0, 0))
check(out == [('move', 999, 799)], 'pointer clamped to the screen')
out = feed(inp, mouse, ev(EV_KEY, 0x110, 1), ev(EV_KEY, 0x110, 0))
check(out == [('down', 1), ('up', 1)], 'left button down/up')
out = feed(inp, mouse, ev(EV_REL, 8, -1)); check(out == [('scroll', 1)], 'wheel scroll')
# tablet (QEMU / VirtualBox / VMware absolute pointer)
tab = _Dev(-1, '/dev/input/event2', 'tablet', {'tablet'}, {0: (0, 32767), 1: (0, 32767)})
out = feed(inp, tab, ev(EV_ABS, 0, 16383), ev(EV_ABS, 1, 32767), ev(EV_SYN, 0, 0))
check(out[-1] == ('move', int(16383 / 32767 * 999), 799), 'absolute tablet maps onto the screen: %s' % (out[-1],))
# touchpad -> relative + tap to click
pad = _Dev(-1, '/dev/input/event3', 'pad', {'pad'}, {0: (0, 1000), 1: (0, 600)})
inp.x, inp.y = 100, 100
feed(inp, pad, ev(EV_KEY, 0x14A, 1), ev(EV_ABS, 0, 400), ev(EV_ABS, 1, 300), ev(EV_SYN, 0, 0))
out = feed(inp, pad, ev(EV_ABS, 0, 450), ev(EV_ABS, 1, 300), ev(EV_SYN, 0, 0))
check(out and out[-1][0] == 'move' and out[-1][1] > 100, 'touchpad finger motion moves the pointer right (%s)' % (out[-1:],))
feed(inp, pad, ev(EV_ABS, 0, 750), ev(EV_ABS, 1, 300), ev(EV_SYN, 0, 0))
out = feed(inp, pad, ev(EV_KEY, 0x14A, 0))
check(out == [], 'a drag is not a tap')
feed(inp, pad, ev(EV_KEY, 0x14A, 1)); out = feed(inp, pad, ev(EV_KEY, 0x14A, 0))
check(out == [('down', 1), ('up', 1)], 'quick touch = tap to click')
# probing logic with a fake ioctl-less /dev/input directory
print('\nRESULT:', 'ALL INPUT TESTS PASSED' if all(res) else 'FAILURES')
sys.exit(0 if all(res) else 1)
