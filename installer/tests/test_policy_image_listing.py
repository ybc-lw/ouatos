"""Runs the whole policy sequence of engine.step_configure on a fixture rebuilt from the real image's `ls -aR`
listing (tests/image_listing.txt.xz): no empty directories, every file 0644, every directory made by tar under
umask 077.  After the repair dpkg/apt must have everything they need.  Override the listing with
OUATOS_IMAGE_LISTING=/path/to/ls-aR.txt."""
import lzma, os, shutil, stat, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, '..', 'installer')]
import policy
res = []
def check(c, m): print(('  PASS  ' if c else '  FAIL  ') + m); res.append(bool(c))

path = os.environ.get('OUATOS_IMAGE_LISTING')
raw = open(path, 'rb').read() if path else lzma.open(os.path.join(HERE, 'image_listing.txt.xz')).read()
secs, cur = {}, None
for line in raw.decode('utf-8', 'replace').split('\n'):
    if line.endswith(':') and line.startswith('.'):
        cur = line[:-1]; secs[cur] = []
    elif cur is not None and line and line not in ('.', '..'):
        secs[cur].append(line)

R = '/tmp/policy_listing_root'; shutil.rmtree(R, ignore_errors=True)
old = os.umask(0o077)
try:
    os.makedirs(R)
    for d, names in secs.items():
        base = os.path.join(R, d[2:]) if d != '.' else R
        os.makedirs(base, exist_ok=True)
        for n in names:
            p = os.path.join(base, n)
            if (d + '/' + n) in secs or os.path.lexists(p):
                continue
            with open(p, 'wb'):
                pass
            os.chmod(p, 0o644)                                   # flattened: no x bits, no setuid
    for b in ('dpkg', 'sudo'):                                    # programs keep their x bit in the archive
        os.chmod(os.path.join(R, 'usr/bin', b), 0o755)
    for dp, ds, fs in os.walk(R):                                 # tar under umask 077 left 0700 directories
        for dn in ds:
            if not os.path.islink(os.path.join(dp, dn)):
                os.chmod(os.path.join(dp, dn), 0o700)
    S = lambda rel: stat.S_IMODE(os.lstat(os.path.join(R, rel)).st_mode)

    check(not os.path.exists(R + '/var/lib/dpkg/updates') and not os.path.exists(R + '/var/cache') and not os.path.exists(R + '/tmp'),
          '(fixture) /var/lib/dpkg/updates, /var/cache and /tmp are missing, as in the image')
    before = policy.verify_dpkg(R)
    check(len(before) > 5, 'verify_dpkg finds %d problems on the unrepaired image' % len(before))

    # same order as engine.Installer.step_configure
    os.umask(0o022)
    created = policy.ensure_standard_dirs(R)
    dirfix = policy.repair_dir_modes(R)
    policy.fix_special_dirs(R)
    policy.enforce_root_ownership(R)
    policy.apply_baseline_permissions(R)
    dpkgfix = policy.repair_dpkg(R)
    print('  created %d dirs, widened %d, dpkg/apt changes %d' % (len(created), len(dirfix), len(dpkgfix)))

    check(policy.verify_dpkg(R) == [], 'verify_dpkg is clean after the repair: %s' % policy.verify_dpkg(R)[:3])
    need = ['var/lib/dpkg/updates', 'var/cache/apt/archives/partial', 'var/lib/apt/lists/partial', 'var/log/apt',
            'var/cache/debconf', 'etc/dpkg/dpkg.cfg.d', 'etc/apt/sources.list.d', 'usr/share/doc', 'tmp', 'var/tmp']
    check(all(os.path.isdir(os.path.join(R, d)) for d in need), 'every directory dpkg/apt need exists: ' + ' '.join(need[:4]) + ' ...')
    check(S('var/lib/dpkg') == 0o755 and S('var/lib/dpkg/info') == 0o755 and S('var/lib/dpkg/triggers') == 0o755
          and S('var/lib/dpkg/alternatives') == 0o755 and S('etc/alternatives') == 0o755, 'dpkg state directories are 0755 (they were 0700)')
    check(S('var/cache') == 0o755 and S('var/cache/apt') == 0o755, 'the implicitly created /var/cache and /var/cache/apt are 0755')
    check(S('tmp') == 0o1777 and S('var/tmp') == 0o1777 and S('root') == 0o700, '/tmp, /var/tmp are 1777 and /root is 0700')
    check(S('var/lib/dpkg/lock') == 0o640 and S('var/lib/dpkg/status') == 0o644, 'lock files 0640, status database 0644')
    scripts = [n for n in os.listdir(R + '/var/lib/dpkg/info') if n.endswith(policy.MAINTAINER_SCRIPTS)]
    check(len(scripts) > 100 and all(S('var/lib/dpkg/info/' + n) == 0o755 for n in scripts), 'all %d maintainer scripts in info/ are executable' % len(scripts))
    bad = [d for dp, ds, fs in os.walk(R) for d in ds if not os.path.islink(os.path.join(dp, d))
           and (stat.S_IMODE(os.lstat(os.path.join(dp, d)).st_mode) & 0o755) != 0o755
           and os.path.relpath(os.path.join(dp, d), R) not in policy.PRIVATE_DIRS
           and not any(os.path.relpath(os.path.join(dp, d), R).startswith(x + '/') for x in policy.PRIVATE_DIRS)]
    check(not bad, 'no non-private directory is left without 0755 bits (%d found)' % len(bad))
    check(policy.ensure_standard_dirs(R) == [] and policy.repair_dir_modes(R) == [] and policy.repair_dpkg(R) == [],
          'running the whole sequence a second time changes nothing')
finally:
    os.umask(old)
    shutil.rmtree(R, ignore_errors=True)
print('\nRESULT:', 'ALL IMAGE-LISTING TESTS PASSED' if all(res) else 'FAILURES: %d' % res.count(False))
sys.exit(0 if all(res) else 1)
