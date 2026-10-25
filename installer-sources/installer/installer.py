#!/usr/bin/env python3
"""ouatos graphical installer (framebuffer) with a text-mode fallback."""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
from engine import (BRAND, GiB, MiB, Config, Env, InstallError, brand_name, check_hostname, check_password,
                    check_username, default_hostname, default_swap_bytes, fmt_size, list_disks, plan_layout,
                    system_name)

STEP_TITLES = ['Welcome', 'Disk', 'Partitions', 'Computer', 'Root password', 'Account', 'Review', 'Install']
PAGES = ['welcome', 'disk', 'layout', 'computer', 'root', 'account', 'review', 'install']


class Wizard:
    def __init__(self, scr, fonts, scale, env, info, disks_fn=None, power_fn=None, input_has_pointer=True):
        import fbgui
        self.g = fbgui
        self.scr, self.env, self.info = scr, env, info
        self.ui = fbgui.UI(scr, fonts, scale)
        self.osr = info['os_release']
        self.sysname = system_name(self.osr)                    # e.g. "ouatos GNU/Linux"
        self.brand = brand_name(self.osr)                       # e.g. "ouatos"
        self.disks_fn = disks_fn or (lambda: list_disks(env.media))
        self.power_fn = power_fn or engine.power
        self.uefi = env.uefi
        self.page = 'welcome'
        self.disks, self.sel, self.scroll = [], None, 0
        self.swap_on = False
        self.host = fbgui.Field(default_hostname(self.osr))
        self.full, self.user = fbgui.Field(''), fbgui.Field('')
        self.pw, self.pw2 = fbgui.Field(''), fbgui.Field('')
        self.rpw, self.rpw2 = fbgui.Field(''), fbgui.Field('')      # root password + confirmation
        self.show_pw = False
        self.err = ''
        self.prog = dict(step=0, frac=0.0, msg='', done=False, error=None)
        self.t_disks = 0.0
        self.last_draw = 0.0
        self.has_pointer = input_has_pointer
        self.ui.focus = 'next'
        self.refresh_disks()

    # ------------------------------------------------------------------ helpers
    def selected_disk(self):
        return next((d for d in self.disks if d.name == self.sel), None)

    def refresh_disks(self):
        try:
            self.disks = self.disks_fn()
        except Exception as e:                                  # never crash the UI over a sysfs hiccup
            engine.log('disk scan failed: %r' % (e,))
            self.disks = []
        if self.sel and not any(d.name == self.sel and d.usable for d in self.disks):
            self.sel = None
        if self.sel is None:
            usable = [d for d in self.disks if d.usable]
            if len(usable) == 1:
                self.sel = usable[0].name
        self.t_disks = time.time()

    def swap_bytes(self):
        d = self.selected_disk()
        return default_swap_bytes(max(d.size, 24 * GiB)) if (d and self.swap_on) else 0

    def swap_possible(self):
        d = self.selected_disk()
        return bool(d and d.size >= 12 * GiB)

    def plan(self):
        d = self.selected_disk()
        return plan_layout(d.size, d.lbs, self.uefi, self.swap_bytes()) if d else []

    def goto(self, page):
        self.page, self.err = page, ''
        self.ui.focus = {'welcome': 'next', 'disk': 'next', 'layout': 'next', 'computer': 'host',
                         'root': 'rpw', 'account': 'user', 'review': 'install', 'done': 'reboot', 'error': 'reboot'}.get(page)
        self.ui.hover = self.ui.pressed = None
        if page == 'disk':
            self.refresh_disks()
            first = next((d.name for d in self.disks if d.usable), None)
            self.ui.focus = 'next' if self.sel else ('disk:' + first if first else 'refresh')
        if page == 'layout':
            self.swap_on = self.swap_possible() and self.selected_disk().size >= 24 * GiB

    # ------------------------------------------------------------------ navigation
    def go_next(self):
        p = self.page
        if p == 'welcome':
            self.goto('disk')
        elif p == 'disk':
            if not self.selected_disk():
                self.err = 'Please choose the disk to install on.'
                return True
            self.goto('layout')
        elif p == 'layout':
            self.goto('computer')
        elif p == 'computer':
            h = self.host.text.strip().lower()
            e = check_hostname(h)
            if e:
                self.err = e
                return True
            self.host.text, self.host.cur = h, len(h)
            self.goto('root')
        elif p == 'root':
            e = check_password(self.rpw.text, self.rpw2.text)
            if e:
                self.err = e
                self.ui.focus = 'rpw' if not self.rpw.text else 'rpw2'
                return True
            self.goto('account')
        elif p == 'account':
            e = (check_username(self.user.text, self.info.get('system_users', [])) or
                 check_password(self.pw.text, self.pw2.text))
            if e:
                self.err = e
                self.ui.focus = 'user' if check_username(self.user.text, self.info.get('system_users', [])) else (
                    'pw' if not self.pw.text else 'pw2')
                return True
            self.goto('review')
        elif p == 'review':
            self.start_install()
        return True

    def go_back(self):
        i = PAGES.index(self.page) if self.page in PAGES else 0
        if self.page in ('install', 'done') or i == 0:
            return False
        self.goto(PAGES[i - 1])
        return True

    def activate(self, wid):
        if wid == 'next':
            return self.go_next()
        if wid == 'back':
            return self.go_back()
        if wid == 'install':
            return self.go_next()
        if wid == 'poweroff':
            self.power_fn('poweroff')
        elif wid == 'reboot':
            self.power_fn('reboot')
        elif wid == 'restart_wizard':
            self.goto('welcome')
        elif wid == 'refresh':
            self.refresh_disks()
        elif wid == 'swap':
            if self.swap_possible():
                self.swap_on = not self.swap_on
        elif wid == 'showpw':
            self.show_pw = not self.show_pw
        elif wid.startswith('disk:'):
            name = wid[5:]
            d = next((x for x in self.disks if x.name == name), None)
            if d and d.usable:
                if self.sel == name:
                    return self.go_next()
                self.sel, self.err = name, ''
        return True

    # ------------------------------------------------------------------ install worker
    def start_install(self):
        d = self.selected_disk()
        cfg = Config(d, self.host.text.strip(), self.user.text, self.full.text, self.pw.text, self.swap_bytes(),
                     root_password=self.rpw.text)
        self.prog = dict(step=0, frac=0.0, msg='Starting…', done=False, error=None)
        self.page = 'install'
        self.ui.focus = None

        def cb(step, frac, msg):
            self.prog.update(step=step, frac=frac, msg=msg)

        def work():
            try:
                engine.Installer(cfg, self.env, cb).run()
                self.prog['done'] = True
            except InstallError as e:
                self.prog['error'] = (str(e), getattr(e, 'detail', ''))
            except Exception as e:
                self.prog['error'] = ('Unexpected error: %s' % e, '')
        threading.Thread(target=work, daemon=True).start()

    def overall(self):
        steps = engine.STEPS
        total = float(sum(w for _, w in steps))
        done = sum(w for _, w in steps[:self.prog['step']]) + steps[self.prog['step']][1] * self.prog['frac']
        return min(1.0, done / total)

    def poll_worker(self):
        if self.page == 'install':
            if self.prog['error']:
                self.page, self.ui.focus = 'error', 'reboot'
                return True
            if self.prog['done']:
                self.page, self.ui.focus = 'done', 'reboot'
                return True
        return False

    # ------------------------------------------------------------------ events
    def handle(self, ev):
        """Returns True when the screen needs to be redrawn."""
        kind = ev[0]
        ui = self.ui
        if kind == 'move':
            self.scr.set_cursor(ev[1], ev[2])
            self.mx, self.my = ev[1], ev[2]
            return ui.mouse_move(ev[1], ev[2])
        if kind == 'down':
            return ui.mouse_down(getattr(self, 'mx', 0), getattr(self, 'my', 0))
        if kind == 'up':
            act = ui.mouse_up(getattr(self, 'mx', 0), getattr(self, 'my', 0))
            return self.activate(act) if act else True
        if kind == 'scroll':
            if self.page == 'disk':
                self.scroll = max(0, self.scroll + ev[1])
                return True
            return False
        if kind == 'key':
            return self.on_key(ev[1], ev[2], ev[3])
        return False

    def on_key(self, name, ch, mods):
        ui, p = self.ui, self.page
        if p == 'install':
            return False
        fw = ui.focused_widget()
        fkind = fw[2] if fw else None
        if fkind == 'field':
            st = fw[4][0]
            if name == 'tab':
                ui.focus_step(-1 if mods[0] else 1)
                return True
            if name == 'enter':
                order = [w[0] for w in ui.prev if w[2] == 'field']
                if fw[0] == order[-1]:
                    return self.go_next()
                ui.focus_step(1)
                return True
            if name in ('up', 'down'):
                ui.focus_step(-1 if name == 'up' else 1)
                return True
            if name == 'esc':
                return self.go_back()
            if st.key(name, ch, mods):
                self.err = ''
                return True
            return False
        if name == 'tab':
            ui.focus_step(-1 if mods[0] else 1)
            return True
        if name == 'esc':
            return self.go_back()
        if p == 'disk' and name in ('up', 'down', 'pageup', 'pagedown', 'home', 'end'):
            return self.move_selection(name)
        if name in ('up', 'left'):
            ui.focus_step(-1)
            return True
        if name in ('down', 'right'):
            ui.focus_step(1)
            return True
        if name == 'enter' or (name == 'char' and ch == ' '):
            if fw:
                return self.activate(fw[0])
            return self.go_next()
        return False

    def move_selection(self, name):
        usable = [d.name for d in self.disks if d.usable]
        if not usable:
            return False
        i = usable.index(self.sel) if self.sel in usable else -1
        step = {'up': -1, 'down': 1, 'pageup': -3, 'pagedown': 3}.get(name)
        i = 0 if name == 'home' else len(usable) - 1 if name == 'end' else min(max(i + step, 0), len(usable) - 1)
        self.sel, self.err = usable[i], ''
        idx = [d.name for d in self.disks].index(self.sel)
        if idx < self.scroll:
            self.scroll = idx
        elif idx >= self.scroll + self.visible_rows:
            self.scroll = idx - self.visible_rows + 1
        return True

    visible_rows = 4

    def tick(self):
        """Periodic work. Returns True if a redraw is due."""
        now, redraw = time.time(), False
        if self.poll_worker():
            return True
        if self.page == 'disk' and now - self.t_disks > 2.5:
            before = [(d.name, d.size, d.reason) for d in self.disks]
            self.refresh_disks()
            redraw |= before != [(d.name, d.size, d.reason) for d in self.disks]
        fw = self.ui.focused_widget()
        if fw and fw[2] == 'field':
            redraw |= int(now * 2) != int(self.last_draw * 2)        # caret blink
        if self.page == 'install':
            redraw |= now - self.last_draw > 0.2
        return redraw

    # ------------------------------------------------------------------ drawing
    def frame(self):
        self.draw()
        self.scr.flush()
        self.last_draw = time.time()

    def draw(self):
        ui, scr, S = self.ui, self.scr, self.ui.S
        g = self.g
        W, H = scr.w, scr.h
        ui.begin(g.BG)
        sw = S(236)
        scr.fill(0, 0, sw, H, g.SIDEBAR)
        ui.text(S(28), S(26), self.brand, 'h1', g.ACCENT_HI, g.SIDEBAR)
        rest = self.sysname[len(self.brand):].strip() if self.sysname.startswith(self.brand) else ''
        ui.text(S(28), S(26) + S(42), (rest + ' installer').strip(), 'small', g.MUTED, g.SIDEBAR)
        cur = PAGES.index(self.page) if self.page in PAGES else (len(PAGES) if self.page == 'done' else len(PAGES) - 1)
        for i, t in enumerate(STEP_TITLES):
            y = S(130) + i * S(40)
            done, now = i < cur, (i == cur and self.page not in ('done',))
            col = g.OK if done else g.ACCENT_HI if now else g.MUTED
            r = S(11)
            f = ui.font('small')
            if done:
                scr.rrect(S(28), y, 2 * r, 2 * r, r, g.OK, g.SIDEBAR)
                lab, lbg, lfg = '✓', g.OK, g.WHITE
            else:
                ring = g.ACCENT_HI if now else g.BORDER
                scr.rrect(S(28), y, 2 * r, 2 * r, r, ring, g.SIDEBAR)
                scr.rrect(S(28) + 2, y + 2, 2 * r - 4, 2 * r - 4, r - 2, g.SIDEBAR, ring, overlay=True)
                lab, lbg, lfg = str(i + 1), g.SIDEBAR, ring if now else g.MUTED
            scr.text(S(28) + r - f.width(lab) // 2, y + r - f.height // 2, lab, f, lfg, lbg)
            ui.text(S(64), y + r - ui.font('body').height // 2, t, 'bodyb' if now else 'body',
                    g.TEXT if now else (g.MUTED if not done else g.TEXT), g.SIDEBAR)
        x0 = sw + S(44)
        cw = min(W - sw - S(88), S(700))
        getattr(self, 'draw_' + self.page)(x0, S(36), cw)
        ui.end()

    # ----- shared chrome
    def title(self, x, y, w, title, sub=''):
        ui, g = self.ui, self.g
        ui.text(x, y, title, 'h1', g.TEXT, g.BG)
        y += ui.lineh('h1') + ui.S(2)
        if sub:
            y = ui.paragraph(x, y, sub, w, 'body', g.MUTED, g.BG) + ui.S(8)
        return y

    def btn_y(self):
        return self.scr.h - self.ui.S(96)

    def nav(self, x, w, next_label='Next', back=True, next_enabled=True, kind='primary', next_id='next'):
        ui, g, S = self.ui, self.g, self.ui.S
        H = self.scr.h
        by = self.btn_y()
        bw, bh = S(140), S(44)
        ui.button(next_id, x + w - bw, by, bw, bh, next_label, kind, next_enabled)
        if back:
            ui.button('back', x + w - 2 * bw - S(14), by, bw, bh, 'Back', 'normal')
        hint = 'Tab: move between fields   ·   Enter: confirm   ·   Esc: go back' if not self.has_pointer else 'Mouse or Tab: select   ·   Enter: confirm   ·   Esc: go back'
        ui.text(x, H - S(34), hint, 'small', g.MUTED, g.BG)
        if getattr(self, 'pointer_names', None):
            ui.text(x, H - S(34) + ui.lineh('small'), ui.ellipsize('Pointer: ' + ', '.join(self.pointer_names), 'small', w), 'small', g.MUTED, g.BG)
        elif self.has_pointer is False:
            ui.text(x, H - S(34) + ui.lineh('small'), 'No mouse detected: use the keyboard', 'small', g.MUTED, g.BG)

    def error_line(self, x, y, w):
        if self.err:
            ui, g = self.ui, self.g
            return ui.paragraph(x, y, '⚠ ' if False else self.err, w, 'body', g.DANGER, g.CARD)
        return y

    # ----- pages
    def draw_welcome(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        y = self.title(x, y, w, 'Install ' + self.sysname,
                       'This wizard installs %s on your computer. It only takes a few minutes.' % self.sysname)
        ui.card(x, y, w, S(166))
        yy = y + S(18)
        for t in ('Choose the disk to install on; its contents will be erased',
                  'Partitions are created automatically',
                  'You pick a computer name and set the root password',
                  'You create your own user account, which can administer the system with sudo'):
            ui.text(x + S(20), yy, '•', 'body', g.ACCENT_HI, g.CARD)
            ui.text(x + S(40), yy, t, 'body', g.TEXT, g.CARD)
            yy += S(34)
        y += S(166) + S(18)
        mem = fmt_size(engine.ram_bytes())
        ui.text(x, y, 'Firmware: %s     Memory: %s     Keyboard: US layout' % ('UEFI' if self.uefi else 'BIOS (legacy)', mem),
                'small', g.MUTED, g.BG)
        self.nav(x, w, 'Continue', back=False)
        ui.button('poweroff', x + w - S(140) * 2 - S(14), self.btn_y(), S(140), S(44), 'Power off', 'normal')

    def draw_disk(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        y = self.title(x, y, w, 'Choose a disk', 'The selected disk will be erased completely and %s installed on it.' % self.sysname)
        rh, gap = S(66), S(10)
        avail = self.btn_y() - S(64) - y - (S(30) if self.err else 0)
        self.visible_rows = max(1, int(avail // (rh + gap)))
        if not self.disks:
            ui.card(x, y, w, S(90))
            ui.paragraph(x + S(20), y + S(18), 'No disks were found. If your disk is not listed, check that it is connected, then choose "Rescan".', w - S(40), 'body', g.MUTED, g.CARD)
        self.scroll = min(self.scroll, max(0, len(self.disks) - self.visible_rows))
        vis = self.disks[self.scroll:self.scroll + self.visible_rows]
        for d in vis:
            sub = '%s  ·  %s' % (d.path, d.transport) if d.transport else d.path
            if d.reason:
                sub = d.reason
            ui.option('disk:' + d.name, x, y, w, rh, d.title(), sub, d.name == self.sel, d.usable, right=d.size_text())
            y += rh + gap
        if len(self.disks) > self.visible_rows:
            ui.text(x, y - S(4), '%d of %d disks shown — use ↑ ↓ to scroll' % (len(vis), len(self.disks)), 'small', g.MUTED, g.BG)
        if self.err:
            ui.text(x, y + S(4), self.err, 'body', g.DANGER, g.BG)
        self.nav(x, w, 'Next', next_enabled=bool(self.sel))
        ui.button('refresh', x, self.btn_y() + S(3), S(120), S(38), 'Rescan', 'normal')

    def draw_layout(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        d = self.selected_disk()
        y = self.title(x, y, w, 'Partitions', 'The disk is set up automatically using a GPT partition table (%s boot).' % ('UEFI' if self.uefi else 'BIOS'))
        plan = self.plan()
        total = sum(p.last - p.first + 1 for p in plan)
        col = {'esp': g.WARN, 'bios': g.WARN, 'root': g.ACCENT, 'swap': g.OK}
        bar_h = S(26)
        card_h = S(18) + bar_h + S(16) + S(26) + S(28) * len(plan) + S(12)
        ui.card(x, y, w, card_h)
        bx, by, bw = x + S(20), y + S(18), w - S(40)
        g_scr = self.scr
        g_scr.rrect(bx, by, bw, bar_h, S(7), g.BORDER, g.CARD)
        cx, minw = bx, S(12)
        sizes = [max((p.last - p.first + 1) / total * bw, minw) for p in plan]
        scale = bw / sum(sizes)
        for p, sz in zip(plan, sizes):
            sz = int(sz * scale)
            g_scr.fill(cx + 1, by + 2, max(sz - 2, 1), bar_h - 4, col[p.kind])
            cx += sz
        ty = by + bar_h + S(16)
        names = {'esp': 'EFI System Partition', 'bios': 'BIOS boot partition', 'root': '%s system' % self.brand, 'swap': 'Swap space'}
        mounts = {'esp': '/boot/efi', 'bios': '—', 'root': '/', 'swap': '—'}
        fsn = {'esp': 'FAT32', 'bios': '—', 'root': 'ext4', 'swap': 'swap'}
        cols = [x + S(44), x + int(w * 0.46), x + int(w * 0.63), x + int(w * 0.77)]
        for cxx, head in zip(cols, ('Partition', 'Size', 'Type', 'Mount')):
            ui.text(cxx, ty, head, 'small', g.MUTED, g.CARD)
        ty += S(26)
        for p in plan:
            g_scr.rrect(x + S(20), ty + S(4), S(14), S(14), S(4), col[p.kind], g.CARD)
            for cxx, val in zip(cols, (names[p.kind], fmt_size((p.last - p.first + 1) * d.lbs), fsn[p.kind], mounts[p.kind])):
                ui.text(cxx, ty, val, 'body', g.TEXT, g.CARD)
            ty += S(28)
        y += card_h + S(14)
        ui.card(x, y, w, S(56))
        ok = self.swap_possible()
        ui.checkbox('swap', x + S(20), y + S(18), 'Create a swap partition' + ('' if ok else ' (disk too small)'), self.swap_on and ok, ok)
        y += S(56) + S(16)
        ui.paragraph(x, y, 'Everything on %s (%s, %s) will be erased.' % (d.title(), d.path, d.size_text()), w, 'bodyb', g.DANGER, g.BG)
        self.nav(x, w, 'Next')

    def draw_computer(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        y = self.title(x, y, w, 'Name this computer',
                       'The computer name identifies this machine on a network. Use lowercase letters, digits and hyphens.')
        ui.card(x, y, w, S(150))
        ui.field('host', x + S(20), y + S(18), w - S(40), self.host, label='Computer name', placeholder=default_hostname(self.osr),
                 error=bool(self.err))
        ui.text(x + S(20), y + S(18) + S(78), 'The default is "%s". You can change it later in /etc/hostname.' % default_hostname(self.osr), 'small', g.MUTED, g.CARD)
        if self.err:
            ui.text(x + S(20), y + S(18) + S(100), self.err, 'body', g.DANGER, g.CARD)
        self.nav(x, w, 'Next')

    def draw_root(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        y = self.title(x, y, w, 'Set the root password',
                       'root is the system administrator account. Choose a strong password and keep it somewhere safe.')
        fw = min(w - S(40), S(440))
        ui.card(x, y, w, S(250) + (S(26) if self.err else 0))
        fx, fy = x + S(20), y + S(16)
        h1 = ui.field('rpw', fx, fy, fw, self.rpw, password=not self.show_pw, label='Root password',
                      error=bool(self.err) and ui.focus == 'rpw')
        fy += h1 + S(14)
        h2 = ui.field('rpw2', fx, fy, fw, self.rpw2, password=not self.show_pw, label='Confirm root password',
                      error=bool(self.err) and ui.focus == 'rpw2')
        fy += h2 + S(14)
        ui.checkbox('showpw', fx, fy, 'Show password', self.show_pw)
        fy += S(34)
        if self.err:
            ui.text(fx, fy, self.err, 'body', g.DANGER, g.CARD)
        elif self.rpw2.text and self.rpw.text != self.rpw2.text:
            ui.text(fx, fy, 'The two passwords do not match yet.', 'body', g.WARN, g.CARD)
        elif self.rpw.text and len(self.rpw.text) < 8:
            ui.text(fx, fy, 'Tip: a longer password is safer (8 or more characters).', 'small', g.MUTED, g.CARD)
        self.nav(x, w, 'Next')

    def draw_account(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        y = self.title(x, y, w, 'Create your account', 'This is the account you will use every day. It can administer the system with sudo.')
        ch = S(20) + S(240)
        ui.card(x, y, w, S(268) + (S(26) if self.err else 0))
        fw = (w - S(60)) // 2
        fx, fy = x + S(20), y + S(16)
        uerr = self.err and self.ui.focus == 'user' and True
        h1 = ui.field('full', fx, fy, fw, self.full, label='Your name (optional)')
        ui.field('user', fx + fw + S(20), fy, fw, self.user, label='Username', error=bool(self.err) and ui.focus == 'user')
        fy += h1 + S(14)
        h2 = ui.field('pw', fx, fy, fw, self.pw, password=not self.show_pw, label='Password', error=bool(self.err) and ui.focus == 'pw')
        ui.field('pw2', fx + fw + S(20), fy, fw, self.pw2, password=not self.show_pw, label='Confirm password', error=bool(self.err) and ui.focus == 'pw2')
        fy += h2 + S(14)
        ui.checkbox('showpw', fx, fy, 'Show password', self.show_pw)
        fy += S(34)
        if self.err:
            ui.text(fx, fy, self.err, 'body', g.DANGER, g.CARD)
        elif self.pw2.text and self.pw.text != self.pw2.text:
            ui.text(fx, fy, 'The two passwords do not match yet.', 'body', g.WARN, g.CARD)
        elif self.pw.text and len(self.pw.text) < 8:
            ui.text(fx, fy, 'Tip: a longer password is safer (8 or more characters).', 'small', g.MUTED, g.CARD)
        self.nav(x, w, 'Next')

    def draw_review(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        d = self.selected_disk()
        y = self.title(x, y, w, 'Ready to install', 'Check the summary. Nothing has been changed on your disk yet.')
        rows = [('Install', self.sysname),
                ('Disk', '%s  (%s, %s)' % (d.title(), d.path, d.size_text())),
                ('Partitions', 'GPT · %s · ext4 root%s' % ('EFI system partition' if self.uefi else 'BIOS boot partition',
                                                         ' · swap %s' % fmt_size(self.swap_bytes()) if self.swap_bytes() else '')),
                ('Computer name', self.host.text),
                ('Root account', 'Password set (%d characters)' % len(self.rpw.text)),
                ('User', '%s  (administrator, uses sudo)' % self.user.text)]
        vw = w - S(190)
        wrapped = [(k, ui.wrap(v, 'bodyb', vw)) for k, v in rows]
        heights = [max(S(36), len(ls) * ui.lineh('bodyb') + S(10)) for _, ls in wrapped]
        card_h = sum(heights) + S(24)
        ui.card(x, y, w, card_h)
        yy = y + S(16)
        for (k, ls), rh in zip(wrapped, heights):
            ui.text(x + S(20), yy, k, 'body', g.MUTED, g.CARD)
            for i, ln in enumerate(ls):
                ui.text(x + S(170), yy + i * ui.lineh('bodyb'), ln, 'bodyb', g.TEXT, g.CARD)
            yy += rh
        y += card_h + S(18)
        ui.paragraph(x, y, '⚠  Installing erases everything on %s. This cannot be undone.' % d.path, w, 'bodyb', g.DANGER, g.BG)
        self.nav(x, w, 'Install now', kind='danger', next_id='install')

    def draw_install(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        y = self.title(x, y, w, 'Installing %s' % self.sysname, 'Please keep the computer powered on. This can take several minutes.')
        ui.card(x, y, w, S(40) * len(engine.STEPS) + S(24))
        yy = y + S(18)
        spin = '|/-\\'[int(time.time() * 6) % 4]
        for i, (t, _) in enumerate(engine.STEPS):
            st = self.prog['step']
            if i < st:
                ui.text(x + S(20), yy, '✓', 'bodyb', g.OK, g.CARD)
                ui.text(x + S(52), yy, t, 'body', g.TEXT, g.CARD)
            elif i == st:
                ui.text(x + S(20), yy, '▸', 'bodyb', g.ACCENT_HI, g.CARD)
                ui.text(x + S(52), yy, t + '  ' + spin, 'bodyb', g.TEXT, g.CARD)
            else:
                ui.text(x + S(20), yy, '○', 'body', g.MUTED, g.CARD)
                ui.text(x + S(52), yy, t, 'body', g.MUTED, g.CARD)
            yy += S(40)
        y += S(40) * len(engine.STEPS) + S(24) + S(24)
        ui.progress(x, y, w, S(16), self.overall())
        y += S(28)
        ui.text(x, y, self.prog['msg'], 'body', g.MUTED, g.BG)
        pct = '%d%%' % int(self.overall() * 100)
        ui.text(x + w - ui.width(pct, 'bodyb'), y, pct, 'bodyb', g.TEXT, g.BG)

    def draw_done(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        r = S(30)
        self.scr.rrect(x, y, 2 * r, 2 * r, r, g.OK, g.BG)
        f = ui.font('h1')
        self.scr.text(x + r - f.width('✓') // 2, y + r - f.height // 2, '✓', f, g.WHITE, g.OK)
        y += 2 * r + S(20)
        y = self.title(x, y, w, '%s is installed' % self.sysname,
                       'Remove the installation medium (disc or USB drive), then restart. You can sign in as "%s".' % self.user.text)
        ui.card(x, y + S(4), w, S(70))
        ui.paragraph(x + S(20), y + S(18), 'The installation log was saved to /var/log/ouatos-install.log on the new system.', w - S(40), 'body', g.MUTED, g.CARD)
        bw, bh, by = S(160), S(44), self.btn_y()
        ui.button('reboot', x + w - bw, by, bw, bh, 'Restart now', 'primary')
        ui.button('poweroff', x + w - 2 * bw - S(14), by, bw, bh, 'Power off', 'normal')

    def draw_error(self, x, y, w):
        ui, g, S = self.ui, self.g, self.ui.S
        msg, detail = self.prog['error'] or ('Unknown error', '')
        y = self.title(x, y, w, 'Installation failed', msg)
        lines = []
        try:
            lines = open(engine.LOG_FILE).read().splitlines()[-12:]
        except OSError:
            lines = (detail or '').splitlines()[-12:]
        lh = ui.lineh('mono')
        ui.card(x, y, w, lh * 12 + S(24))
        yy = y + S(12)
        for ln in lines:
            ui.text(x + S(14), yy, ui.ellipsize(ln, 'mono', w - S(28)), 'mono', g.MUTED, g.CARD)
            yy += lh
        ui.text(x, y + lh * 12 + S(36), 'Full log: %s' % engine.LOG_FILE, 'small', g.MUTED, g.BG)
        bw, bh, by = S(160), S(44), self.btn_y()
        ui.button('reboot', x + w - bw, by, bw, bh, 'Restart', 'primary')
        ui.button('poweroff', x + w - 2 * bw - S(14), by, bw, bh, 'Power off', 'normal')
        ui.button('restart_wizard', x + w - 3 * bw - S(28), by, bw, bh, 'Start over', 'normal')


# ====================================================================== text-mode fallback
class TextFrontend:
    """Plain console installer, used when no framebuffer is available (or with ouatos.text=1)."""

    def __init__(self, env, info):
        self.env, self.info = env, info
        self.osr = info['os_release']

    def ask(self, prompt, default=''):
        s = input('%s%s: ' % (prompt, ' [%s]' % default if default else '')).strip()
        return s or default

    def run(self):
        import getpass
        name = system_name(self.osr)
        print('\n=== Install %s (text mode) ===\n' % name)
        disks = [d for d in list_disks(self.env.media) if d.usable]
        if not disks:
            print('No usable disk found.')
            return 1
        for i, d in enumerate(disks, 1):
            print('  %d) %-40s %s  %s' % (i, d.title(), d.path, d.size_text()))
        while True:
            try:
                disk = disks[int(self.ask('Install on which disk? (number)')) - 1]
                break
            except (ValueError, IndexError):
                print('Please enter one of the numbers above.')
        swap = default_swap_bytes(disk.size) if disk.size >= 24 * GiB else 0
        while True:
            host = self.ask('Computer name', default_hostname(self.osr)).lower()
            e = check_hostname(host)
            if not e:
                break
            print(e)
        while True:
            user = self.ask('Username')
            e = check_username(user, self.info.get('system_users', []))
            if not e:
                break
            print(e)
        while True:
            rpw = getpass.getpass('Root password: ')
            e = check_password(rpw, getpass.getpass('Confirm root password: '))
            if not e:
                break
            print(e)
        full = self.ask('Your name (optional)')
        while True:
            pw = getpass.getpass('Password: ')
            e = check_password(pw, getpass.getpass('Confirm password: '))
            if not e:
                break
            print(e)
        print('\nThis will ERASE %s (%s) and install %s.' % (disk.path, disk.size_text(), name))
        if self.ask('Type YES to continue').strip() != 'YES':
            print('Cancelled.')
            return 1
        last = [None]

        def cb(step, frac, msg):
            key = (step, msg)
            if key != last[0]:
                last[0] = key
                print('[%d/%d] %s - %s' % (step + 1, len(engine.STEPS), engine.STEPS[step][0], msg), flush=True)
        try:
            engine.Installer(Config(disk, host, user, full, pw, swap, root_password=rpw), self.env, cb).run()
        except InstallError as e:
            print('\nInstallation failed: %s\n%s\n(log: %s)' % (e, getattr(e, 'detail', ''), engine.LOG_FILE))
            return 2
        print('\n%s is installed. Remove the installation medium and restart.' % name)
        return 0


# ====================================================================== entry point
def cmdline():
    try:
        return open('/proc/cmdline').read().split()
    except OSError:
        return []


def set_vt_graphics(on):
    """Stop the kernel console drawing over our pixels."""
    try:
        import fcntl
        fd = os.open('/dev/tty0', os.O_RDWR)
        fcntl.ioctl(fd, 0x4B3A, 1 if on else 0)        # KDSETMODE: KD_GRAPHICS / KD_TEXT
        os.close(fd)
    except OSError:
        pass


def run_gui(env, info, fbdev='/dev/fb0'):
    import fbgui
    fonts = fbgui.FontSet(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'font.bin'))
    scr = None
    for _ in range(50):                                   # the framebuffer may appear a moment after boot
        try:
            scr = fbgui.Screen.open_fb(fbdev)
            break
        except OSError:
            time.sleep(0.2)
    if scr is None:
        raise OSError('no usable framebuffer')
    scale = max([s for s in fbgui.SCALES if scr.w >= 800 * s and scr.h >= 560 * s] or [1.0])
    inp = fbgui.EvdevInput(scr.w, scr.h, scale)
    wiz = Wizard(scr, fonts, scale, env, info, input_has_pointer=True)
    scr.cursor_enabled = inp.has_pointer
    wiz.has_pointer = inp.has_pointer
    wiz.pointer_names = inp.pointer_names()
    set_vt_graphics(True)
    try:
        wiz.frame()
        if inp.has_pointer:
            scr.set_cursor(inp.x, inp.y)
        last_assert = time.time()
        while True:
            redraw = False
            for ev in inp.poll(0.1):
                redraw |= bool(wiz.handle(ev))
            names = inp.pointer_names()
            if inp.has_pointer != scr.cursor_enabled or names != wiz.pointer_names:
                first = inp.has_pointer and not scr.cursor_enabled
                scr.cursor_enabled = inp.has_pointer              # show the pointer only when a mouse/tablet/touchpad exists
                wiz.has_pointer, wiz.pointer_names = inp.has_pointer, names
                engine.log('pointing devices: %s' % (names or 'none'))
                redraw = True
                if first:
                    scr._cx, scr._cy = inp.x, inp.y
            redraw |= bool(wiz.tick())
            if redraw:
                wiz.frame()
            if time.time() - last_assert > 0.5:                    # repair the pointer if anything overwrote it
                last_assert = time.time()
                scr.refresh_cursor()
    finally:
        set_vt_graphics(False)



def main(argv):
    args = dict(a.split('=', 1) if '=' in a else (a, '1') for a in argv[1:])
    import media as _media

    if '--media' in args:
        mediasrc = _media.MountedMedia(args['--media'])
    elif '--media-dev' in args:
        mediasrc = _media.RawMedia(args['--media-dev'])
    else:
        mediasrc = _media.open_media()

    env = Env(media=mediasrc, target=args.get('--target', '/mnt/target'))
    engine.log('installation medium: %s' % mediasrc.describe())
    print('Using installation medium: %s' % mediasrc.describe(), flush=True)

    info = engine.load_media_info(mediasrc)

    text = (
        '--text' in args
        or 'ouatos.text=1' in cmdline()
        or os.environ.get('OUATOS_TEXT') == '1'
    )

    if not text:
        try:
            run_gui(env, info, args.get('--fb', '/dev/fb0'))
            return 0
        except OSError as e:
            engine.log('graphical mode unavailable (%s); using text mode' % e)
            print(
                'Graphical mode is unavailable (%s); continuing in text mode.' % e
            )

    rc = TextFrontend(env, info).run()
    if rc == 0:
        engine.power('reboot')
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv))
