"""Drives the real Wizard on a virtual screen with synthetic keyboard/mouse events."""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, '..', 'installer'), HERE]
import engine
import fbgui
from shot import save_png
import installer

OUT = '/tmp/shots'
os.makedirs(OUT, exist_ok=True)
results = []


def check(cond, msg):
    print(('  PASS  ' if cond else '  FAIL  ') + msg)
    results.append(bool(cond))


GiB = engine.GiB
FAKE_DISKS = lambda: [
    engine.Disk('nvme0n1', 500 * 10**9, 512, 'Samsung SSD 970 EVO Plus 500GB', 'NVMe', False),
    engine.Disk('sda', 2000 * 10**9, 512, 'WDC WD20EZAZ-00GGJB0', 'SATA/SCSI', False),
    engine.Disk('sdb', 16 * GiB, 512, 'SanDisk Ultra USB 3.0', 'USB', True),
    engine.Disk('sdc', 4 * 10**9, 512, 'Tiny Thing', 'USB', True, reason='Too small (at least 4.0 GiB needed)'),
    engine.Disk('sdd', 8 * GiB, 512, 'Ventoy Installer USB', 'USB', True, reason='This is the installation medium'),
]


def key(w, name, ch='', shift=False, ctrl=False):
    return w.handle(('key', name, ch, (shift, ctrl, False)))


def typ(w, s):
    for c in s:
        key(w, 'char', c)


def shot(w, name):
    w.frame()
    save_png(w.scr, '%s/%s.png' % (OUT, name))


def click_id(w, wid):
    for (i, (x, y, ww, hh), kind, en, obj, x0) in w.ui.prev:
        if i == wid:
            cx, cy = x + ww // 2, y + hh // 2
            w.handle(('move', cx, cy))
            w.frame()
            w.handle(('down', 1))
            w.handle(('up', 1))
            return True
    raise AssertionError('widget %s not on screen' % wid)


class FakeInstaller:
    created = []

    def __init__(self, cfg, env, progress):
        FakeInstaller.created.append(cfg)
        self.cfg, self.progress = cfg, progress

    def run(self):
        steps = engine.STEPS
        for s in range(len(steps)):
            for f in (0.0, 0.5, 1.0):
                self.progress(s, f, 'working on %s' % steps[s][0].lower())
                time.sleep(0.02)
            if self.cfg.username == 'boom' and s == 2:
                raise engine.InstallError('Copying the system failed (tar exit status 2).', 'tar: something broke')


def build(w_px, h_px, power):
    scr = fbgui.Screen.virtual(w_px, h_px)
    fonts = fbgui.FontSet(os.path.join(HERE, '..', 'installer', 'font.bin'))
    scale = max([s for s in fbgui.SCALES if w_px >= 800 * s and h_px >= 560 * s] or [1.0])
    info = engine.load_media_info('/tmp/fakemedia')
    env = engine.Env(media='/tmp/fakemedia', target='/tmp/none', uefi=True)
    wiz = installer.Wizard(scr, fonts, scale, env, info, disks_fn=FAKE_DISKS, power_fn=power)
    drawn = []
    orig = scr.text

    def rec(x, y, s, *a):
        drawn.append(s)
        return orig(x, y, s, *a)
    scr.text = rec
    return wiz, drawn, scale


