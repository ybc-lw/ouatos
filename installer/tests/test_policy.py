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
shutil.rmtree(R)
print('\nRESULT:', 'ALL POLICY TESTS PASSED' if all(res) else 'FAILURES: %d' % res.count(False))
sys.exit(0 if all(res) else 1)
