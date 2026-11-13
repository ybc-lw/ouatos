"""Ownership / permission policy applied to a freshly extracted system.

Rules implemented here:
  * "/" and everything under it is owned by root:root (the only exceptions are user home directories and
    package-specific entries resolved from SPECIAL_OWNERS, tmpfiles.d, and dpkg statoverride).
  * /tmp and /var/tmp are root:root with mode 1777 (sticky bit: users cannot delete each other's files).
  * /home/<user> is owned by <user>:<user>  (done by the engine with useradd -U, verified by verify_policy()).
  * /etc/machine-id is initialised with a fresh random ID for every installation.

The OS image is an archive that has lost information: it contains no empty directories (no /dev /proc /sys
/run /tmp /home ...), every entry is owned by 0:0 and its modes are flattened (no setuid bits, /etc/shadow is
0644 ...).  So the installer (1) creates the standard directories, (2) tightens -- never loosens -- the few
security-critical modes, and (3) restores the setuid bit and package-specific owners/modes from SPECIAL_OWNERS,
tmpfiles.d, and dpkg statoverride.
"""
import errno
import fnmatch
import glob
import os
import re
import stat
import uuid

# (path relative to /, mode).  Created only when missing; existing entries are left for standard_dir_conflicts().
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
    ('var/local', 0o755), ('var/opt', 0o755), ('var/mail', 0o2775), ('var/backups', 0o755),
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
    'root/.bash_history': 0o600, 'var/cache/debconf/passwords.dat': 0o600, 'var/log/btmp': 0o660,
    'var/lib/dpkg/lock': 0o640, 'var/lib/dpkg/lock-frontend': 0o640, 'var/cache/apt/archives/lock': 0o640,
    'var/lib/apt/lists/lock': 0o640,
}
TIGHTEN_GLOBS = {'etc/sudoers.d/*': 0o440}

# Programs that cannot work without the setuid bit (sudo is what makes the new user an administrator).
SETUID = [
    'usr/bin/sudo', 'usr/bin/su', 'usr/bin/passwd', 'usr/bin/chfn', 'usr/bin/chsh', 'usr/bin/gpasswd',
    'usr/bin/newgrp', 'usr/bin/mount', 'usr/bin/umount', 'usr/bin/fusermount', 'usr/bin/fusermount3',
    'usr/bin/pkexec', 'usr/lib/polkit-1/polkit-agent-helper-1', 'usr/libexec/polkit-agent-helper-1',
]

HOME_ROOT = 'home'

# Entries that are deliberately NOT root:root (or require specific setgid/directory permissions).
# Tuple: (path, owner, group_or_candidates, mode_or_None) or (..., recursive_bool).
# Each path is unique; candidate tuples pick the first matching name in the TARGET's /etc/passwd or /etc/group.
# Target tmpfiles.d and dpkg's statoverride database are layered on top (statoverride has highest priority).
SPECIAL_OWNERS = [
    ('usr/lib/dbus-1.0/dbus-daemon-launch-helper', 'root', 'messagebus', 0o4754),
    ('usr/bin/chage', 'root', 'shadow', 0o2755),
    ('usr/bin/expiry', 'root', 'shadow', 0o2755),
    ('usr/sbin/unix_chkpwd', 'root', 'shadow', 0o2755),
    ('usr/bin/wall', 'root', 'tty', 0o2755),
    ('usr/bin/write', 'root', 'tty', 0o2755),
    ('usr/bin/bsd-write', 'root', 'tty', 0o2755),
    ('usr/bin/crontab', 'root', 'crontab', 0o2755),
    ('var/spool/cron/crontabs', 'root', 'crontab', 0o1730),
    ('var/mail', 'root', 'mail', 0o2775),
    ('usr/bin/ssh-agent', 'root', ('_ssh', 'ssh'), 0o2755),
    ('usr/lib/openssh/ssh-keysign', 'root', ('_ssh', 'ssh'), 0o2755),
    ('usr/lib/utempter/utempter', 'root', 'utmp', 0o2755),
    ('var/log/wtmp', 'root', 'utmp', 0o664),
    ('var/log/btmp', 'root', 'utmp', 0o660),
    ('var/log/lastlog', 'root', 'utmp', 0o664),
    ('var/log/journal', 'root', 'systemd-journal', 0o2755, True),
    ('etc/ssl/private', 'root', 'ssl-cert', 0o710),
    ('etc/polkit-1/rules.d', 'polkitd', 'root', 0o750),
    ('var/lib/polkit-1', 'polkitd', 'root', 0o700, True),
    ('etc/cups/ssl', 'root', 'lp', 0o700),
    ('var/spool/cups', 'root', 'lp', 0o710, True),
    ('var/cache/cups', 'root', 'lp', 0o775, True),
    ('usr/sbin/pppd', 'root', 'dip', 0o4754),
    ('var/lib/colord', 'colord', 'colord', 0o755, True),
    ('var/lib/geoclue', 'geoclue', 'geoclue', 0o755, True),
    ('var/lib/tpm2-tss', 'tss', 'tss', 0o755, True),
    # the setgid-shadow helpers above can only read these through the group
    ('etc/shadow', 'root', 'shadow', None), ('etc/shadow-', 'root', 'shadow', None),
    ('etc/gshadow', 'root', 'shadow', None), ('etc/gshadow-', 'root', 'shadow', None),
    # apt downloads as the _apt user; without this it warns and downloads unsandboxed as root
    ('var/cache/apt/archives/partial', '_apt', 'root', 0o700),
    ('var/lib/apt/lists/partial', '_apt', 'root', 0o700),
]

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


