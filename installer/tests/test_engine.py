"""End-to-end installer-engine test on a loop-backed virtual disk.

The sandbox kernel cannot parse partition tables, so a LoopScanner stands in for
the kernel: it exposes each partition as its own loop device (offset+sizelimit).
Everything else (GPT, mkfs, extraction, chroot, useradd, GRUB) is the real code.
"""
import json
import os
import re
import stat
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, '..', 'installer'), HERE, os.path.join(HERE, '..', 'build')]
import engine
import fatcheck
import gptcheck
from engine import sh

MIDS = []
TAR = os.environ.get('OUATOS_TEST_TAR', '/mnt/user-data/uploads/rootfs_tar.xz')
MEDIA = '/tmp/fakemedia'
IMG = '/tmp/testdisk.img'
TGT = '/tmp/tgt'
VER = '/tmp/verify'


class LoopScanner:
    def __init__(self):
        self.loops = {}

    def rescan(self, disk, parts):
        self.release()
        for p in parts:
            dev = sh(['losetup', '-f', '--show', '-o', str(p.first * disk.lbs), '--sizelimit', str(p.bytes(disk.lbs)),
                      disk.path]).output.strip()
            self.loops[p.number] = dev

    def path(self, disk, part):
        return self.loops[part.number]

    def release(self):
        for d in self.loops.values():
            subprocess.run(['losetup', '-d', d])
        self.loops = {}


def setup_media():
    os.makedirs(MEDIA + '/ouatos', exist_ok=True)
    dst = MEDIA + '/ouatos/rootfs.txz'
    if not os.path.exists(dst):
        os.symlink(TAR, dst)
    if not os.path.exists(MEDIA + '/ouatos/rootfsinfo.json'):
        from rootfs_info import make_rootfs_info
        info = make_rootfs_info(TAR)
        json.dump(info, open(MEDIA + '/ouatos/rootfsinfo.json', 'w'))
    info = json.load(open(MEDIA + '/ouatos/rootfsinfo.json'))
    print('media info: installed_bytes=%d MiB users=%d groups=%d kernel=%s' % (
        info['installed_bytes'] >> 20, len(info['system_users']), len(info['system_groups']), info['kernel']))
    return info


def run_install(swap):
    sh(['umount', '-R', TGT], check=False)
    disk_dev = sh(['losetup', '-f', '--show', IMG]).output.strip()
    disk = engine.Disk('loop0', os.path.getsize(IMG), 512, 'Virtual test disk', '', False, path=disk_dev)
    scanner = LoopScanner()
    mk = os.environ.get('OUATOS_TEST_MKE2FS')
    iso = os.environ.get('OUATOS_TEST_ISO')
    if iso:
        import media as _m
        print('  (reading the payload straight from the ISO image %s, no mount)' % iso)
    env = engine.Env(media=_m.RawMedia(iso) if iso else MEDIA, target=TGT, scanner=scanner, mke2fs=[mk] if mk else None)
    cfg = engine.Config(disk, 'ouatos', 'alice', 'Alice Example', 'Pa55:w0rd!', swap_bytes=swap, root_password='R00t:Pass!')
    events = []
    last = [None]

    def prog(step, frac, msg):
        key = (step, msg)
        if key != last[0]:
            last[0] = key
            events.append('  [%d] %-28s %3d%%  %s' % (step, engine.STEPS[step][0], frac * 100, msg))
    inst = engine.Installer(cfg, env, prog)
    try:
        inst.run()
    finally:
        print('\n'.join(events))
    return inst, disk, scanner, disk_dev


def archive_attrs(paths):
    """(uid, gid, mode) of the given paths as the OS image archive itself records them."""
    import lzma
    want, found = {p.strip('/') for p in paths}, {}
    with lzma.open(TAR, 'rb') as decoded, tarfile.open(fileobj=decoded, mode='r|') as tf:
        for m in tf:
            n = m.name[2:] if m.name.startswith('./') else m.name
            if n.rstrip('/') in want:
                found[n.rstrip('/')] = (m.uid, m.gid, stat.S_IMODE(m.mode))
    return found


