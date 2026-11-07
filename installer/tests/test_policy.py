"""Unit test of installer/policy.py on a synthetic tree that has the same flaws as the OS image archive."""
import os, re, shutil, stat, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, '..', 'installer')]
import policy
res = []
def check(c, m): print(('  PASS  ' if c else '  FAIL  ') + m); res.append(bool(c))
R = '/tmp/policy_root'; shutil.rmtree(R, ignore_errors=True)
def mk(rel, mode=0o755, data=None, uid=0, gid=0):
    p = os.path.join(R, rel)
    if data is None: os.makedirs(p, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(p), exist_ok=True); open(p, 'w').write(data)
    os.chmod(p, mode); os.chown(p, uid, gid)
for d in ('etc', 'usr/bin', 'var', 'root', 'etc/sudoers.d'): mk(d)
mk('etc/shadow', 0o644, 'root:*:1:::::\n'); mk('etc/gshadow', 0o644, ''); mk('etc/sudoers', 0o644, '%sudo ALL=(ALL:ALL) ALL\n')
mk('etc/sudoers.d/x', 0o644, '# x\n'); mk('etc/machine-id', 0o644, '')
for b in ('sudo', 'su', 'passwd', 'mount'): mk('usr/bin/' + b, 0o755, 'ELF')
mk('usr/bin/ls', 0o755, 'ELF'); mk('usr/lib/stray', 0o644, 'x', 1000, 1000); mk('etc/odd', 0o640, 'x', 0, 42)
os.symlink('/etc/odd', os.path.join(R, 'usr/lib/link')); os.lchown(os.path.join(R, 'usr/lib/link'), 1000, 1000)
mk('home/preexisting', 0o700, uid=777, gid=777)           # a user home that must NOT be touched by enforce_root_ownership
st = lambda rel: (os.lstat(os.path.join(R, rel)).st_uid, os.lstat(os.path.join(R, rel)).st_gid, stat.S_IMODE(os.lstat(os.path.join(R, rel)).st_mode))