def tpath(root, rel, follow=False):
    """The real path, on the machine the installer runs on, of `rel` inside the target at `root`.

    The target is a foreign file system: an absolute symlink in it (/var/run -> /run, or one the image author put in
    by mistake) points into the TARGET once the system runs, but the installer's own open()/chown()/mount() would
    resolve it against the LIVE system and touch the host.  So every directory component is resolved here, inside
    `root`, and the result never leaves it.  The last component is not followed unless follow=True (use that to read
    a file or test that something exists; leave it off to replace, chown or unlink the entry itself).
    Raises OSError(ELOOP) for a symlink loop."""
    rel = rel.lstrip('/')
    full = os.path.join(root, rel) if rel else root
    if follow or not rel:
        real = resolve_in_root(root, full)
    else:
        parent = resolve_in_root(root, os.path.dirname(full))
        real = None if parent is None else os.path.join(parent, os.path.basename(full))
    if real is None:
        raise OSError(errno.ELOOP, 'too many levels of symbolic links', full)
    return real


def _p(root, rel):
    return tpath(root, rel)


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
            if not last and (os.path.islink(cur) or not os.path.isdir(cur)):
                return False                                # a symlink or a file in the way: never walk through it
            continue
        os.mkdir(cur)
        os.chown(cur, 0, 0)
        os.chmod(cur, mode if last else 0o755)
        if last:
            return True
    return False


def resolve_in_root(root, path, limit=40):
    """Resolve `path` the way the installed system will see it: an absolute symlink target is taken relative to
    `root`, not to the live system, and '..' never leaves `root`.  Returns the resolved path, or None for a loop."""
    root = os.path.normpath(root)
    cur = root
    todo = [c for c in os.path.relpath(os.path.normpath(path), root).split('/') if c]
    todo.reverse()
    hops = 0
    while todo:
        part = todo.pop()
        if part == '.':
            continue
        if part == '..':
            if cur != root:
                cur = os.path.dirname(cur)
            continue
        nxt = os.path.join(cur, part)
        if os.path.islink(nxt):
            hops += 1
            if hops > limit:
                return None
            link = os.readlink(nxt)
            if link.startswith('/'):
                cur = root
            todo.extend(reversed([c for c in link.split('/') if c]))
            continue
        cur = nxt
    return cur


