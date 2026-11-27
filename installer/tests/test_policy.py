"""The installer creates no directories and sets no owners or modes on the OS image.  What is left in policy.py is
the path safety for a foreign file system (absolute symlinks in the target must never reach the machine the installer
runs on) and the per-installation machine-id.  This test checks both, and that engine.py really leaves owners, modes
and directories alone."""
import ast, os, re, shutil, stat, sys, types
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, '..', 'installer')]
import engine, policy
res = []
def check(c, m): print(('  PASS  ' if c else '  FAIL  ') + m); res.append(bool(c))

def snap(top):
    out = {}
    for r_, ds_, fs_ in os.walk(top):
        for n_ in ds_ + fs_:
            p_ = os.path.join(r_, n_); st_ = os.lstat(p_)
            out[p_] = (stat.S_IMODE(st_.st_mode), st_.st_uid, st_.st_gid, st_.st_size, os.readlink(p_) if stat.S_ISLNK(st_.st_mode) else (open(p_, 'rb').read() if stat.S_ISREG(st_.st_mode) else None))
    out[top] = (stat.S_IMODE(os.lstat(top).st_mode), os.lstat(top).st_uid, os.lstat(top).st_gid, 0, None)
    return out

# ---- the module no longer contains anything that builds directories or sets owners / modes ----
src = open(os.path.join(HERE, '..', 'installer', 'policy.py')).read()
check(not re.search(r'\bos\.(chown|lchown|chmod|mkdir|makedirs)\(', src), 'policy.py makes no chown / chmod / mkdir call')
for gone in ('ensure_standard_dirs', 'repair_dir_modes', 'fix_special_dirs', 'enforce_root_ownership', 'apply_baseline_permissions',
             'repair_dpkg', 'standard_dir_conflicts', 'apply_special_owners', 'verify_policy', 'verify_dpkg', 'special_owners'):
    check(not hasattr(policy, gone), 'policy.%s is gone' % gone)
calls = [(n.lineno, ast.unparse(n.func)) for n in ast.walk(ast.parse(open(os.path.join(HERE, '..', 'installer', 'engine.py')).read()))
         if isinstance(n, ast.Call) and ast.unparse(n.func) in ('os.chown', 'os.lchown', 'os.chmod', 'os.mkdir', 'os.makedirs')]
check([c for _, c in calls] == ['os.makedirs'], 'engine.py: no chown/chmod; the only makedirs is the live mount point of the target (%s)' % calls)

# ---- a target full of absolute links into a simulated host ----
H = '/tmp/policy_hostsim'; G = '/tmp/policy_symtarget'
for d_ in (H, G): shutil.rmtree(d_, ignore_errors=True)
for d_ in ('dbus', 'apt/lists', 'home', 'dpkginfo', 'run', 'etc', 'usr_bin', 'tmpdir'): os.makedirs(os.path.join(H, d_))
open(H + '/passwd', 'w').write('host:x:0:0::/root:/bin/sh\n'); open(H + '/shadow', 'w').write('host-secret\n'); os.chmod(H + '/shadow', 0o640)
open(H + '/machine-id', 'w').write('0' * 32 + '\n'); open(H + '/hostfile', 'w').write('do not touch\n')
os.chmod(H + '/apt/lists', 0o700); os.chmod(H + '/dbus', 0o700)
for d_ in ('var/lib', 'etc', 'usr/bin', 'usr/lib', 'var/log', 'tmp'): os.makedirs(os.path.join(G, d_))
for link_, dst_ in (('var/lib/dbus', H + '/dbus'), ('var/lib/apt', H + '/apt'), ('etc/passwd', H + '/passwd'), ('etc/shadow', H + '/shadow'),
                    ('etc/machine-id', H + '/machine-id'), ('home', H + '/home'), ('run', H + '/run'), ('var/run', '/run'), ('etc/dpkg', H + '/etc')):
    os.symlink(dst_, os.path.join(G, link_))
