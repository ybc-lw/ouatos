"""Live-environment install test.

Runs the installer engine INSIDE the built live tree (chroot) - so only tools the live initramfs really
contains are used - against loop-backed virtual disks, once in BIOS and once in UEFI mode.
Results are then verified from the host side with independent checkers.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, '..', 'installer'), HERE]
import fatcheck
import gptcheck

LIVE = '/tmp/live'
MEDIA = '/tmp/fakemedia'
results = []


def check(c, m):
    print(('  PASS  ' if c else '  FAIL  ') + m)
    results.append(bool(c))


INNER = r'''
import os, subprocess, sys
sys.path.insert(0, '/opt/ouatos-installer')
import engine
from engine import sh
mode = sys.argv[1]
img = '/tmp/%s.img' % mode
class LoopScanner:
    def __init__(self): self.loops = {}
    def rescan(self, disk, parts):
        for p in parts:
            self.loops[p.number] = sh(['losetup', '-f', '--show', '-o', str(p.first*disk.lbs), '--sizelimit', str(p.bytes(disk.lbs)), disk.path]).output.strip()
    def path(self, disk, part): return self.loops[part.number]
dev = sh(['losetup', '-f', '--show', img]).output.strip()
open('/tmp/devname-%s' % mode, 'w').write(dev)
disk = engine.Disk('loopx', os.path.getsize(img), 512, 'Virtual disk', '', False, path=dev)
sc = LoopScanner()
env = engine.Env(media='/run/iso', target='/mnt/target', scanner=sc, uefi=(mode == 'uefi'))
print('using mke2fs:', env.mke2fs())
cfg = engine.Config(disk, 'ouatos', 'alice', 'Alice Example', 'Pa55:w0rd!', swap_bytes=(1 << 30) if mode == 'uefi' else 0)
last = [None]
def prog(s, f, m):
    if (s, m) != last[0]:
        last[0] = (s, m); print('   [%d] %s - %s' % (s, engine.STEPS[s][0], m), flush=True)
engine.Installer(cfg, env, prog).run()
import json
json.dump({p.number: [p.kind, p.first, p.last, p.fs_uuid] for p in sc.loops and [] or []}, open('/dev/null', 'w'))
print('INSTALL-OK', mode)
'''


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main():
    for d in ('dev', 'proc', 'sys', 'run/iso', 'mnt/target', 'mnt/user-data/uploads'):
        os.makedirs(os.path.join(LIVE, d), exist_ok=True)
    mounts = []

    def bind(src, dst, *opt):
        subprocess.run(['mount'] + list(opt) + [src, os.path.join(LIVE, dst)], check=True)
        mounts.append(dst)
    try:
        bind('/dev', 'dev', '--rbind')
        bind('proc', 'proc', '-t')
        bind('/sys', 'sys', '--rbind')
        bind(MEDIA, 'run/iso', '--bind')
        bind('/mnt/user-data/uploads', 'mnt/user-data/uploads', '--bind')
        open(LIVE + '/tmp/inner.py', 'w').write(INNER)
        for mode in ('bios', 'uefi'):
            img = '%s/tmp/%s.img' % (LIVE, mode)
            if os.path.exists(img):
                os.unlink(img)
            open(img, 'wb').truncate(6 << 30)
            print('\n================ LIVE-TREE %s install ================' % mode.upper())
            r = subprocess.run(['chroot', LIVE, '/usr/bin/python3', '/tmp/inner.py', mode], capture_output=True, text=True)
            print(r.stdout[-1800:])
            if r.returncode != 0:
                print(r.stderr[-1500:])
            check(r.returncode == 0 and 'INSTALL-OK' in r.stdout, '%s install completed using only the live tree\'s tools' % mode.upper())
            if r.returncode != 0:
                continue
            dev = open('%s/tmp/devname-%s' % (LIVE, mode)).read().strip()
            with open(dev, 'rb') as f:
                parts = gptcheck.read_gpt(f)
            check(len(parts) == (3 if mode == 'uefi' else 2), 'GPT valid with %d partitions: %s' % (len(parts), [p['name'] for p in parts]))
            p2 = parts[1]
            lo = sh(['losetup', '-f', '--show', '-o', str(p2['first'] * 512), '--sizelimit', str((p2['last'] - p2['first'] + 1) * 512), dev]).stdout.strip()
            try:
                e = sh(['e2fsck', '-fn', lo])
                check(e.returncode == 0, 'e2fsck clean: ' + e.stdout.strip().splitlines()[-1])
                os.makedirs('/tmp/verify', exist_ok=True)
                subprocess.run(['mount', '-o', 'ro', lo, '/tmp/verify'], check=True)
                try:
                    rd = lambda p: open('/tmp/verify/' + p).read()
                    check('alice:' in rd('etc/passwd') and rd('etc/hostname') == 'ouatos\n', 'user + hostname present')
                    check('alice' in [l.split(':')[3] for l in rd('etc/group').splitlines() if l.startswith('sudo:')][0].split(','), 'alice in sudo group')
                    check(os.path.exists('/tmp/verify/boot/grub/grub.cfg') and os.path.exists('/tmp/verify/usr/bin/sudo'), 'grub.cfg written, sudo present')
                    cfg = rd('boot/grub/grub.cfg')
                    check('ouatos GNU/Linux' in cfg and 'OUATOS' not in cfg, 'boot menu says "ouatos GNU/Linux"')
                finally:
                    subprocess.run(['umount', '/tmp/verify'])
            finally:
                subprocess.run(['losetup', '-d', lo])
            if mode == 'uefi':
                esp = parts[0]
                with open(dev, 'rb') as f:
                    info = fatcheck.read_fat(f, esp['first'] * 512)
                    files = {k: v[1] for k, v in fatcheck.walk(info).items() if v[1] is not None}
                check('/EFI/BOOT/BOOTX64.EFI' in files and files['/EFI/BOOT/BOOTX64.EFI'][:2] == b'MZ', 'ESP FAT32 holds a valid EFI binary')
            else:
                with open(dev, 'rb') as f:
                    mbr = f.read(512)
                import struct
                check(mbr[510:512] == b'\x55\xaa' and struct.unpack_from('<Q', mbr, 0x5C)[0] == parts[0]['first'], 'GRUB boot sector points at embedded core.img')
            subprocess.run(['losetup', '-d', dev])
    finally:
        for d in reversed(mounts):
            if subprocess.run(['umount', '-R', os.path.join(LIVE, d)]).returncode:
                subprocess.run(['umount', '-R', '-l', os.path.join(LIVE, d)])
        for m in ('bios', 'uefi'):
            p = '%s/tmp/%s.img' % (LIVE, m)
            if os.path.exists(p):
                os.unlink(p)
    print('\nRESULT:', 'LIVE-TREE INSTALL TESTS PASSED' if all(results) else 'FAILURES')
    sys.exit(0 if all(results) else 1)


if __name__ == '__main__':
    main()