def standard_dir_conflicts(root):
    """Check the TYPE of every path in STANDARD_DIRS / DPKG_DIRS (run it after ensure_standard_dirs and
    repair_dpkg): it must be a directory, or a symlink that resolves to one inside the target.  Returns problems."""
    problems, seen = [], set()
    for rel, _ in STANDARD_DIRS + DPKG_DIRS:
        if rel in seen:
            continue
        seen.add(rel)
        try:
            entry = _p(root, rel)
        except OSError:
            problems.append('/%s has a symbolic link loop in a parent directory' % rel)
            continue
        real = resolve_in_root(root, entry)
        if real is None:
            problems.append('/%s is a symbolic link loop' % rel)
        elif os.path.isdir(real):
            continue
        elif os.path.lexists(real):
            problems.append('/%s exists but is not a directory' % rel)
        elif os.path.lexists(entry):
            problems.append('/%s is a symbolic link to %s, which does not exist' % (rel, os.readlink(entry)))
        else:
            problems.append('/%s is missing and could not be created (something that is not a directory is in the way)' % rel)
    return problems


def _id_tables(root):
    users, groups = {}, {}
    for r in parse_colon_file(tpath(root, 'etc/passwd', follow=True)):
        if len(r) > 2 and r[2].isdigit():
            users[r[0]] = int(r[2])
    for r in parse_colon_file(tpath(root, 'etc/group', follow=True)):
        if len(r) > 2 and r[2].isdigit():
            groups[r[0]] = int(r[2])
    return users, groups


def _lookup_one(name, table):
    if name.startswith('#') and name[1:].isdigit():          # dpkg writes numeric ids as #1234
        return int(name[1:])
    if name.isdigit():
        return int(name)
    return table.get(name)


def _lookup_owner_group(owner, group, users, groups):
    """Resolve owner and group names/IDs.  A '-' or '' means 'leave unchanged' and resolves to (True, None)."""
    owners = owner if isinstance(owner, (tuple, list)) else (owner,)
    uid_ok, uid = False, None
    for cand in owners:
        if cand in ('-', ''):
            uid_ok, uid = True, None
            break
        val = _lookup_one(cand, users)
        if val is not None:
            uid_ok, uid = True, val
            break

    grp_list = group if isinstance(group, (tuple, list)) else (group,)
    gid_ok, gid = False, None
    for cand in grp_list:
        if cand in ('-', ''):
            gid_ok, gid = True, None
            break
        val = _lookup_one(cand, groups)
        if val is not None:
            gid_ok, gid = True, val
            break
    return uid_ok, uid, gid_ok, gid


def _fmt_names(val):
    return '|'.join(val) if isinstance(val, (tuple, list)) else str(val)


def _parse_tmpfiles_mode(raw_mode):
    """Parse systemd tmpfiles.d mode field.
    Returns (mode_or_None, masked_bool):
      - '-' -> (None, False)
      - ':' prefix -> mode applies only when creating a new entry; for existing entries returns (None, False)
      - '~' prefix -> mode is masked dynamically against existing r/w/x bits (masked_bool=True)
    """
    if raw_mode == '-':
        return None, False
    prefix_len = len(raw_mode) - len(raw_mode.lstrip('~:'))
    prefix = raw_mode[:prefix_len]
    digits = raw_mode[prefix_len:]
    if not re.fullmatch(r'[0-7]{3,4}', digits):
        return None, False
    if ':' in prefix:
        return None, False
    return int(digits, 8), ('~' in prefix)


def _glob_in_root(root, rel_pattern):
    """Expand shell-style globs (*, ?, [...]) strictly inside `root`."""
    parts = [p for p in rel_pattern.split('/') if p and p != '.']
    if not parts or '..' in parts:
        return []
    matches = ['']
    for part in parts:
        next_matches = []
        has_magic = any(ch in part for ch in ('*', '?', '['))
        for base_rel in matches:
            try:
                base_abs = tpath(root, base_rel, follow=True)
            except OSError:
                continue
            if not os.path.isdir(base_abs):
                continue
            if has_magic:
                try:
                    it = os.scandir(base_abs)
                except OSError:
                    continue
                with it:
                    names = sorted(e.name for e in it if fnmatch.fnmatchcase(e.name, part))
                for name in names:
                    next_matches.append(base_rel + '/' + name if base_rel else name)
            else:
                cand_abs = os.path.join(base_abs, part)
                if os.path.lexists(cand_abs):
                    next_matches.append(base_rel + '/' + part if base_rel else part)
        matches = next_matches
        if not matches:
            break
    return matches


