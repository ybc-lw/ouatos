"""Ownership / permission policy applied to a freshly extracted system.

Rules implemented here:
  * "/" and everything under it is owned by root:root (the only exception is a user's home directory).
  * /tmp and /var/tmp are root:root with mode 1777 (sticky bit: users cannot delete each other's files).
  * /home/<user> is owned by <user>:<user>  (done by the engine with useradd -U, verified by verify_policy()).
  * /etc/machine-id is initialised with a fresh random ID for every installation.

The OS image is an archive that has lost information: it contains no empty directories (no /dev /proc /sys
/run /tmp /home ...), every entry is owned by 0:0 and its modes are flattened (no setuid bits, /etc/shadow is
0644 ...).  So the installer (1) creates the standard directories, (2) tightens -- never loosens -- the few
security-critical modes, and (3) restores the setuid bit on the handful of programs that cannot work without it.

dpkg/apt additionally need their state directories (info, alternatives, triggers, updates, archives/partial ...),
lock files and executable maintainer scripts; those are created / repaired by repair_dpkg() and checked by
verify_dpkg().  Directories that tar created implicitly (their mode then depends on the process umask) are
repaired by repair_dir_modes().
"""
import glob
import os
import re
import stat
import uuid

# (path relative to /, mode).  Created only when missing; existing directories are left as they are.
STANDARD_DIRS = [
    # top level and mount points
    ('dev', 0o755), ('proc', 0o755), ('sys', 0o755), ('run', 0o755), ('mnt', 0o755), ('media', 0o755),
    ('srv', 0o755), ('opt', 0o755), ('home', 0o755), ('root', 0o700), ('tmp', 0o1777), ('var/tmp', 0o1777),
    # /var
    ('var/log', 0o755), ('var/log/journal', 0o755), ('var/cache', 0o755), ('var/cache/apt', 0o755),
    ('var/lib', 0o755), ('var/lib/misc', 0o755), ('var/lib/grub', 0o755), ('var/lib/sudo', 0o711),
    ('var/lib/dpkg/updates', 0o755), ('var/lib/dpkg/parts', 0o755), ('var/lib/apt/lists', 0o755),
    ('var/lib/apt/mirrors', 0o755), ('var/lib/apt/periodic', 0o755), ('var/spool', 0o755),
    ('var/lib/dpkg/info', 0o755), ('var/lib/dpkg/triggers', 0o755), ('var/lib/dpkg/alternatives', 0o755),
    ('var/lib/apt/lists/partial', 0o700), ('var/cache/apt/archives', 0o755),
    ('var/cache/apt/archives/partial', 0o700), ('var/log/apt', 0o755), ('var/cache/debconf', 0o755),
    ('var/lib/private', 0o700),
    ('etc/alternatives', 0o755), ('etc/dpkg', 0o755), ('etc/apt', 0o755), ('etc/apt/apt.conf.d', 0o755),
    ('etc/apt/trusted.gpg.d', 0o755),
    ('etc/init.d', 0o755), ('etc/rc2.d', 0o755), ('etc/rc3.d', 0o755), ('etc/rc4.d', 0o755),
    ('etc/rc5.d', 0o755), ('etc/profile.d', 0o755), ('etc/skel', 0o755), ('etc/default', 0o755),
    ('usr/share/doc', 0o755), ('usr/lib/dpkg', 0o755),
    ('var/local', 0o755), ('var/opt', 0o755), ('var/mail', 0o755), ('var/backups', 0o755),
    # /usr
    ('usr/games', 0o755), ('usr/src', 0o755), ('usr/local', 0o755), ('usr/local/bin', 0o755),
    ('usr/local/sbin', 0o755), ('usr/local/lib', 0o755), ('usr/local/share', 0o755), ('usr/local/etc', 0o755),
    ('usr/local/include', 0o755), ('usr/local/src', 0o755), ('usr/local/games', 0o755),
    ('usr/local/libexec', 0o755), ('usr/lib/depmod.d', 0o755), ('usr/lib/sasl2', 0o755),
    ('usr/share/dict', 0o755), ('usr/share/info', 0o755), ('usr/share/file/magic', 0o755),
    ('usr/share/initramfs-tools/conf-hooks.d', 0o755), ('usr/share/initramfs-tools/conf.d', 0o755),
    ('usr/share/initramfs-tools/modules.d', 0o755),
    # /etc
    ('etc/opt', 0o755), ('etc/modprobe.d', 0o755), ('etc/depmod.d', 0o755), ('etc/binfmt.d', 0o755),
    ('etc/tmpfiles.d', 0o755), ('etc/udev/rules.d', 0o755), ('etc/udev/hwdb.d', 0o755),
    ('etc/systemd/network', 0o755), ('etc/systemd/user', 0o755), ('etc/ssl/private', 0o700),
    ('etc/ca-certificates', 0o755), ('etc/credstore', 0o700), ('etc/credstore.encrypted', 0o700),
    ('etc/gss', 0o755), ('etc/security/namespace.d', 0o755), ('etc/default/grub.d', 0o755),
    ('etc/kernel/install.d', 0o755), ('etc/dpkg/dpkg.cfg.d', 0o755), ('etc/apt/auth.conf.d', 0o755),
    ('etc/apt/keyrings', 0o755), ('etc/apt/preferences.d', 0o755), ('etc/apt/sources.list.d', 0o755),
    ('etc/initramfs-tools/conf.d', 0o755), ('etc/initramfs-tools/hooks', 0o755),
    ('etc/initramfs-tools/scripts', 0o755),
    ('etc/rc0.d', 0o755), ('etc/rc1.d', 0o755), ('etc/rc6.d', 0o755), ('etc/rcS.d', 0o755),
]