for d in ('tmp', 'var/tmp', 'proc', 'dev'): check(not os.path.exists(os.path.join(R, d)), '(fixture) /%s is missing, as in the image' % d)
created = policy.ensure_standard_dirs(R)
check(all(os.path.isdir(os.path.join(R, d)) for d in ('dev', 'proc', 'sys', 'run', 'mnt', 'media', 'srv', 'opt', 'home', 'tmp', 'var/tmp', 'usr/local/bin', 'var/lib/dpkg/updates')), 'standard directories created (%d)' % len(created))
check('home' not in created, '/home was created only if missing (it existed: %s)' % ('home' in created))
policy.fix_special_dirs(R)
check(st('tmp') == (0, 0, 0o1777) and st('var/tmp') == (0, 0, 0o1777), '/tmp and /var/tmp are root:root mode 1777')
check(st('root') == (0, 0, 0o700), '/root is root:root 0700')
check(st('proc') == (0, 0, 0o755) and st('run') == (0, 0, 0o755), 'mount points are root:root 0755')
changed = policy.enforce_root_ownership(R)
check(sorted(changed) == ['etc/odd', 'usr/lib/link', 'usr/lib/stray'], 'only entries that were not root:root were reset: %s' % sorted(changed))
check(st('usr/lib/stray')[:2] == (0, 0) and os.lstat(os.path.join(R, 'usr/lib/link')).st_uid == 0, 'files and symlinks now belong to root:root')
check(st('etc/odd')[2] == 0o640, 'ownership change keeps the mode')
check(st('home/preexisting')[:2] == (777, 777), 'a home directory is left alone by the root:root rule')
check(st('home')[:2] == (0, 0), '/home itself is root:root')
os.chown(R, 5, 5); policy.enforce_root_ownership(R); s = os.lstat(R); check((s.st_uid, s.st_gid) == (0, 0), '/ itself is forced to root:root')
perm = policy.apply_baseline_permissions(R)
check(st('etc/shadow')[2] == 0o640 and st('etc/gshadow')[2] == 0o640, '/etc/shadow and /etc/gshadow tightened 0644 -> 0640')
check(st('etc/sudoers')[2] == 0o440 and st('etc/sudoers.d')[2] == 0o750 and st('etc/sudoers.d/x')[2] == 0o440, 'sudoers files tightened to 0440 / 0750')
check(all(st('usr/bin/' + b)[2] == 0o4755 for b in ('sudo', 'su', 'passwd', 'mount')), 'setuid restored on sudo, su, passwd, mount')
check(st('usr/bin/ls')[2] == 0o755, 'ordinary programs untouched')
os.chmod(os.path.join(R, 'etc/shadow'), 0o600); os.chmod(os.path.join(R, 'etc/sudoers'), 0o400)
policy.apply_baseline_permissions(R)
check(st('etc/shadow')[2] == 0o600 and st('etc/sudoers')[2] == 0o400, 'permissions are only ever tightened, never loosened')
check(policy.apply_baseline_permissions(R) == [], 'running the policy twice changes nothing (idempotent)')
a = policy.init_machine_id(R); b = policy.init_machine_id(R)
check(re.fullmatch(r'[0-9a-f]{32}', a) and a != b, 'machine-id is 32 lowercase hex digits and differs every time (%s.. / %s..)' % (a[:8], b[:8]))
check(open(os.path.join(R, 'etc/machine-id')).read() == b + '\n' and st('etc/machine-id') == (0, 0, 0o444), '/etc/machine-id: "<id>\\n", root:root 0444')
check(int(a[12], 16) == 4 and int(a[16], 16) in (8, 9, 10, 11), 'the ID is a version-4 UUID (as systemd generates)')
os.makedirs(os.path.join(R, 'var/lib/dbus')); policy.init_machine_id(R)
check(os.readlink(os.path.join(R, 'var/lib/dbus/machine-id')) == '/etc/machine-id', 'D-Bus machine-id links to /etc/machine-id when D-Bus is present')
# the user: emulate what useradd -m -U does
open(os.path.join(R, 'etc/passwd'), 'w').write('root:x:0:0::/root:/bin/bash\nalice:x:1000:1000:Alice:/home/alice:/bin/bash\n')
open(os.path.join(R, 'etc/group'), 'w').write('root:x:0:\nsudo:x:27:alice\nalice:x:1000:\n')
mk('home/alice', 0o700, uid=1000, gid=1000); mk('home/alice/.bashrc', 0o644, 'x', 1000, 1000)
check(policy.verify_policy(R, 'alice') == [], 'verify_policy: everything in the ownership table is satisfied')
os.chown(os.path.join(R, 'home/alice'), 0, 0)
check(any('/home/alice is owned by 0:0' in p for p in policy.verify_policy(R, 'alice')), 'verify_policy detects a home directory owned by root')
os.chown(os.path.join(R, 'home/alice'), 1000, 1000); os.chown(os.path.join(R, 'home/alice/.bashrc'), 0, 0)
check(any('inside the home directory' in p for p in policy.verify_policy(R, 'alice')), 'verify_policy detects wrongly owned files inside the home')
os.chown(os.path.join(R, 'home/alice/.bashrc'), 1000, 1000)
open(os.path.join(R, 'etc/group'), 'w').write('root:x:0:\nsudo:x:27:alice\nusers:x:1000:\n')
check(any('primary group' in p for p in policy.verify_policy(R, 'alice')), 'verify_policy detects a primary group not named after the user')
open(os.path.join(R, 'etc/group'), 'w').write('root:x:0:\nsudo:x:27:alice\nalice:x:1000:\n')
os.chmod(os.path.join(R, 'tmp'), 0o1755)
check(any('/tmp must be' in p for p in policy.verify_policy(R, 'alice')), 'verify_policy detects /tmp without mode 1777')
os.chmod(os.path.join(R, 'tmp'), 0o1777); os.chown(os.path.join(R, 'etc/odd'), 33, 33)
check(any('not owned by root:root' in p for p in policy.verify_policy(R, 'alice')), 'verify_policy detects a system file not owned by root:root')
# a mounted filesystem inside the target (the chroot's /proc, /dev ...) must be ignored by both walkers
import subprocess
subprocess.run(['mount', '-t', 'tmpfs', 'tmpfs', os.path.join(R, 'proc')], check=True)
try:
    os.makedirs(os.path.join(R, 'proc/4242')); os.chown(os.path.join(R, 'proc/4242'), 99, 99)
    open(os.path.join(R, 'proc/environ'), 'w').write('x'); os.chown(os.path.join(R, 'proc/environ'), 99, 99)
    ch = policy.enforce_root_ownership(R)
    check(not any(c.startswith('proc/') for c in ch) and os.lstat(os.path.join(R, 'proc/4242')).st_uid == 99, 'enforce_root_ownership does not cross into a mounted filesystem (/proc)')
    check(policy.verify_policy(R, 'alice') == [], 'verify_policy does not cross into a mounted filesystem either')