def _iter_tmpfiles_entries(root):
    """Yield (rel, owner, group, mode, masked, recursive, src) from target's usr/lib/tmpfiles.d and etc/tmpfiles.d.
    Only ownership/permission adjustment types ('d', 'D', 'e', 'z', 'Z') are processed; file creation/write types
    ('f', 'F', 'w') are excluded.  Per systemd-tmpfiles semantics, configs are read in sorted filename order
    (etc/tmpfiles.d overrides same-named usr/lib/tmpfiles.d) and the first matching rule for a path wins."""
    conf_rels = {}
    for base in ('usr/lib/tmpfiles.d', 'etc/tmpfiles.d'):
        try:
            d = tpath(root, base, follow=True)
        except OSError:
            continue
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith('.conf'):
                conf_rels[name] = base + '/' + name             # etc/tmpfiles.d overrides usr/lib/tmpfiles.d

    seen_paths = set()
    for name in sorted(conf_rels):
        try:
            conf_path = tpath(root, conf_rels[name], follow=True)
            with open(conf_path) as f:
                lines = f.readlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            rtype, raw_path, raw_mode, owner, group = parts[:5]
            if rtype not in ('d', 'D', 'e', 'z', 'Z'):
                continue
            if '%' in raw_path:
                continue
            rel_pattern = os.path.normpath(raw_path).lstrip('/')
            if not rel_pattern or rel_pattern == '.' or rel_pattern.startswith('..'):
                continue
            mode, masked = _parse_tmpfiles_mode(raw_mode)
            if owner == '-' and group == '-' and mode is None:
                continue

            if any(ch in rel_pattern for ch in ('*', '?', '[')):
                if rtype not in ('e', 'z', 'Z'):
                    continue
                rels = _glob_in_root(root, rel_pattern)
            else:
                rels = [rel_pattern]

            for rel in rels:
                if rel in seen_paths:
                    continue
                try:
                    entry_path = _p(root, rel)
                    st = os.lstat(entry_path)
                except OSError:
                    continue
                if rtype == 'e' and not stat.S_ISDIR(st.st_mode):
                    continue
                seen_paths.add(rel)
                yield (rel, owner, group, mode, masked, rtype == 'Z', 'tmpfiles.d/%s' % name)


def _special_owner_rules(root):
    """Resolve package-specific ownership/permission rules from SPECIAL_OWNERS, tmpfiles.d, and statoverride.
    Returns ({rel: (uid_or_None, gid_or_None, mode_or_None, masked, recursive)}, [skipped descriptions])."""
    users, groups = _id_tables(root)
    out, skipped = {}, []
    wanted = []
    for item in SPECIAL_OWNERS:
        rel, o, g, m = item[:4]
        rec = bool(item[4]) if len(item) > 4 else False
        wanted.append((rel, o, g, m, False, rec, 'built-in'))
    for rel, o, g, m, masked, rec, src in _iter_tmpfiles_entries(root):
        wanted.append((rel, o, g, m, masked, rec, src))
    try:                                                       # owner group mode path, as dpkg-statoverride --list
        with open(tpath(root, 'var/lib/dpkg/statoverride', follow=True)) as f:
            for line in f:
                parts = line.split(None, 3)
                if len(parts) == 4 and re.fullmatch(r'[0-7]{3,4}', parts[2]):
                    rel = os.path.normpath(parts[3].strip()).lstrip('/')
                    if rel and rel != '.' and not rel.startswith('..'):
                        wanted.append((rel, parts[0], parts[1], int(parts[2], 8), False, False, 'statoverride'))
    except OSError:
        pass
    for rel, owner, group, mode, masked, rec, src in wanted:
        uid_ok, uid, gid_ok, gid = _lookup_owner_group(owner, group, users, groups)
        if not uid_ok or not gid_ok:
            skipped.append('/%s (%s %s:%s: no such %s in the target)' % (
                rel, src, _fmt_names(owner), _fmt_names(group), 'user' if not uid_ok else 'group'))
            continue
        prev = out.get(rel)
        if prev is not None:
            merged_uid = prev[0] if uid is None else uid
            merged_gid = prev[1] if gid is None else gid
            if mode is None:
                merged_mode, merged_masked = prev[2], prev[3]
            else:
                merged_mode, merged_masked = mode, masked
            merged_rec = prev[4] or rec
            out[rel] = (merged_uid, merged_gid, merged_mode, merged_masked, merged_rec)
        else:
            out[rel] = (uid, gid, mode, masked, rec)
    return out, skipped


