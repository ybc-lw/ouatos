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
}
TIGHTEN_GLOBS = {'etc/sudoers.d/*': 0o440}

# Programs that cannot work without the setuid bit (sudo is what makes the new user an administrator).
SETUID = ['usr/bin/sudo', 'usr/bin/su', 'usr/bin/passwd', 'usr/bin/chfn', 'usr/bin/chsh', 'usr/bin/gpasswd',
          'usr/bin/newgrp', 'usr/bin/mount', 'usr/bin/umount']

HOME_ROOT = 'home'


def _p(root, rel):
    return os.path.join(root, rel)


def ensure_standard_dirs(root):
    """Create missing standard directories (owner root:root).  Returns the list created."""
    created = []
    for rel, mode in STANDARD_DIRS:
        path = _p(root, rel)
        if os.path.lexists(path):
            continue                                        # already there (or something else is): leave it
        os.makedirs(path)
        os.chown(path, 0, 0)
        os.chmod(path, mode)
        created.append(rel)
    return created


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