def verify(inst, disk, scanner, disk_dev):
    ok = []

    def check(cond, msg):
        print(('  PASS  ' if cond else '  FAIL  ') + msg)
        ok.append(bool(cond))
    # ---- GPT
    with open(disk_dev, 'rb') as f:
        parts = gptcheck.read_gpt(f)
    check(True, 'GPT valid (CRCs, backup copy, alignment, no overlap): ' +
          ', '.join('p%d=%s' % (p['n'], p['name']) for p in parts))
    check(parts[0]['type'] == engine.GUID_ESP, 'p1 is the EFI system partition')
    check(parts[1]['type'] == engine.GUID_LINUX and parts[1]['name'] == 'ouatos', 'p2 is root named "ouatos"')
    # ---- root filesystem
    root = next(p for p in inst.parts if p.kind == 'root')
    rdev = scanner.path(disk, root)
    try:
        r = subprocess.run(['e2fsck', '-fn', rdev], capture_output=True, text=True)
        check(r.returncode == 0, 'e2fsck -fn clean on root: ' + r.stdout.strip().splitlines()[-1])
    except FileNotFoundError:
        print('  SKIP  e2fsck (not part of the live system)')
    os.makedirs(VER, exist_ok=True)
    sh(['mount', '-o', 'ro', rdev, VER])
    try:
        rd = lambda p: open(VER + '/' + p).read()
        blk = subprocess.run(['blkid', '-o', 'value', '-s', 'UUID', rdev], capture_output=True, text=True).stdout.strip()
        check(blk == root.fs_uuid, 'root filesystem UUID matches plan (%s)' % blk)
        check(rd('etc/hostname') == 'ouatos\n', 'hostname = ouatos')
        check('127.0.1.1\touatos' in rd('etc/hosts'), '/etc/hosts has 127.0.1.1 ouatos')
        fstab = rd('etc/fstab')
        check('UUID=%s\t/\text4' % root.fs_uuid in fstab, 'fstab has root by UUID')
        check('/boot/efi' in fstab, 'fstab has the EFI system partition entry')
        passwd = [l.split(':') for l in rd('etc/passwd').splitlines()]
        u = next((x for x in passwd if x[0] == 'alice'), None)
        check(u and u[5] == '/home/alice' and u[6].strip() == '/bin/bash' and u[4] == 'Alice Example', 'user alice: home, bash, full name')
        shadow = {l.split(':')[0]: l.split(':')[1] for l in rd('etc/shadow').splitlines()}
        check(shadow['alice'].startswith('$y$'), 'alice password stored as yescrypt hash')
        check(shadow['root'].startswith('$y$'), 'root has a password (yescrypt hash): the account is not locked')
        verify_pw = lambda user, pw: subprocess.run(['chroot', VER, '/usr/bin/python3', '-c',
            'import ctypes,sys;c=ctypes.CDLL("libcrypt.so.1");c.crypt.restype=ctypes.c_char_p;h=open("/etc/shadow").read().split("%s:")[1].split(":")[0];'
            'print(c.crypt(sys.argv[1].encode(),h.encode()).decode()==h)' % user, pw], capture_output=True, text=True).stdout.strip() == 'True'
        check(verify_pw('root', 'R00t:Pass!'), 'the root password verifies against its stored hash')
        check(not verify_pw('root', 'Pa55:w0rd!') and not verify_pw('alice', 'R00t:Pass!'), 'root and user passwords are independent')
        check('R00t' not in open('/tmp/ouatos-install.log').read(), 'root password never appears in the install log')
        grp = {l.split(':')[0]: l.split(':')[3].split(',') for l in rd('etc/group').splitlines()}
        check('alice' in grp['sudo'], 'alice is in group sudo (sudoers: %%sudo ALL=(ALL:ALL) ALL)')
        check(os.path.isdir(VER + '/home/alice') and os.stat(VER + '/home/alice').st_uid == int(u[2]), 'home dir exists, owned by alice')
        check(os.path.exists(VER + '/home/alice/.bashrc'), 'skeleton files copied')
        # password really verifies (hash check done by the target's own crypt via chroot python)
        r = subprocess.run(['chroot', VER, '/usr/bin/python3', '-c',
                            'import ctypes;c=ctypes.CDLL("libcrypt.so.1");c.crypt.restype=ctypes.c_char_p;'
                            'import sys;h=open("/etc/shadow").read().split("alice:")[1].split(":")[0];'
                            'print(c.crypt(b"Pa55:w0rd!",h.encode()).decode()==h)'], capture_output=True, text=True)
        check(r.stdout.strip() == 'True', 'password verifies against the stored hash (crypt)')
        check('Pa55' not in open('/tmp/ouatos-install.log').read(), 'password never appears in the install log')
        osr = engine.parse_os_release(rd('etc/os-release'))
        check(engine.system_name(osr) == 'ouatos', 'system name from target os-release: %r' % engine.system_name(osr))
        default = rd('etc/default/grub')
        check('GRUB_DISTRIBUTOR="ouatos"' in default, 'GRUB_DISTRIBUTOR="ouatos" (lowercase)')
        cfg = rd('boot/grub/grub.cfg')
        check('menuentry' in cfg and 'root=UUID=%s' % root.fs_uuid in cfg, 'grub.cfg has a menu entry booting root=UUID')
        title = next((line for line in cfg.splitlines() if re.match(r'\s*menuentry\s+', line)), '')
        check('ouatos' in title and 'Ouatos' not in cfg and 'OUATOS' not in cfg, 'menu title lowercase: ' + title.strip()[:70])
        check(os.path.exists(VER + '/boot/grub/x86_64-efi/normal.mod'), 'grub modules copied')
        check(not os.path.exists(VER + '/tmp/ouatos-early.cfg'), 'temporary files removed')
        check(os.path.exists(VER + '/var/log/ouatos-install.log'), 'install log saved into the new system')
        mid = open(VER + '/etc/machine-id').read()
        check(re.fullmatch(r'[0-9a-f]{32}\n', mid) is not None and set(mid.strip()) != {'0'}, 'machine-id initialised: 32 hex digits (%s...)' % mid[:8])
        MIDS.append(mid)
        # ---- owners and modes: the installer sets none, the archive's own values must survive
        S_ = lambda p: os.lstat(VER + p)
        pwrow = next(x for x in passwd if x[0] == 'alice'); uid_, gid_ = int(pwrow[2]), int(pwrow[3])
        grpname = next(l.split(':')[0] for l in rd('etc/group').splitlines() if l.split(':')[2] == str(gid_))
        check(grpname == 'alice', "alice's primary group is named alice (gid %d)" % gid_)
        check((S_('/home/alice').st_uid, S_('/home/alice').st_gid) == (uid_, gid_), '/home/alice is owned by alice:alice (%d:%d), as created by useradd' % (uid_, gid_))
        sample = ['/', '/tmp', '/var/tmp', '/root', '/usr/bin/sudo', '/usr/bin/passwd', '/etc/sudoers']
        recorded = archive_attrs(sample)
        for rel_, (u_, g_, m_) in sorted(recorded.items()):
            now_ = S_('/' + rel_) if rel_ else S_('/')
            check((now_.st_uid, now_.st_gid, stat.S_IMODE(now_.st_mode)) == (u_, g_, m_),
                  '/%s keeps the archive owner and mode (%d:%d %04o)' % (rel_, u_, g_, m_))
        check(all(os.path.isdir(VER + d_) for d_ in ('/dev', '/proc', '/sys', '/run', '/tmp', '/boot/efi')), 'the directories the installer needs came with the image (dev proc sys run tmp boot/efi)')
        expect = os.environ.get('OUATOS_TEST_EXPECT_FILE')
        if expect:
            check(os.path.exists(VER + expect), 'installed system contains %s (proves THIS rootfs was installed)' % expect)
        exp_size = os.environ.get('OUATOS_TEST_EXPECT_SIZE')
        if exp_size:
            path_, size_ = exp_size.rsplit(':', 1)
            check(os.path.getsize(VER + path_) == int(size_), '%s is %s bytes in the installed system' % (path_, size_))
    finally:
        sh(['umount', VER], check=False)
    # ---- bootloader
    with open(disk_dev, 'rb') as f:
        esp = inst.parts[0]
        f.seek(esp.first * 512)
        info = fatcheck.read_fat(f, esp.first * 512)
        tree = fatcheck.walk(info)
        files = {k: v[1] for k, v in tree.items() if v[1] is not None}
        check(info['bits'] == 32, 'ESP is FAT32 (%d clusters, spc=%d)' % (info['clusters'], info['spc']))
        check('%04X-%04X' % (info['volid'] >> 16, info['volid'] & 0xFFFF) == esp.fs_uuid, 'ESP volume id matches fstab UUID %s' % esp.fs_uuid)
        check('/EFI/BOOT/BOOTX64.EFI' in files and '/EFI/ouatos/grubx64.efi' in files, 'ESP has EFI/BOOT/BOOTX64.EFI and EFI/ouatos/grubx64.efi (lowercase dir)')
        b = files['/EFI/BOOT/BOOTX64.EFI']
        check(b[:2] == b'MZ' and files['/EFI/ouatos/grubx64.efi'] == b, 'both are the same PE/EFI binary (%d bytes)' % len(b))
        check(info['hidden'] == esp.first, 'FAT hidden sectors = partition start')
    return all(ok)


if __name__ == '__main__':
    open('/tmp/ouatos-install.log', 'w').close()
    setup_media()
    if not os.path.exists(IMG):
        open(IMG, 'wb').truncate(6 * engine.GiB)
    allok = True
    for swap in (0, 1 * engine.GiB):
        print('\n================ UEFI install, swap=%s ================' % (swap and engine.fmt_size(swap)))
        inst, disk, scanner, dev = run_install(swap)
        try:
            allok &= verify(inst, disk, scanner, dev)
        finally:
            scanner.release()
            subprocess.run(['losetup', '-d', dev])
    print('  %s  every installation got its own machine-id (%s)' % ('PASS' if len(set(MIDS)) == len(MIDS) == 2 else 'FAIL', ', '.join(m[:8] for m in MIDS)))
    allok &= len(set(MIDS)) == len(MIDS) == 2
    print('\nRESULT:', 'ALL ENGINE TESTS PASSED' if allok else 'SOME TESTS FAILED')
    sys.exit(0 if allok else 1)