def _expected_rule_mode(rule_mode, masked, st_mode, is_exact):
    """Compute target mode from a rule against an entry's existing `st_mode`:
      - When `masked` (`~` in tmpfiles.d), clear r/w/x bits that are completely absent in `st_mode`,
        and clear sticky/SUID/SGID bits (`0o7000`) unless the entry is a directory.
      - Otherwise, exact rules apply `rule_mode` directly; recursive descendants apply `rule_mode` to directories
        and strip directory-only/execute bits (`& 0o666`) for regular files."""
    if rule_mode is None:
        return None
    if masked:
        cur = stat.S_IMODE(st_mode)
        m = rule_mode
        if (cur & 0o444) == 0:
            m &= ~0o444
        if (cur & 0o222) == 0:
            m &= ~0o222
        if (cur & 0o111) == 0:
            m &= ~0o111
        if not stat.S_ISDIR(st_mode):
            m &= ~0o7000
        return m
    if is_exact or stat.S_ISDIR(st_mode):
        return rule_mode
    return rule_mode & 0o666


def special_owners(root):
    """The entries that are not root:root, resolved against the TARGET's passwd/group files.
    Returns ({rel: (uid_or_None, gid_or_None, mode_or_None)}, [descriptions of entries that were skipped])."""
    rules, skipped = _special_owner_rules(root)
    out = {}
    for rel, (uid, gid, mode, masked, _rec) in rules.items():
        eff_mode = mode
        if mode is not None and masked:
            try:
                path = _p(root, rel)
                if os.path.lexists(path) and not os.path.islink(path):
                    eff_mode = _expected_rule_mode(mode, True, os.lstat(path).st_mode, True)
            except OSError:
                pass
        out[rel] = (uid, gid, eff_mode)
    return out, skipped


def _matched_owner_rule(rel, rules):
    """Return (rule_tuple, is_exact) for `rel` via exact match or nearest recursive ancestor, else (None, False)."""
    if rel in rules:
        return rules[rel], True
    cur = os.path.dirname(rel)
    while cur and cur != '.':
        rule = rules.get(cur)
        if rule and rule[4]:
            return rule, False
        cur = os.path.dirname(cur)
    return None, False