def run(w_px, h_px, tag):
    print('\n===== %dx%d =====' % (w_px, h_px))
    powered = []
    wiz, drawn, scale = build(w_px, h_px, powered.append)
    engine.Installer = FakeInstaller
    FakeInstaller.created.clear()
    check(wiz.sysname == 'ouatos GNU/Linux' and wiz.brand == 'ouatos', 'names derived from os-release: %r / %r (scale %.2f)' % (wiz.sysname, wiz.brand, scale))
    shot(wiz, tag + '_1_welcome')
    key(wiz, 'enter'); wiz.frame()
    check(wiz.page == 'disk', 'Enter on Welcome -> disk page')
    check(wiz.sel is None, 'two usable disks -> nothing pre-selected')
    shot(wiz, tag + '_2_disk_none')
    check(wiz.ui.focus == 'disk:nvme0n1', 'keyboard focus starts on the first usable disk')
    check(not any(n == wiz.sel for n in ('sdc', 'sdd')), 'unusable disks are not selected')
    key(wiz, 'down'); wiz.frame()
    check(wiz.sel == 'nvme0n1', 'Down selects first usable disk')
    key(wiz, 'down'); key(wiz, 'down'); key(wiz, 'down'); wiz.frame()
    check(wiz.sel == 'sdb', 'Down skips unusable disks, stops at last usable (%s)' % wiz.sel)
    key(wiz, 'up'); key(wiz, 'up'); wiz.frame()
    check(wiz.sel == 'nvme0n1', 'Up goes back')
    shot(wiz, tag + '_3_disk_selected')
    click_id(wiz, 'disk:sda'); wiz.frame()
    check(wiz.sel == 'sda', 'mouse click selects a disk')
    if any(i == 'disk:sdd' for i, *_ in wiz.ui.prev):
        click_id(wiz, 'disk:sdd'); wiz.frame()
        check(wiz.sel == 'sda', 'mouse click on the installation medium is ignored')
    else:
        check(wiz.visible_rows < 5, 'small screen: list scrolls (%d rows visible)' % wiz.visible_rows)
    click_id(wiz, 'next'); wiz.frame()
    check(wiz.page == 'layout', 'mouse click on Next -> layout page')
    shot(wiz, tag + '_4_layout')
    check(wiz.swap_on, 'swap defaults on for a big disk')
    click_id(wiz, 'swap'); wiz.frame()
    check(not wiz.swap_on, 'checkbox toggles swap off')
    click_id(wiz, 'swap'); wiz.frame()
    click_id(wiz, 'back'); wiz.frame()
    check(wiz.page == 'disk', 'Back button returns to disk page')
    key(wiz, 'esc'); wiz.frame()
    check(wiz.page == 'welcome', 'Esc goes back again')
    key(wiz, 'enter'); wiz.frame()
    check(wiz.page == 'disk' and wiz.ui.focus == 'next', 'returning to disk page with a choice focuses Next')
    key(wiz, 'enter'); wiz.frame()
    check(wiz.page == 'layout', 'Enter continues when a disk is chosen')
    key(wiz, 'enter'); wiz.frame()
    check(wiz.page == 'computer', 'layout -> computer')
    check(wiz.host.text == 'ouatos', 'hostname defaults to "ouatos"')
    shot(wiz, tag + '_5_computer')
    for _ in range(10):
        key(wiz, 'backspace')
    typ(wiz, 'My PC!')
    key(wiz, 'enter'); wiz.frame()
    check(wiz.page == 'computer' and wiz.err, 'invalid hostname rejected: ' + wiz.err)
    shot(wiz, tag + '_5b_computer_error')
    for _ in range(10):
        key(wiz, 'backspace')
    typ(wiz, 'MyPC'); key(wiz, 'enter'); wiz.frame()
    check(wiz.page == 'root' and wiz.host.text == 'mypc', 'hostname lower-cased and accepted -> root password page')
    shot(wiz, tag + '_5c_root')
    click_id(wiz, 'next'); wiz.frame()
    check(wiz.page == 'root' and wiz.err, 'empty root password rejected: ' + wiz.err)
    click_id(wiz, 'rpw'); typ(wiz, 'r00tpass'); click_id(wiz, 'rpw2'); typ(wiz, 'r00tpas'); wiz.frame()
    shot(wiz, tag + '_5d_root_mismatch')
    click_id(wiz, 'next'); wiz.frame()
    check(wiz.page == 'root' and 'match' in wiz.err, 'mismatching root passwords rejected: ' + wiz.err)
    click_id(wiz, 'rpw2'); typ(wiz, 's'); click_id(wiz, 'showpw'); wiz.frame()
    check(wiz.show_pw, 'Show password works on the root page too')
    click_id(wiz, 'showpw'); click_id(wiz, 'rpw2'); key(wiz, 'enter'); wiz.frame()
    check(wiz.page == 'account', 'valid root password + Enter in the last field -> account page')
    key(wiz, 'esc'); wiz.frame(); check(wiz.page == 'root' and wiz.rpw.text == 'r00tpass', 'Esc returns to the root page with the password kept')
    key(wiz, 'enter') if False else click_id(wiz, 'next'); wiz.frame()
    check(wiz.page == 'account', 'Next again -> account page')
    click_id(wiz, 'user'); typ(wiz, 'root')
    click_id(wiz, 'pw'); typ(wiz, 'hunter22')
    click_id(wiz, 'pw2'); typ(wiz, 'hunter2'); wiz.frame()
    shot(wiz, tag + '_6_account_mismatch')
    check(wiz.ui.focus == 'pw2', 'clicking a field focuses it')
    key(wiz, 'tab'); check(wiz.ui.focus == 'showpw', 'Tab moves focus on to the next widget')
    click_id(wiz, 'next'); wiz.frame()
    check(wiz.page == 'account' and 'reserved' in wiz.err, 'reserved username "root" rejected: ' + wiz.err)
    click_id(wiz, 'user')
    for _ in range(6):
        key(wiz, 'backspace')
    typ(wiz, 'Alice'); click_id(wiz, 'next'); wiz.frame()
    check(wiz.page == 'account' and wiz.err, 'uppercase username rejected: ' + wiz.err)
    click_id(wiz, 'user')
    for _ in range(6):
        key(wiz, 'backspace')
    typ(wiz, 'alice')
    click_id(wiz, 'next'); wiz.frame()
    check(wiz.page == 'account' and 'match' in wiz.err, 'mismatching passwords rejected: ' + wiz.err)
    click_id(wiz, 'pw2'); typ(wiz, '2')
    click_id(wiz, 'showpw'); wiz.frame()
    check(wiz.show_pw, 'Show password checkbox works')
    shot(wiz, tag + '_6b_account_filled')
    click_id(wiz, 'pw2'); key(wiz, 'enter'); wiz.frame()          # Enter in the last field = Next
    check(wiz.page == 'review', 'valid account -> review page (err=%r)' % wiz.err)
    shot(wiz, tag + '_7_review')
    click_id(wiz, 'install'); wiz.frame()
    check(wiz.page == 'install', 'Install now starts the installation')
    time.sleep(0.12); wiz.tick(); shot(wiz, tag + '_8_install')
    deadline = time.time() + 5
    while wiz.page == 'install' and time.time() < deadline:
        time.sleep(0.05); wiz.tick()
    wiz.frame()
    check(wiz.page == 'done', 'progress completes -> done page')
    shot(wiz, tag + '_9_done')
    cfg = FakeInstaller.created[-1]
    check(cfg.root_password == 'r00tpass', 'root password handed to the installer')
    check((cfg.hostname, cfg.username, cfg.password, cfg.disk.name) == ('mypc', 'alice', 'hunter22', 'sda'),
          'config handed to installer: host=%s user=%s disk=%s swap=%s' % (cfg.hostname, cfg.username, cfg.disk.name, engine.fmt_size(cfg.swap_bytes)))
    check(cfg.swap_bytes > 0, 'swap size included')
    click_id(wiz, 'reboot')
    check(powered == ['reboot'], 'Restart button calls reboot')
    # failure path
    wiz.goto('welcome'); wiz.sel = 'sda'; wiz.user.text = 'boom'; wiz.pw.text = wiz.pw2.text = 'x'; wiz.host.text = 'h'; wiz.rpw.text = wiz.rpw2.text = 'y'
    open(engine.LOG_FILE, 'a').write('[00:00:00] $ tar -x ...\n[00:00:01] tar: something broke\n')
    wiz.start_install()
    deadline = time.time() + 5
    while wiz.page == 'install' and time.time() < deadline:
        time.sleep(0.05); wiz.tick()
    wiz.frame()
    check(wiz.page == 'error', 'failed install -> error page')
    shot(wiz, tag + '_10_error')
    click_id(wiz, 'restart_wizard'); wiz.frame()
    check(wiz.page == 'welcome', 'Start over returns to welcome')
    # branding rule across everything drawn
    allt = ' '.join(drawn)
    bad = re.findall(r'(?i)ouatos', allt)
    check(bad and all(b == 'ouatos' for b in bad), 'every "ouatos" drawn is lowercase (%d occurrences)' % len(bad))
    check('GNU/Linux' in allt and not re.search(r'OUATOS|Ouatos', allt), 'no OUATOS / Ouatos anywhere on screen')
    known = set(wiz.ui.fonts.meta[next(iter(wiz.ui.fonts.meta))]['glyphs'])
    missing = sorted({c for c in allt if c not in known})
    check(not missing, 'every character drawn exists in the glyph atlas (missing: %r)' % missing)


if __name__ == '__main__':
    open(engine.LOG_FILE, 'w').close()
    run(1024, 768, 'a')
    run(800, 600, 'b')
    run(1920, 1080, 'c')
    print('\nRESULT:', 'ALL GUI TESTS PASSED' if all(results) else 'SOME GUI TESTS FAILED (%d/%d)' % (results.count(False), len(results)))
    sys.exit(0 if all(results) else 1)