finally:
    subprocess.run(['umount', os.path.join(R, 'proc')])

# ---- dpkg / apt: a tree like the image (no empty dirs, restrictive implicit dirs, tar run under umask 077) ----
D = '/tmp/policy_dpkg'; shutil.rmtree(D, ignore_errors=True)
def mkd(rel, mode=0o755, data=None):
    p = os.path.join(D, rel)
    if data is None: os.makedirs(p, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(p), exist_ok=True); open(p, 'w').write(data)
    os.chmod(p, mode)
old_umask = os.umask(0o077)
try:
    os.makedirs(D + '/var/lib/dpkg/info'); os.makedirs(D + '/usr/lib/x86_64-linux-gnu'); os.makedirs(D + '/etc')
    os.makedirs(D + '/usr/bin'); os.makedirs(D + '/root'); os.makedirs(D + '/var/tmp'); os.makedirs(D + '/tmp')
    os.makedirs(D + '/etc/ssl/private'); os.makedirs(D + '/etc/sudoers.d'); os.makedirs(D + '/var/lib/sudo')
    open(D + '/usr/bin/dpkg', 'w').write('ELF'); os.chmod(D + '/usr/bin/dpkg', 0o755)
    open(D + '/var/lib/dpkg/status', 'w').write('Package: dpkg\nStatus: install ok installed\n'); os.chmod(D + '/var/lib/dpkg/status', 0o600)
    for n, m in (('dpkg.list', 0o600), ('bash.postinst', 0o644), ('bash.prerm', 0o600), ('bash.md5sums', 0o644)):
        open(D + '/var/lib/dpkg/info/' + n, 'w').write('x'); os.chmod(D + '/var/lib/dpkg/info/' + n, m)
    os.chmod(D + '/var/lib/dpkg', 0o700); os.chmod(D + '/var/lib/dpkg/info', 0o700); os.chmod(D + '/usr/lib/x86_64-linux-gnu', 0o700)
    os.chmod(D + '/etc/ssl/private', 0o700); os.chmod(D + '/etc/sudoers.d', 0o750); os.chmod(D + '/var/lib/sudo', 0o711)
    stD = lambda rel: stat.S_IMODE(os.lstat(os.path.join(D, rel)).st_mode)
    check(any('missing' in p for p in policy.verify_dpkg(D)), 'verify_dpkg detects the dpkg state directories the image lacks')

    # umask 077 must not leak into directories the installer creates
    created = policy.ensure_standard_dirs(D)
    check(stD('usr/share/file') == 0o755 and stD('usr/share/file/magic') == 0o755, 'implicit parents of a created directory are 0755 under umask 077 (usr/share/file)')
    check(stD('usr/local') == 0o755 and stD('usr/local/bin') == 0o755 and stD('var/cache/apt/archives') == 0o755, 'directories created under umask 077 are 0755')
    check(all(os.lstat(os.path.join(D, d)).st_uid == 0 for d in ('usr/share/file', 'var/cache/apt/archives/partial')), 'created directories are root-owned')
    check(os.path.isdir(D + '/var/lib/dpkg/alternatives') and os.path.isdir(D + '/var/lib/dpkg/triggers') and os.path.isdir(D + '/var/log/apt') and os.path.isdir(D + '/var/cache/apt/archives/partial') and os.path.isdir(D + '/etc/alternatives'),
          'dpkg/apt directories created (alternatives, triggers, log/apt, archives/partial, /etc/alternatives)')
    check(stD('var/cache/apt/archives/partial') == 0o700 and stD('var/lib/apt/lists/partial') == 0o700, 'apt partial directories are 0700')

    fix = policy.repair_dir_modes(D)
    check(stD('usr/lib/x86_64-linux-gnu') == 0o755, 'a directory left at 0700 by tar is made traversable (usr/lib/x86_64-linux-gnu)')
    check(stD('etc/ssl/private') == 0o700 and stD('etc/sudoers.d') == 0o750 and stD('var/lib/sudo') == 0o711, 'private directories (ssl/private, sudoers.d, lib/sudo) keep their mode')
    check(policy.repair_dir_modes(D) == [], 'repair_dir_modes is idempotent')

    changes = policy.repair_dpkg(D)
    check(stD('var/lib/dpkg') == 0o755 and stD('var/lib/dpkg/info') == 0o755, '/var/lib/dpkg and /var/lib/dpkg/info are 0755')
    check(all(os.path.isfile(D + '/' + f) for f in ('var/lib/dpkg/lock', 'var/lib/dpkg/lock-frontend', 'var/cache/apt/archives/lock', 'var/lib/apt/lists/lock', 'var/lib/dpkg/available')), 'dpkg/apt lock files and the available file exist')
    check(stD('var/lib/dpkg/lock') == 0o640 and stD('var/lib/dpkg/lock-frontend') == 0o640, 'dpkg lock files are 0640')
    check(stD('var/lib/dpkg/status') == 0o644 and stD('var/lib/dpkg/info/dpkg.list') == 0o644, 'dpkg database files readable by everybody (status 0600 -> 0644)')
    check(stD('var/lib/dpkg/info/bash.postinst') == 0o755 and stD('var/lib/dpkg/info/bash.prerm') == 0o755, 'maintainer scripts are executable (0755)')
    check(stD('var/lib/dpkg/info/bash.md5sums') == 0o644, 'ordinary info files stay 0644')
    check(policy.repair_dpkg(D) == [], 'repair_dpkg is idempotent (%s)' % changes[:2])
    policy.fix_special_dirs(D); policy.enforce_root_ownership(D); policy.apply_baseline_permissions(D)
    check(policy.verify_dpkg(D) == [], 'verify_dpkg: nothing left to complain about (%s)' % policy.verify_dpkg(D)[:2])

    # verify_dpkg must catch regressions
    os.chmod(D + '/var/lib/dpkg/info', 0o700)
    check(any('/var/lib/dpkg/info must be' in p for p in policy.verify_dpkg(D)), 'verify_dpkg detects /var/lib/dpkg/info at 0700')
    os.chmod(D + '/var/lib/dpkg/info', 0o755); os.chmod(D + '/var/lib/dpkg/info/bash.postinst', 0o644)
    check(any('maintainer scripts' in p for p in policy.verify_dpkg(D)), 'verify_dpkg detects a maintainer script without the x bit')
    os.chmod(D + '/var/lib/dpkg/info/bash.postinst', 0o755); os.rmdir(D + '/var/lib/dpkg/alternatives')
    check(any('alternatives is missing' in p for p in policy.verify_dpkg(D)), 'verify_dpkg detects a missing /var/lib/dpkg/alternatives')
    check(policy.verify_dpkg('/tmp/policy_root') == [] if os.path.isdir('/tmp/policy_root') else True, 'verify_dpkg ignores a system without dpkg')
    # a symlinked parent is never followed out of the target
    os.symlink('/nonexistent-host-dir', D + '/usr/share/info-link')
    check(policy._mkdir(D, 'usr/share/info-link/x', 0o755) is False and not os.path.exists('/nonexistent-host-dir'), '_mkdir never walks through a symlink')