def apply_special_owners(root):
    """chown/chmod the entries from special_owners().  chown comes first because it clears setuid/setgid bits.
    Returns (changes, skipped)."""
    rules, skipped = _special_owner_rules(root)
    changes = []
    for rel, (uid, gid, mode, masked, rec) in sorted(rules.items()):
        try:
            path = _p(root, rel)
        except OSError:
            continue
        if not os.path.lexists(path) or os.path.islink(path):
            continue
        st = os.lstat(path)
        want_uid = st.st_uid if uid is None else uid
        want_gid = st.st_gid if gid is None else gid
        chowned = (st.st_uid, st.st_gid) != (want_uid, want_gid)
        if chowned:
            os.chown(path, want_uid, want_gid)
        want_mode = _expected_rule_mode(mode, masked, st.st_mode, True)
        cur = stat.S_IMODE(os.lstat(path).st_mode)
        if want_mode is not None and (chowned or cur != want_mode):
            os.chmod(path, want_mode)
            cur = want_mode
        if chowned or (want_mode is not None and stat.S_IMODE(st.st_mode) != want_mode):
            changes.append('/%s -> %d:%d %04o' % (rel, want_uid, want_gid, cur))
        if rec and stat.S_ISDIR(st.st_mode):
            for r, ds, fs in os.walk(path):
                for name in ds + fs:
                    sub = os.path.join(r, name)
                    sub_rel = os.path.relpath(sub, root)
                    matched, is_exact = _matched_owner_rule(sub_rel, rules)
                    if matched is None or matched != (uid, gid, mode, masked, rec) or is_exact:
                        continue
                    sst = os.lstat(sub)
                    sub_want_uid = sst.st_uid if uid is None else uid
                    sub_want_gid = sst.st_gid if gid is None else gid
                    sub_chowned = (sst.st_uid, sst.st_gid) != (sub_want_uid, sub_want_gid)
                    if sub_chowned:
                        os.lchown(sub, sub_want_uid, sub_want_gid)
                    if stat.S_ISLNK(sst.st_mode):
                        if sub_chowned:
                            changes.append('/%s -> %d:%d' % (sub_rel, sub_want_uid, sub_want_gid))
                        continue
                    want_sub_mode = _expected_rule_mode(mode, masked, sst.st_mode, False)
                    cur_sub_mode = stat.S_IMODE(os.lstat(sub).st_mode)
                    mode_changed = False
                    if want_sub_mode is not None and (sub_chowned or cur_sub_mode != want_sub_mode):
                        os.chmod(sub, want_sub_mode)
                        mode_changed = stat.S_IMODE(sst.st_mode) != want_sub_mode
                        cur_sub_mode = want_sub_mode
                    if sub_chowned and mode_changed:
                        changes.append('/%s -> %d:%d %04o' % (sub_rel, sub_want_uid, sub_want_gid, cur_sub_mode))
                    elif sub_chowned:
                        changes.append('/%s -> %d:%d' % (sub_rel, sub_want_uid, sub_want_gid))
                    elif mode_changed:
                        changes.append('/%s -> %04o' % (sub_rel, cur_sub_mode))
    return changes, skipped


def ensure_standard_dirs(root):
    """Create missing standard directories (owner root:root).  Returns the list created.  A path that already exists
    is left alone whatever it is -- standard_dir_conflicts() reports the ones that are not directories."""
    created = []
    for rel, mode in STANDARD_DIRS:
        try:
            if os.path.lexists(_p(root, rel)):
                continue                                    # already there (or something else is): leave it
        except OSError:
            continue                                        # symlink loop in parent; standard_dir_conflicts() reports it
        if _mkdir(root, rel, mode):
            created.append(rel)
    return created


def repair_dir_modes(root):
    """Directories must be traversable by everybody (at least 0755) unless private or explicitly governed by a rule.
    tar creates the parents of archive members implicitly with 0777 & ~umask, so a restrictive umask during the
    extraction leaves directories such as /usr/lib/x86_64-linux-gnu or /var/lib/dpkg/info at 0700.  Only adds the
    missing rwx / r-x bits.  Returns 'path 0700 -> 0755' descriptions."""
    changes = []
    private = tuple(PRIVATE_DIRS)
    rules = _special_owner_rules(root)[0]
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
                matched, _ = _matched_owner_rule(rel, rules)
                if matched is None or matched[2] is None:
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
    owners = special_owners(root)[0]
    for rel, mode in DPKG_DIRS:
        try:
            path = _p(root, rel)
        except OSError:
            continue
        if not os.path.lexists(path):
            if _mkdir(root, rel, mode):
                changes.append('/%s created (%04o)' % (rel, mode))
            continue
        if os.path.islink(path) or not os.path.isdir(path):
            continue                                        # verify_dpkg() reports it: never replace what the image put there
        st = os.lstat(path)
        override = owners.get(rel)
        if override is None and (st.st_uid, st.st_gid) != (0, 0):
            os.chown(path, 0, 0)
            changes.append('/%s owner %d:%d -> 0:0' % (rel, st.st_uid, st.st_gid))
        want_mode = override[2] if (override is not None and override[2] is not None) else mode
        if stat.S_IMODE(st.st_mode) != want_mode:
            os.chmod(path, want_mode)
            changes.append('/%s %04o -> %04o' % (rel, stat.S_IMODE(st.st_mode), want_mode))
    for rel, mode in DPKG_FILES:
        try:
            path = _p(root, rel)
        except OSError:
            continue
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
        try:
            path = _p(root, rel)
        except OSError:
            continue
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
    """Everything outside /home/<user> and special_owners rules belongs to root:root.  Returns the list of paths that had to change."""
    os.chown(root, 0, 0)
    os.chmod(root, 0o755)
    changed = []
    stack = [root]
    home = tpath(root, HOME_ROOT, follow=True)
    rootdev = os.lstat(root).st_dev
    rules = _special_owner_rules(root)[0]
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
                rel = os.path.relpath(e.path, root)
                matched, _ = _matched_owner_rule(rel, rules)
                if matched is None and (st.st_uid, st.st_gid) != (0, 0):
                    os.lchown(e.path, 0, 0)
                    changed.append(rel)
                if stat.S_ISDIR(st.st_mode) and not e.is_symlink():
                    stack.append(e.path)
    return changed