# Directories whose owner and mode are enforced even when they already exist.
SPECIAL_DIRS = {'tmp': 0o1777, 'var/tmp': 0o1777, 'root': 0o700}

# Modes that are only ever TIGHTENED: a more permissive mode is reduced to (mode & standard).
TIGHTEN = {
    'etc/shadow': 0o640, 'etc/shadow-': 0o640, 'etc/gshadow': 0o640, 'etc/gshadow-': 0o640,
    'etc/security/opasswd': 0o600, 'etc/.pwd.lock': 0o600, 'etc/sudoers': 0o440, 'etc/sudoers.d': 0o750,
    'root/.bash_history': 0o600, 'var/cache/debconf/passwords.dat': 0o600, 'var/log/btmp': 0o640,
    'var/lib/dpkg/lock': 0o640, 'var/lib/dpkg/lock-frontend': 0o640, 'var/cache/apt/archives/lock': 0o640,
    'var/lib/apt/lists/lock': 0o640,
}
TIGHTEN_GLOBS = {'etc/sudoers.d/*': 0o440}

# Programs that cannot work without the setuid bit (sudo is what makes the new user an administrator).
SETUID = ['usr/bin/sudo', 'usr/bin/su', 'usr/bin/passwd', 'usr/bin/chfn', 'usr/bin/chsh', 'usr/bin/gpasswd',
          'usr/bin/newgrp', 'usr/bin/mount', 'usr/bin/umount']

HOME_ROOT = 'home'