before_host = snap(H)
os.symlink('/loop-b', G + '/loop-a'); os.symlink('/loop-a', G + '/loop-b')
try: policy.tpath(G, 'loop-a/x'); looped = False
except OSError as e_: looped = e_.errno == 40
check(looped, 'tpath raises ELOOP for a symlink loop instead of hanging or leaving the target')
os.unlink(G + '/loop-a'); os.unlink(G + '/loop-b')
check(policy.resolve_in_root(G, G + '/usr/../../../etc') == G + '/etc', "'..' cannot climb out of the target")
check(policy.tpath(G, 'var/lib/dbus', follow=True) == G + H + '/dbus', 'tpath resolves an absolute link against the TARGET (%s)' % policy.tpath(G, 'var/lib/dbus', follow=True).replace(G, '<target>'))
check(policy.tpath(G, 'var/lib/dbus/machine-id') == G + H + '/dbus/machine-id', 'tpath resolves parent directories inside the target (non-follow keeps the last component)')
check(os.path.islink(policy.tpath(G, 'etc/passwd')) and policy.tpath(G, 'etc/passwd') == G + '/etc/passwd', 'tpath without follow leaves the last component alone (so a link itself can be replaced)')
policy.init_machine_id(G)
check(snap(H) == before_host, 'init_machine_id: host untouched (var/lib/dbus and etc/machine-id are absolute links)')

# ---- machine-id: unique per call, owner and mode of the image's file are kept ----
R = '/tmp/policy_root'; shutil.rmtree(R, ignore_errors=True); os.makedirs(R + '/etc')
open(R + '/etc/machine-id', 'w').write(''); os.chmod(R + '/etc/machine-id', 0o444); os.chown(R + '/etc/machine-id', 0, 42)
inode = os.stat(R + '/etc/machine-id').st_ino
a = policy.init_machine_id(R); b = policy.init_machine_id(R)
check(re.fullmatch(r'[0-9a-f]{32}', a) and a != b, 'machine-id is 32 lowercase hex digits and differs every time (%s.. / %s..)' % (a[:8], b[:8]))
s_ = os.stat(R + '/etc/machine-id')
check(open(R + '/etc/machine-id').read() == b + '\n' and (s_.st_uid, s_.st_gid, stat.S_IMODE(s_.st_mode), s_.st_ino) == (0, 42, 0o444, inode),
      '/etc/machine-id is rewritten in place: owner 0:42, mode 0444 and inode are the image\'s own')
check(int(a[12], 16) == 4 and int(a[16], 16) in (8, 9, 10, 11), 'the ID is a version-4 UUID (as systemd generates)')
os.makedirs(R + '/var/lib/dbus'); policy.init_machine_id(R)
check(os.readlink(R + '/var/lib/dbus/machine-id') == '/etc/machine-id', 'D-Bus machine-id links to /etc/machine-id when D-Bus is present')

# ---- engine: mount points must come with the image, _write creates nothing ----
M = '/tmp/policy_chroot'; shutil.rmtree(M, ignore_errors=True)
for d_ in ('dev', 'proc', 'sys'): os.makedirs(M + '/' + d_)
ch_ = engine.Chroot(M)
try: ch_.__enter__(); raised = False
except engine.InstallError as e_: raised = '/run is missing' in str(e_)
check(raised and ch_.mounts == [] and not os.path.exists(M + '/run'), 'Chroot reports a mount point missing from the image, creates none and mounts nothing')
os.symlink('/tmp/policy_hostsim/run', M + '/run')                       # would be a mount over the host's directory
try: ch_.__enter__(); raised = False
except engine.InstallError as e_: raised = 'symbolic link' in str(e_)
check(raised and ch_.mounts == [], 'Chroot refuses a symlink as mount point and mounts nothing (checked before the first mount)')
os.unlink(M + '/run'); shutil.rmtree(M + '/proc'); open(M + '/proc', 'w').write('x'); os.makedirs(M + '/run')
try: ch_.__enter__(); raised = False
except engine.InstallError as e_: raised = 'not a directory' in str(e_)
check(raised and ch_.mounts == [], 'Chroot refuses a regular file as mount point')

inst = object.__new__(engine.Installer); inst.env = types.SimpleNamespace(target=G)
os.symlink(H + '/hostfile', G + '/etc/hostname')
old = os.umask(0o022)
inst._write('etc/hostname', 'newname\n')
os.umask(old)
check(open(H + '/hostfile').read() == 'do not touch\n' and not os.path.islink(G + '/etc/hostname') and open(G + '/etc/hostname').read() == 'newname\n',
      'Installer._write replaces an absolute link instead of writing through it to the host')