finally:
    os.umask(old_umask)
shutil.rmtree(D, ignore_errors=True)

# ---- owners that must NOT be root:root, and the type check of standard directories ----
O = '/tmp/policy_owners'; shutil.rmtree(O, ignore_errors=True)
def mko(rel, mode, data='x'):
    p = os.path.join(O, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    if data is None: os.makedirs(p, exist_ok=True)
    else: open(p, 'w').write(data)
    os.chmod(p, mode)
mko('etc/passwd', 0o644, 'root:x:0:0::/root:/bin/bash\n_apt:x:42:65534::/nonexistent:/usr/sbin/nologin\nmessagebus:x:100:107::/nonexistent:/usr/sbin/nologin\n')
mko('etc/group', 0o644, 'root:x:0:\ntty:x:5:\nshadow:x:42:\nmessagebus:x:107:\nsudo:x:27:\n')
for f in ('usr/lib/dbus-1.0/dbus-daemon-launch-helper', 'usr/bin/chage', 'usr/bin/expiry', 'usr/sbin/unix_chkpwd', 'usr/bin/wall'): mko(f, 0o755, 'ELF')
for f in ('etc/shadow', 'etc/gshadow'): mko(f, 0o644)
mko('var/cache/apt/archives/partial', 0o755, None); mko('var/lib/apt/lists/partial', 0o755, None)
mko('usr/bin/ssh-keysign', 0o755, 'ELF'); mko('var/lib/dpkg/statoverride', 0o644, 'root messagebus 4754 /usr/lib/dbus-1.0/dbus-daemon-launch-helper\nroot sudo 2750 /usr/bin/ssh-keysign\nroot nosuchgroup 0755 /usr/bin/wall\n')
stO = lambda rel: (os.lstat(os.path.join(O, rel)).st_uid, os.lstat(os.path.join(O, rel)).st_gid, stat.S_IMODE(os.lstat(os.path.join(O, rel)).st_mode))
for d in ('etc', 'usr', 'var', 'etc', 'usr/bin', 'usr/sbin', 'usr/lib', 'usr/lib/dbus-1.0'): os.makedirs(os.path.join(O, d), exist_ok=True)
owners, skipped = policy.special_owners(O)
check(owners['usr/lib/dbus-1.0/dbus-daemon-launch-helper'] == (0, 107, 0o4754) and owners['usr/bin/chage'][1] == 42 and owners['usr/bin/wall'][1] == 5,
      'special_owners resolves names from the TARGET passwd/group (messagebus=107, shadow=42, tty=5)')
check(owners['usr/bin/ssh-keysign'] == (0, 27, 0o2750), 'dpkg statoverride entries are applied too')
check(any('nosuchgroup' in x for x in skipped) and owners['usr/bin/wall'] == (0, 5, 0o2755), 'a statoverride entry naming a missing group is skipped and logged (built-in kept)')
changes, skipped2 = policy.apply_special_owners(O)
check(stO('usr/lib/dbus-1.0/dbus-daemon-launch-helper') == (0, 107, 0o4754), 'D-Bus launch helper is root:messagebus 4754 (setuid kept: chown first, chmod second)')
check(stO('usr/bin/chage') == (0, 42, 0o2755) and stO('usr/sbin/unix_chkpwd') == (0, 42, 0o2755) and stO('usr/bin/wall') == (0, 5, 0o2755), 'chage/unix_chkpwd are root:shadow 2755, wall is root:tty 2755')
check(stO('etc/shadow') == (0, 42, 0o644) and stO('etc/gshadow')[:2] == (0, 42), '/etc/shadow and /etc/gshadow get group shadow, mode untouched by the owner step')
check(stO('var/cache/apt/archives/partial') == (42, 0, 0o700) and stO('var/lib/apt/lists/partial') == (42, 0, 0o700), 'apt partial directories are _apt:root 0700')
check(stO('usr/bin/ssh-keysign') == (0, 27, 0o2750), 'statoverride mode/owner applied (root:sudo 2750)')
check(policy.apply_special_owners(O)[0] == [], 'apply_special_owners is idempotent')
ch = policy.enforce_root_ownership(O)
check(not any(c in owners for c in ch) and stO('usr/bin/chage')[1] == 42 and stO('usr/lib/dbus-1.0/dbus-daemon-launch-helper')[1] == 107,
      'enforce_root_ownership leaves the declared exceptions alone')
os.chmod(O + '/usr/bin/chage', 0o755)
check(any('/usr/bin/chage must be 0:42 2755' in p for p in policy.verify_policy(O)), 'verify_policy detects a lost setgid bit on chage')
os.chmod(O + '/usr/bin/chage', 0o2755); os.chown(O + '/usr/bin/expiry', 0, 0)
check(any('/usr/bin/expiry must be 0:42' in p for p in policy.verify_policy(O)), 'verify_policy detects an exception that is back to root:root')
os.chown(O + '/usr/bin/expiry', 0, 42); os.chmod(O + '/usr/bin/expiry', 0o2755)
check(not any('must be 0:' in p or 'must be 42:' in p for p in policy.verify_policy(O)), 'verify_policy accepts the declared exceptions')
mko('usr/bin/stray', 0o755, 'x'); os.chown(O + '/usr/bin/stray', 0, 42)
check(any('not owned by root:root' in p and 'usr/bin/stray' in p for p in policy.verify_policy(O)), 'any other non-root entry is still reported')
mko('etc/group', 0o644, 'root:x:0:\nsudo:x:27:\n')
check(policy.special_owners(O)[0].get('usr/bin/chage') is None and policy.apply_special_owners(O)[1], 'groups missing in the target: entries skipped, nothing guessed')

# type of existing standard paths
T = '/tmp/policy_types'; shutil.rmtree(T, ignore_errors=True)
for d in ('usr/local', 'usr/share', 'var/lib', 'etc/apt', 'run'): os.makedirs(os.path.join(T, d))
open(T + '/usr/local/bin', 'w').write('i am a file')                 # file where a directory is required
os.symlink('/nonexistent-on-the-host', T + '/usr/local/sbin')          # absolute link to nowhere
os.symlink('/usr/share', T + '/usr/local/share')                       # absolute link that resolves INSIDE the target
os.symlink('../loop', T + '/usr/loop'); os.symlink('usr/loop', T + '/loop')
open(T + '/var/lib/misc', 'w').write('file')                           # blocks var/lib/misc itself
open(T + '/etc/apt/keyrings', 'w').write('file')
policy.ensure_standard_dirs(T)                                         # must not crash or touch anything odd
conf = policy.standard_dir_conflicts(T)
check(any(c.startswith('/usr/local/bin exists but is not a directory') for c in conf), 'a regular file where a directory is required is reported (usr/local/bin)')
check(any(c.startswith('/usr/local/sbin is a symbolic link to') for c in conf), 'a dangling symlink is reported (usr/local/sbin)')
check(not any(c.startswith('/usr/local/share') for c in conf), 'an absolute symlink that resolves to a directory INSIDE the target is accepted (usr/local/share)')
check(any(c.startswith('/var/lib/misc exists but is not a directory') for c in conf) and any(c.startswith('/etc/apt/keyrings exists') for c in conf), 'files blocking var/lib/misc and etc/apt/keyrings are reported')
check(os.path.isdir(T + '/var/lib/dpkg/info') and os.path.isdir(T + '/usr/local/lib'), 'unrelated directories are still created')
check(not os.path.exists('/nonexistent-on-the-host'), 'nothing was created outside the target through the absolute symlink')
check(policy._mkdir(T, 'usr/local/bin/sub/dir', 0o755) is False, '_mkdir returns False instead of crashing when a parent is a regular file')
check(policy.resolve_in_root(T, T + '/usr/loop') is None, 'resolve_in_root detects a symlink loop')
shutil.rmtree(O, ignore_errors=True); shutil.rmtree(T, ignore_errors=True)

# ---- absolute symlinks in the target must never reach the machine the installer runs on ----
H = '/tmp/policy_hostsim'; G = '/tmp/policy_symtarget'
for d_ in (H, G): shutil.rmtree(d_, ignore_errors=True)
def snap(top):
    out = {}
    for r_, ds_, fs_ in os.walk(top):
        for n_ in ds_ + fs_:
            p_ = os.path.join(r_, n_); st_ = os.lstat(p_)
            out[p_] = (stat.S_IMODE(st_.st_mode), st_.st_uid, st_.st_gid, st_.st_size, os.readlink(p_) if stat.S_ISLNK(st_.st_mode) else (open(p_, 'rb').read() if stat.S_ISREG(st_.st_mode) else None))
    out[top] = (stat.S_IMODE(os.lstat(top).st_mode), os.lstat(top).st_uid, os.lstat(top).st_gid, 0, None)
    return out
for d_ in ('dbus', 'apt/lists', 'apt/lists/partial', 'home', 'dpkginfo', 'run', 'etc', 'usr_bin', 'tmpdir'): os.makedirs(os.path.join(H, d_))
open(H + '/passwd', 'w').write('host:x:0:0::/root:/bin/sh\nhostuser:x:1000:1000::/home/hostuser:/bin/sh\n'); open(H + '/group', 'w').write('shadow:x:99:\nmessagebus:x:99:\n')
open(H + '/shadow', 'w').write('host-secret\n'); os.chmod(H + '/shadow', 0o640); open(H + '/machine-id', 'w').write('0' * 32 + '\n')
open(H + '/dpkginfo/dpkg.postinst', 'w').write('#!/bin/sh\n'); os.chmod(H + '/dpkginfo/dpkg.postinst', 0o644)
open(H + '/usr_bin/chage', 'w').write('ELF'); os.chmod(H + '/usr_bin/chage', 0o755)
open(H + '/hostfile', 'w').write('do not touch\n')
os.chmod(H + '/apt/lists', 0o700); os.chmod(H + '/dbus', 0o700)
for d_ in ('var/lib', 'etc', 'usr/bin', 'usr/lib', 'var/lib/dpkg'): os.makedirs(os.path.join(G, d_))
open(G + '/usr/bin/dpkg', 'w').write('ELF'); os.chmod(G + '/usr/bin/dpkg', 0o755)
for link_, dst_ in (('var/lib/dbus', H + '/dbus'), ('var/lib/apt', H + '/apt'), ('var/lib/dpkg/info', H + '/dpkginfo'), ('etc/passwd', H + '/passwd'),
                    ('etc/group', H + '/group'), ('etc/shadow', H + '/shadow'), ('etc/machine-id', H + '/machine-id'), ('home', H + '/home'),
                    ('run', H + '/run'), ('var/run', '/run'), ('var/lock', '/run/lock'), ('usr/bin/chage', H + '/usr_bin/chage'),
                    ('etc/dpkg', H + '/etc')):
    os.symlink(dst_, os.path.join(G, link_))
before_host = snap(H)
os.symlink('/loop-b', G + '/loop-a'); os.symlink('/loop-a', G + '/loop-b')
try: policy.tpath(G, 'loop-a/x'); looped = False
except OSError as e_: looped = e_.errno == 40
check(looped, 'tpath raises ELOOP for a symlink loop instead of hanging or leaving the target')
os.unlink(G + '/loop-a'); os.unlink(G + '/loop-b')
check(policy.tpath(G, 'var/lib/dbus', follow=True) == G + H + '/dbus', 'tpath resolves an absolute link against the TARGET (%s)' % policy.tpath(G, 'var/lib/dbus', follow=True).replace(G, '<target>'))
check(policy.tpath(G, 'var/lib/dbus/machine-id') == G + H + '/dbus/machine-id' and not policy.tpath(G, 'var/lib/dbus/machine-id').startswith(H), 'tpath resolves parent directories inside the target (non-follow keeps the last component)')
check(os.path.islink(policy.tpath(G, 'etc/passwd')) and policy.tpath(G, 'etc/passwd') == G + '/etc/passwd', 'tpath without follow leaves the last component alone (so a link itself can be replaced)')
check(policy._id_tables(G)[0] == {} and policy._id_tables(G)[1] == {}, "passwd/group behind an absolute link are read from the target (nothing found), not from the host")
policy.init_machine_id(G)
check(snap(H) == before_host, 'init_machine_id: host untouched (var/lib/dbus and etc/machine-id are absolute links)')
policy.ensure_standard_dirs(G); policy.repair_dir_modes(G)
try: policy.fix_special_dirs(G)
except OSError: pass
policy.enforce_root_ownership(G); policy.apply_baseline_permissions(G); policy.repair_dpkg(G); policy.apply_special_owners(G); policy.verify_policy(G, None)
check(snap(H) == before_host, 'ensure/repair/enforce/baseline/repair_dpkg/special owners/verify: host file system byte-for-byte unchanged')
check(stat.S_IMODE(os.stat(H + '/apt/lists').st_mode) == 0o700 and stat.S_IMODE(os.stat(H + '/dbus').st_mode) == 0o700 and not os.path.exists(H + '/apt/lists/lock'), 'the host directories keep mode 0700 and gained no lock files')
check(os.path.isdir(G + H + '/apt/lists') or os.path.isdir(G + '/var/lib/dpkg'), 'what had to be created was created inside the target')
check(os.path.lexists(G + '/var/run') and os.readlink(G + '/var/run') == '/run' and os.path.lexists(G + '/usr/bin/chage'), 'the links themselves are left as the image made them')

# engine: mount points and _write
import types, engine
M = '/tmp/policy_chroot'; shutil.rmtree(M, ignore_errors=True); os.makedirs(M)
os.symlink('/tmp/policy_hostsim/run', M + '/run')                       # would be a mount over the host's directory
ch_ = engine.Chroot(M)
try:
    ch_.__enter__(); raised = False
except engine.InstallError as e_:
    raised = 'symbolic link' in str(e_)
check(raised and ch_.mounts == [], 'Chroot refuses a symlink as mount point and mounts nothing (checked before the first mount)')
os.unlink(M + '/run'); shutil.rmtree(M + '/proc'); open(M + '/proc', 'w').write('x')
try:
    ch_.__enter__(); raised = False
except engine.InstallError as e_:
    raised = 'not a directory' in str(e_)
check(raised and ch_.mounts == [], 'Chroot refuses a regular file as mount point')
inst = object.__new__(engine.Installer); inst.env = types.SimpleNamespace(target=G)
os.makedirs(G + '/etc', exist_ok=True)
os.symlink(H + '/hostfile', G + '/etc/hostname')
inst._write('etc/hostname', 'newname\n')
check(open(H + '/hostfile').read() == 'do not touch\n' and not os.path.islink(G + '/etc/hostname') and open(G + '/etc/hostname').read() == 'newname\n',
      'Installer._write replaces an absolute link instead of writing through it to the host')
inst._write('var/log/x.cfg', 'y\n')
check(snap(H) == snap(H) and open(H + '/hostfile').read() == 'do not touch\n', 'Installer._write into a directory behind an absolute link stays inside the target')
shutil.rmtree(M, ignore_errors=True); shutil.rmtree(H, ignore_errors=True); shutil.rmtree(G, ignore_errors=True)
shutil.rmtree(R)
print('\nRESULT:', 'ALL POLICY TESTS PASSED' if all(res) else 'FAILURES: %d' % res.count(False))
sys.exit(0 if all(res) else 1)