# Directories whose exact mode is enforced even when they already exist: what dpkg / apt / update-alternatives /
# debconf need to find (and to be able to write) when they run as root, and to read when run as an ordinary user.
DPKG_DIRS = [
    ('var/lib/dpkg', 0o755), ('var/lib/dpkg/info', 0o755), ('var/lib/dpkg/updates', 0o755),
    ('var/lib/dpkg/triggers', 0o755), ('var/lib/dpkg/alternatives', 0o755), ('var/lib/dpkg/parts', 0o755),
    ('var/cache/apt', 0o755), ('var/cache/apt/archives', 0o755), ('var/cache/apt/archives/partial', 0o700),
    ('var/lib/apt', 0o755), ('var/lib/apt/lists', 0o755), ('var/lib/apt/lists/partial', 0o700),
    ('var/log/apt', 0o755), ('var/cache/debconf', 0o755),
    ('etc/dpkg', 0o755), ('etc/dpkg/dpkg.cfg.d', 0o755), ('etc/alternatives', 0o755), ('etc/apt', 0o755),
    ('etc/apt/apt.conf.d', 0o755), ('etc/apt/sources.list.d', 0o755), ('etc/apt/preferences.d', 0o755),
    ('usr/share/doc', 0o755), ('usr/lib/dpkg', 0o755),
]
# (path, mode) files that must exist (created empty when missing) -- dpkg/apt take these locks themselves.
DPKG_FILES = [
    ('var/lib/dpkg/lock', 0o640), ('var/lib/dpkg/lock-frontend', 0o640), ('var/cache/apt/archives/lock', 0o640),
    ('var/lib/apt/lists/lock', 0o640), ('var/lib/dpkg/available', 0o644),
]
# Files in /var/lib/dpkg that must be readable by everybody (a mode that is too restrictive is widened to this).
DPKG_DB_READABLE = ['var/lib/dpkg/status', 'var/lib/dpkg/status-old', 'var/lib/dpkg/available',
                    'var/lib/dpkg/available-old', 'var/lib/dpkg/diversions', 'var/lib/dpkg/diversions-old',
                    'var/lib/dpkg/statoverride', 'var/lib/dpkg/statoverride-old', 'var/lib/dpkg/arch',
                    'var/lib/dpkg/cmethopt']
# dpkg keeps the maintainer scripts of every package in info/ as 0755; flattened images lose the x bit.
MAINTAINER_SCRIPTS = ('.preinst', '.postinst', '.prerm', '.postrm', '.config')

# Directories that are private on purpose: repair_dir_modes() leaves them (and everything below) alone.
PRIVATE_DIRS = ['root', 'home', 'etc/ssl/private', 'etc/credstore', 'etc/credstore.encrypted', 'etc/sudoers.d',
                'var/lib/sudo', 'var/lib/private', 'var/cache/private', 'var/log/private', 'var/lib/machines',
                'var/lib/portables', 'var/lib/apt/lists/partial', 'var/cache/apt/archives/partial',
                'var/spool/cron/crontabs', 'etc/polkit-1/rules.d', 'usr/share/polkit-1/rules.d', 'etc/cups/ssl',
                'etc/security/opasswd', 'tmp', 'var/tmp', 'lost+found']


def _p(root, rel):
    return os.path.join(root, rel)


def _mkdir(root, rel, mode=0o755):
    """mkdir -p that never depends on the process umask: every missing component becomes root:root, and the
    components above the last one get 0755 (os.makedirs would give them 0777 & ~umask).  Returns True if the
    last component was created.  Never walks through a symlink (an absolute one would leave the target)."""
    parts = [p for p in rel.split('/') if p]
    cur = root
    for i, part in enumerate(parts):
        cur = os.path.join(cur, part)
        last = i == len(parts) - 1
        if os.path.lexists(cur):
            if not last and os.path.islink(cur):
                return False
            continue
        os.mkdir(cur)
        os.chown(cur, 0, 0)
        os.chmod(cur, mode if last else 0o755)
        if last:
            return True
    return False


def ensure_standard_dirs(root):
    """Create missing standard directories (owner root:root).  Returns the list created."""
    created = []
    for rel, mode in STANDARD_DIRS:
        if os.path.lexists(_p(root, rel)):
            continue                                        # already there (or something else is): leave it
        if _mkdir(root, rel, mode):
            created.append(rel)
    return created