check(stat.S_IMODE(os.lstat(G + '/etc/hostname').st_mode) == 0o644, 'a file the installer writes is 0644 (umask 022), no chmod involved')
try: inst._write('var/nodir/x.cfg', 'y\n'); raised = False
except FileNotFoundError: raised = True
check(raised and not os.path.exists(G + '/var/nodir'), 'Installer._write does not create a missing directory')
inst._write('var/log/x.cfg', 'y\n')
check(snap(H) == before_host and open(H + '/hostfile').read() == 'do not touch\n', 'host file system byte-for-byte unchanged after everything above')

# ---- the whole configure step makes no chown / chmod / mkdir ----
T = '/tmp/policy_configure'; shutil.rmtree(T, ignore_errors=True)
for d_ in ('etc', 'tmp', 'boot/efi', 'usr/bin'): os.makedirs(T + '/' + d_)
open(T + '/etc/os-release', 'w').write('NAME=ouatos\n'); open(T + '/etc/machine-id', 'w').write('')
open(T + '/etc/group', 'w').write('root:x:0:\nsudo:x:27:\n'); open(T + '/usr/bin/dpkg', 'w').write('')
cfg = engine.Config(types.SimpleNamespace(path='/dev/x'), 'ouatos', 'alice', 'Alice', 'pw', root_password='rootpw')
job = object.__new__(engine.Installer)
job.cfg, job.env, job.osr, job.info, job._progress = cfg, types.SimpleNamespace(target=T), {}, {}, lambda *a: None
job.parts = [engine.Part('root', 1, 2, engine.GUID_LINUX, 'ouatos', 'ext4'), engine.Part('esp', 3, 4, engine.GUID_ESP, 'esp', 'vfat')]
job.parts[0].fs_uuid, job.parts[1].fs_uuid = 'ROOT-UUID', 'ABCD-1234'
fake = mock.MagicMock(); fake.run.return_value = types.SimpleNamespace(returncode=0)
touched = []
def spy(name):
    return lambda *a, **k: touched.append(name)
with mock.patch.object(engine, 'Chroot', return_value=fake), mock.patch.object(engine, 'log'), \
     mock.patch.object(os, 'chown', spy('chown')), mock.patch.object(os, 'lchown', spy('lchown')), mock.patch.object(os, 'chmod', spy('chmod')), \
     mock.patch.object(os, 'mkdir', spy('mkdir')), mock.patch.object(os, 'makedirs', spy('makedirs')):
    job.step_configure()
check(touched == [], 'step_configure: no chown, chmod, mkdir or makedirs call (%s)' % touched)
cmds = [c.args[0][0] for c in fake.run.call_args_list]
check('/usr/sbin/useradd' in cmds and '/usr/sbin/chpasswd' in cmds, 'the accounts are still created (useradd -m, chpasswd)')
check(open(T + '/etc/hostname').read() == 'ouatos\n' and 'UUID=ABCD-1234\t/boot/efi' in open(T + '/etc/fstab').read(), 'hostname and fstab (with the ESP) are written')

# ---- layout: UEFI only ----
parts = engine.plan_layout(8 * engine.GiB, 512, 0)
check([p.kind for p in parts] == ['esp', 'root'] and parts[0].guid == engine.GUID_ESP, 'plan_layout: ESP + root, no BIOS boot partition')
check([p.kind for p in engine.plan_layout(30 * engine.GiB, 512, engine.GiB)] == ['esp', 'root', 'swap'], 'plan_layout: ESP + root + swap')
check(not hasattr(engine, 'GUID_BIOS') and not hasattr(engine.Installer, '_install_bios') and not hasattr(engine.Env, 'uefi'), 'no BIOS code is left in the engine')

for d_ in (H, G, M, R, T): shutil.rmtree(d_, ignore_errors=True)
print('\nRESULT:', 'ALL POLICY TESTS PASSED' if all(res) else 'FAILURES: %d' % res.count(False))
sys.exit(0 if all(res) else 1)