def apply_baseline_permissions(root):
    """Tighten secrets, restore setuid on the essentials (respecting special_owners overrides).  Returns human readable change descriptions."""
    changes = []
    owners = special_owners(root)[0]
    items = dict(TIGHTEN)
    for pattern, mode in TIGHTEN_GLOBS.items():
        for f in glob.glob(_p(root, pattern)):
            items[os.path.relpath(f, root)] = mode
    for rel, std in sorted(items.items()):
        override = owners.get(rel)
        if override is not None and override[2] is not None:
            continue
        path = _p(root, rel)
        if os.path.islink(path) or not os.path.exists(path):
            continue
        cur = stat.S_IMODE(os.lstat(path).st_mode)
        if cur & ~std:
            os.chmod(path, cur & std)
            changes.append('%s %04o -> %04o' % (rel, cur, cur & std))
    for rel in SETUID:
        override = owners.get(rel)
        if override is not None and override[2] is not None:
            continue
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
    dbus = tpath(root, 'var/lib/dbus', follow=True)         # inside the target, even if var/lib/dbus is an absolute link
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
    if not os.path.exists(tpath(root, 'usr/bin/dpkg', follow=True)):
        return problems
    owners = special_owners(root)[0]
    for rel, mode in DPKG_DIRS:
        try:
            path = _p(root, rel)
            st = os.lstat(path)
        except OSError:
            problems.append('/%s is missing (dpkg needs it)' % rel)
            continue
        override = owners.get(rel)
        want_mode = override[2] if (override is not None and override[2] is not None) else mode
        if not stat.S_ISDIR(st.st_mode):
            problems.append('/%s must be a directory (dpkg needs it)' % rel)
        elif ((st.st_uid, st.st_gid) != (0, 0) and override is None) or stat.S_IMODE(st.st_mode) != want_mode:
            problems.append('/%s must be root:root %04o (is %d:%d %04o)' % (
                rel, want_mode, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)))
    for rel, _ in DPKG_FILES:
        if not os.path.isfile(tpath(root, rel, follow=True)):
            problems.append('/%s is missing (dpkg needs it)' % rel)
    status = tpath(root, 'var/lib/dpkg/status', follow=True)
    if not os.path.isfile(status):
        problems.append('/var/lib/dpkg/status is missing: the image has no dpkg database')
    elif not os.access(status, os.R_OK):
        problems.append('/var/lib/dpkg/status is not readable')
    info = tpath(root, 'var/lib/dpkg/info', follow=True)
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
    recursive_bad = []
    home = tpath(root, HOME_ROOT, follow=True)
    rootdev = st.st_dev
    rules, _skipped = _special_owner_rules(root)
    stack = [root]
    while stack and (len(bad) < 10 or len(recursive_bad) < 10):
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
                rel = os.path.relpath(e.path, root)
                matched, is_exact = _matched_owner_rule(rel, rules)
                if matched is None:
                    if (s.st_uid, s.st_gid) != (0, 0) and len(bad) < 10:
                        bad.append(rel)
                elif not is_exact and len(recursive_bad) < 10:
                    want_uid, want_gid, rule_mode, masked, _rec = matched
                    want_mode = (
                        None if stat.S_ISLNK(s.st_mode)
                        else _expected_rule_mode(rule_mode, masked, s.st_mode, False)
                    )
                    uid_mismatch = want_uid is not None and s.st_uid != want_uid
                    gid_mismatch = want_gid is not None and s.st_gid != want_gid
                    mode_mismatch = want_mode is not None and stat.S_IMODE(s.st_mode) != want_mode
                    if uid_mismatch or gid_mismatch or mode_mismatch:
                        recursive_bad.append('/%s must be %s:%s%s (is %d:%d %04o)' % (
                            rel,
                            '-' if want_uid is None else str(want_uid),
                            '-' if want_gid is None else str(want_gid),
                            '' if want_mode is None else ' %04o' % want_mode,
                            s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)))
                if stat.S_ISDIR(s.st_mode) and not e.is_symlink():
                    stack.append(e.path)
    if bad:
        problems.append('entries not owned by root:root: ' + ', '.join(bad))
    problems += recursive_bad
    for rel, (uid, gid, mode, masked, _rec) in sorted(rules.items()):    # the deliberate exceptions must be exactly as declared
        try:
            path = _p(root, rel)
        except OSError:
            continue
        if not os.path.lexists(path) or os.path.islink(path):
            continue
        s = os.lstat(path)
        want_mode = _expected_rule_mode(mode, masked, s.st_mode, True)
        uid_mismatch = uid is not None and s.st_uid != uid
        gid_mismatch = gid is not None and s.st_gid != gid
        mode_mismatch = want_mode is not None and stat.S_IMODE(s.st_mode) != want_mode
        if uid_mismatch or gid_mismatch or mode_mismatch:
            problems.append('/%s must be %s:%s%s (is %d:%d %04o)' % (
                rel,
                '-' if uid is None else str(uid),
                '-' if gid is None else str(gid),
                '' if want_mode is None else ' %04o' % want_mode,
                s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)))
    problems += verify_dpkg(root)
    if username:
        pw = next((r for r in parse_colon_file(tpath(root, 'etc/passwd', follow=True)) if r and r[0] == username), None)
        if not pw:
            problems.append('user %s is missing from /etc/passwd' % username)
        else:
            uid, gid = int(pw[2]), int(pw[3])
            grp = next((r for r in parse_colon_file(tpath(root, 'etc/group', follow=True)) if len(r) > 2 and r[2] == str(gid)), None)
            if not grp or grp[0] != username:
                problems.append('the primary group of %s is %r, expected %r' % (username, grp[0] if grp else None, username))
            h = _p(root, HOME_ROOT + '/' + username)
            try:
                s = os.lstat(h)
                if not stat.S_ISDIR(s.st_mode):
                    problems.append('/home/%s is not a directory' % username)
                    h = None
                elif (s.st_uid, s.st_gid) != (uid, gid):
                    problems.append('/home/%s is owned by %d:%d, expected %d:%d (%s:%s)' % (username, s.st_uid, s.st_gid, uid, gid, username, username))
                for r, ds, fs in (os.walk(h) if h else ()):
                    for n in ds + fs:
                        s = os.lstat(os.path.join(r, n))
                        if (s.st_uid, s.st_gid) != (uid, gid):
                            problems.append('%s inside the home directory is owned by %d:%d' % (os.path.relpath(os.path.join(r, n), root), s.st_uid, s.st_gid))
                            break
            except OSError:
                problems.append('/home/%s does not exist' % username)
    try:
        with open(tpath(root, 'etc/machine-id', follow=True)) as f:
            mid = f.read()
        if not re.fullmatch(r'[0-9a-f]{32}\n', mid) or mid.strip('0\n') == '':
            problems.append('/etc/machine-id is not a valid machine ID')
    except OSError:
        problems.append('/etc/machine-id is missing')
    return problems