def repair_dir_modes(root):
    """Directories must be traversable by everybody (at least 0755) unless they are private on purpose.  tar
    creates the parents of archive members implicitly with 0777 & ~umask, so a restrictive umask during the
    extraction leaves directories such as /usr/lib/x86_64-linux-gnu or /var/lib/dpkg/info at 0700.  Only adds the
    missing rwx / r-x bits.  Returns 'path 0700 -> 0755' descriptions."""
    changes = []
    private = tuple(PRIVATE_DIRS)
    rootdev = os.lstat(root).st_dev
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                rel = os.path.relpath(e.path, root)
                if rel in private or rel.startswith(tuple(x + '/' for x in private)):
                    continue
                st = e.stat(follow_symlinks=False)
                if st.st_dev != rootdev or not stat.S_ISDIR(st.st_mode):
                    continue
                cur = stat.S_IMODE(st.st_mode)
                if 0o755 & ~cur:
                    os.chmod(e.path, cur | 0o755)
                    changes.append('%s %04o -> %04o' % (rel, cur, cur | 0o755))
                stack.append(e.path)
    return changes


def repair_dpkg(root):
    """Make dpkg, apt, update-alternatives and debconf usable on the freshly extracted system: their state
    directories exist with the right owner and mode, their lock files exist, the dpkg database is readable and
    the maintainer scripts are executable.  Returns human readable descriptions of what was changed."""
    changes = []
    for rel, mode in DPKG_DIRS:
        path = _p(root, rel)
        if not os.path.lexists(path):
            if _mkdir(root, rel, mode):
                changes.append('/%s created (%04o)' % (rel, mode))
            continue
        if os.path.islink(path) or not os.path.isdir(path):
            continue                                        # verify_dpkg() reports it: never replace what the image put there
        st = os.lstat(path)
        if (st.st_uid, st.st_gid) != (0, 0):
            os.chown(path, 0, 0)
            changes.append('/%s owner %d:%d -> 0:0' % (rel, st.st_uid, st.st_gid))
        if stat.S_IMODE(st.st_mode) != mode:
            os.chmod(path, mode)
            changes.append('/%s %04o -> %04o' % (rel, stat.S_IMODE(st.st_mode), mode))
    for rel, mode in DPKG_FILES:
        path = _p(root, rel)
        if os.path.lexists(path):
            continue
        if not os.path.isdir(os.path.dirname(path)):
            continue
        with open(path, 'wb'):
            pass
        os.chown(path, 0, 0)
        os.chmod(path, mode)
        changes.append('/%s created (%04o)' % (rel, mode))
    for rel in DPKG_DB_READABLE:                            # root-owned, readable by all, writable by root only
        path = _p(root, rel)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        cur = stat.S_IMODE(os.lstat(path).st_mode)
        if (cur & 0o644) != 0o644:
            os.chmod(path, cur | 0o644)
            changes.append('/%s %04o -> %04o' % (rel, cur, cur | 0o644))
    info = _p(root, 'var/lib/dpkg/info')
    if os.path.isdir(info) and not os.path.islink(info):
        for name in sorted(os.listdir(info)):
            path = os.path.join(info, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            cur = stat.S_IMODE(os.lstat(path).st_mode)
            want = cur | 0o755 if name.endswith(MAINTAINER_SCRIPTS) else cur | 0o644
            if want != cur:
                os.chmod(path, want)
                changes.append('/var/lib/dpkg/info/%s %04o -> %04o' % (name, cur, want))
    return changes


def fix_special_dirs(root):
    """/tmp and /var/tmp must be root:root 1777; /root must be root:root 0700."""
    for rel, mode in SPECIAL_DIRS.items():
        path = _p(root, rel)
        if os.path.islink(path) or not os.path.isdir(path):
            raise OSError('%s is not a directory' % ('/' + rel))
        os.chown(path, 0, 0)
        os.chmod(path, mode)


def enforce_root_ownership(root):
    """Everything outside /home/<user> belongs to root:root.  Returns the list of paths that had to change."""
    os.chown(root, 0, 0)
    os.chmod(root, 0o755)
    changed = []
    stack = [root]
    home = os.path.realpath(_p(root, HOME_ROOT))
    rootdev = os.lstat(root).st_dev
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if os.path.dirname(e.path) == home:          # /home/<user>: user data, owned by the user
                    continue
                st = e.stat(follow_symlinks=False)
                if st.st_dev != rootdev:                      # a mounted filesystem (/proc, /sys, /dev ...): not ours
                    continue
                if (st.st_uid, st.st_gid) != (0, 0):
                    os.lchown(e.path, 0, 0)
                    changed.append(os.path.relpath(e.path, root))
                if stat.S_ISDIR(st.st_mode) and not e.is_symlink():
                    stack.append(e.path)
    return changed


def apply_baseline_permissions(root):
    """Tighten secrets, restore setuid on the essentials.  Returns human readable change descriptions."""
    changes = []
    items = dict(TIGHTEN)
    for pattern, mode in TIGHTEN_GLOBS.items():
        for f in glob.glob(_p(root, pattern)):
            items[os.path.relpath(f, root)] = mode
    for rel, std in sorted(items.items()):
        path = _p(root, rel)
        if os.path.islink(path) or not os.path.exists(path):
            continue
        cur = stat.S_IMODE(os.lstat(path).st_mode)
        if cur & ~std:
            os.chmod(path, cur & std)
            changes.append('%s %04o -> %04o' % (rel, cur, cur & std))
    for rel in SETUID:
        path = _p(root, rel)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        st = os.lstat(path)
        if (st.st_uid, st.st_gid) == (0, 0) and not st.st_mode & stat.S_ISUID:
            os.chmod(path, 0o4755)
            changes.append('%s %04o -> 4755 (setuid)' % (rel, stat.S_IMODE(st.st_mode)))
    return changes


def init_machine_id(root):
    """Give this installation its own random machine-id (never the image's)."""
    mid = uuid.uuid4().hex                                  # 32 lowercase hex digits, a random (version 4) UUID
    path = _p(root, 'etc/machine-id')
    if os.path.lexists(path):
        os.unlink(path)
    with open(path, 'w') as f:
        f.write(mid + '\n')
    os.chown(path, 0, 0)
    os.chmod(path, 0o444)
    dbus = _p(root, 'var/lib/dbus')
    if os.path.isdir(dbus):                                 # D-Bus shares the same ID
        link = os.path.join(dbus, 'machine-id')
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink('/etc/machine-id', link)
    return mid


def parse_colon_file(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                if line.strip() and not line.startswith('#'):
                    rows.append(line.rstrip('\n').split(':'))
    except OSError:
        pass
    return rows


def verify_dpkg(root):
    """Static check that dpkg/apt can work.  Returns a list of problems (empty = fine, or dpkg is not installed)."""
    problems = []
    if not os.path.exists(_p(root, 'usr/bin/dpkg')):
        return problems
    for rel, mode in DPKG_DIRS:
        path = _p(root, rel)
        try:
            st = os.lstat(path)
        except OSError:
            problems.append('/%s is missing (dpkg needs it)' % rel)
            continue
        if not stat.S_ISDIR(st.st_mode):
            problems.append('/%s must be a directory (dpkg needs it)' % rel)
        elif (st.st_uid, st.st_gid) != (0, 0) or stat.S_IMODE(st.st_mode) != mode:
            problems.append('/%s must be root:root %04o (is %d:%d %04o)' % (
                rel, mode, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)))
    for rel, _ in DPKG_FILES:
        if not os.path.isfile(_p(root, rel)):
            problems.append('/%s is missing (dpkg needs it)' % rel)
    status = _p(root, 'var/lib/dpkg/status')
    if not os.path.isfile(status):
        problems.append('/var/lib/dpkg/status is missing: the image has no dpkg database')
    elif not os.access(status, os.R_OK):
        problems.append('/var/lib/dpkg/status is not readable')
    info = _p(root, 'var/lib/dpkg/info')
    if os.path.isdir(info):
        bad = [n for n in os.listdir(info) if n.endswith(MAINTAINER_SCRIPTS)
               and os.path.isfile(os.path.join(info, n)) and not os.lstat(os.path.join(info, n)).st_mode & 0o111]
        if bad:
            problems.append('maintainer scripts without the x bit: ' + ', '.join(sorted(bad)[:8]))
    return problems


def verify_policy(root, username=None):
    """Check the ownership table.  Returns a list of problems (empty = all good)."""
    problems = []
    st = os.lstat(root)
    if (st.st_uid, st.st_gid) != (0, 0):
        problems.append('/ is not owned by root:root')
    for rel in ('tmp', 'var/tmp'):
        try:
            s = os.lstat(_p(root, rel))
        except OSError:
            problems.append('/%s is missing' % rel)
            continue
        if not stat.S_ISDIR(s.st_mode) or (s.st_uid, s.st_gid) != (0, 0) or stat.S_IMODE(s.st_mode) != 0o1777:
            problems.append('/%s must be a root:root directory with mode 1777 (is %s %d:%d %04o)' % (
                rel, 'dir' if stat.S_ISDIR(s.st_mode) else 'not a dir', s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)))
    bad = []
    home = os.path.realpath(_p(root, HOME_ROOT))
    rootdev = st.st_dev
    stack = [root]
    while stack and len(bad) < 10:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if os.path.dirname(e.path) == home:          # /home/<user>: checked separately below
                    continue
                s = e.stat(follow_symlinks=False)
                if s.st_dev != rootdev:                       # mounted filesystem (chroot bind mounts): skip
                    continue
                if (s.st_uid, s.st_gid) != (0, 0):
                    bad.append(os.path.relpath(e.path, root))
                if stat.S_ISDIR(s.st_mode) and not e.is_symlink():
                    stack.append(e.path)
    if bad:
        problems.append('entries not owned by root:root: ' + ', '.join(bad))
    problems += verify_dpkg(root)
    if username:
        pw = next((r for r in parse_colon_file(_p(root, 'etc/passwd')) if r and r[0] == username), None)
        if not pw:
            problems.append('user %s is missing from /etc/passwd' % username)
        else:
            uid, gid = int(pw[2]), int(pw[3])
            grp = next((r for r in parse_colon_file(_p(root, 'etc/group')) if len(r) > 2 and r[2] == str(gid)), None)
            if not grp or grp[0] != username:
                problems.append('the primary group of %s is %r, expected %r' % (username, grp[0] if grp else None, username))
            h = _p(root, HOME_ROOT + '/' + username)
            try:
                s = os.lstat(h)
                if (s.st_uid, s.st_gid) != (uid, gid):
                    problems.append('/home/%s is owned by %d:%d, expected %d:%d (%s:%s)' % (username, s.st_uid, s.st_gid, uid, gid, username, username))
                for r, ds, fs in os.walk(h):
                    for n in ds + fs:
                        s = os.lstat(os.path.join(r, n))
                        if (s.st_uid, s.st_gid) != (uid, gid):
                            problems.append('%s inside the home directory is owned by %d:%d' % (os.path.relpath(os.path.join(r, n), root), s.st_uid, s.st_gid))
                            break
            except OSError:
                problems.append('/home/%s does not exist' % username)
    try:
        mid = open(_p(root, 'etc/machine-id')).read()
        if not re.fullmatch(r'[0-9a-f]{32}\n', mid) or mid.strip('0\n') == '':
            problems.append('/etc/machine-id is not a valid machine ID')
    except OSError:
        problems.append('/etc/machine-id is missing')
    return problems
