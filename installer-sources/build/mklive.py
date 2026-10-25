"""Assembles the live installer environment (an initramfs) from the ouatos rootfs itself.

The live system is a trimmed copy of the target's own userland (python, util-linux, kmod, udev ...),
the kernel modules the shipped initrd already considers necessary for booting on arbitrary hardware,
the installer, and a private copy of mke2fs (the OS ships no e2fsprogs).
"""
import gzip
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import zlib

BINARIES = '''dash bash sh cat chmod chown chroot cp date dd df dirname basename echo env false head id ln ls mkdir mknod
mktemp mv readlink rm rmdir sleep sort stat sync tail tee touch tr true uname uniq wc seq sed grep find xargs tar gzip
mount umount lsblk blkid findmnt losetup mkswap swapon swapoff wipefs blockdev partx dmesg setsid kmod modprobe insmod
rmmod lsmod depmod modinfo udevadm ldd awk mawk fsck lscpu cmp diff test [ expr sha256sum md5sum tty stty reset
printf realpath du free'''.split()

PY_IGNORE = shutil.ignore_patterns('test', 'tests', 'idlelib', 'tkinter', 'turtledemo', 'ensurepip', 'lib2to3',
                                   'pydoc_data', 'turtle.py', 'config-*', '*.pyc')

EXTRA_MODULES = ['evdev', 'psmouse', 'virtio_input', 'hid-generic', 'usbhid', 'isofs', 'loop', 'sr_mod', 'cdrom',
                 'sd_mod', 'usb-storage', 'uas', 'ext4', 'xhci-pci', 'ehci-pci', 'ohci-pci', 'uhci-hcd', 'nvme',
                 'virtio_blk', 'virtio_scsi', 'ahci', 'ata_piix', 'mmc_block', 'sdhci-pci', 'hid_multitouch']


def log(*a):
    print('  ', *a, flush=True)


# ---------------------------------------------------------------------------------- file helpers
def _resolve_parent(rootfs, rel):
    """Canonicalise the directory part of rel (so lib/... and usr/lib/... map to the same place)."""
    d, b = os.path.split(rel)
    real = os.path.realpath(os.path.join(rootfs, d))
    return os.path.join(os.path.relpath(real, os.path.realpath(rootfs)), b)


def install_path(rootfs, live, rel, seen=None):
    """Copy one path (following symlink chains, preserving them) from rootfs into live."""
    seen = seen if seen is not None else set()
    rel = _resolve_parent(rootfs, rel.lstrip('/'))
    if rel in seen:
        return rel
    seen.add(rel)
    src, dst = os.path.join(rootfs, rel), os.path.join(live, rel)
    if not os.path.lexists(src):
        raise FileNotFoundError(rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.islink(src):
        tgt = os.readlink(src)
        if not os.path.lexists(dst):
            os.symlink(tgt, dst)
        trel = tgt.lstrip('/') if tgt.startswith('/') else os.path.normpath(os.path.join(os.path.dirname(rel), tgt))
        install_path(rootfs, live, trel, seen)
    elif os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
    elif not os.path.exists(dst):
        shutil.copy2(src, dst)
    return rel


def is_elf(path):
    try:
        with open(path, 'rb') as f:
            return f.read(4) == b'\x7fELF'
    except OSError:
        return False


def ldd(rootfs, rel):
    """Shared objects needed by an ELF file, as rootfs-relative paths (uses the rootfs's own ldd)."""
    r = subprocess.run(['chroot', rootfs, '/usr/bin/ldd', '/' + rel], capture_output=True, text=True)
    deps = []
    for line in r.stdout.splitlines():
        m = re.search(r'=>\s+(/\S+)', line) or re.match(r'\s*(/\S+)\s+\(0x', line)
        if m:
            deps.append(m.group(1).lstrip('/'))
        elif 'not found' in line:
            raise RuntimeError('%s: unresolved dependency: %s' % (rel, line.strip()))
    return deps


def install_elf(rootfs, live, rel, seen):
    real = install_path(rootfs, live, rel, seen)
    full = os.path.join(rootfs, real)
    if is_elf(full):
        for d in ldd(rootfs, real):
            install_path(rootfs, live, d, seen)


def find_bin(rootfs, name):
    for d in ('usr/bin', 'usr/sbin'):
        if os.path.lexists(os.path.join(rootfs, d, name)):
            return d + '/' + name
    return None


# ---------------------------------------------------------------------------------- modules
def initrd_modules(rootfs, kver):
    """Module paths (relative to the module dir) that the shipped initrd carries."""
    data = open(os.path.join(rootfs, 'boot/initrd.img-' + kver), 'rb').read()
    off, mods = 0, []
    while True:
        h = data[off:off + 110]
        if h[:6] not in (b'070701', b'070702'):
            break
        f = [int(h[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
        fs, ns = f[6], f[11]
        name = data[off + 110:off + 110 + ns - 1].decode('latin1')
        off = ((off + 110 + ns + 3) & ~3)
        off = (off + fs + 3) & ~3
        if name == 'TRAILER!!!':
            break
        m = re.match(r'usr/lib/modules/[^/]+/(kernel/.*\.ko(\.\w+)?)$', name)
        if m:
            mods.append(m.group(1))
    return mods


def module_closure(rootfs, kver, wanted_paths, extra_names):
    base = os.path.join(rootfs, 'usr/lib/modules', kver)
    dep = {}
    for line in open(os.path.join(base, 'modules.dep')):
        k, _, v = line.strip().partition(':')
        dep[k] = v.split()
    byname = {}
    for k in dep:
        byname.setdefault(re.sub(r'\.ko(\.\w+)?$', '', os.path.basename(k)).replace('-', '_'), k)
    todo = list(wanted_paths)
    for n in extra_names:
        k = byname.get(n.replace('-', '_'))
        if k:
            todo.append(k)
        else:
            log('note: module %s not present in this kernel' % n)
    out = set()
    while todo:
        k = todo.pop()
        if k in out or k not in dep:
            continue
        out.add(k)
        todo += dep[k]
    return sorted(out)


# ---------------------------------------------------------------------------------- cpio
def write_cpio_gz(root, out_path, nodes=(), level=6):
    """newc cpio of `root` (+ device nodes), gzip-compressed, as the kernel expects for an initramfs."""
    comp = zlib.compressobj(level, zlib.DEFLATED, 31)
    ino = [0]
    with open(out_path, 'wb') as out:
        def emit(chunk):
            out.write(comp.compress(chunk))

        def entry(name, mode, data=b'', rdev=(0, 0), mtime=0, nlink=1):
            ino[0] += 1
            nm = name.encode() + b'\0'
            hdr = b'070701' + b''.join(b'%08X' % v for v in (
                ino[0], mode, 0, 0, nlink, mtime, len(data), 0, 0, rdev[0], rdev[1], len(nm), 0))
            emit(hdr + nm + b'\0' * ((-(110 + len(nm))) % 4))
            return len(data)

        def body(data):
            emit(data + b'\0' * ((-len(data)) % 4))

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            rel = os.path.relpath(dirpath, root)
            if rel != '.':
                st = os.lstat(dirpath)
                entry(rel, st.st_mode, mtime=int(st.st_mtime), nlink=2)
            for fn in sorted(filenames) + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
                p = os.path.join(dirpath, fn)
                name = os.path.normpath(os.path.join(rel, fn))
                st = os.lstat(p)
                if stat.S_ISLNK(st.st_mode):
                    t = os.readlink(p).encode()
                    entry(name, st.st_mode, t, mtime=int(st.st_mtime))
                    body(t)
                elif stat.S_ISREG(st.st_mode):
                    data = open(p, 'rb').read()
                    entry(name, st.st_mode, data, mtime=int(st.st_mtime))
                    body(data)
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name, mode, major, minor in nodes:
            entry(name, mode, rdev=(major, minor))
        entry('TRAILER!!!', 0)
        out.write(comp.flush())
    return os.path.getsize(out_path)


# ---------------------------------------------------------------------------------- main assembly
INIT = r'''#!/bin/sh
# ouatos live installer: PID 1 of the initramfs
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root TERM=linux LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1
mount -t proc -o nosuid,nodev,noexec proc /proc
mount -t sysfs -o nosuid,nodev,noexec sysfs /sys
mount -t devtmpfs -o mode=0755,nosuid devtmpfs /dev
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts
mount -t tmpfs -o mode=1777 tmpfs /dev/shm
mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run
mount -t tmpfs -o mode=1777 tmpfs /tmp
[ -d /sys/firmware/efi ] && mount -t efivarfs efivarfs /sys/firmware/efi/efivars 2>/dev/null
echo "3 3 1 3" > /proc/sys/kernel/printk 2>/dev/null
mkdir -p /run/iso /mnt/target /run/udev
CMDLINE=$(cat /proc/cmdline)

emergency() {
    echo; echo "*** $1"
    echo "*** Dropping to a shell. 'exit' retries the installer; 'reboot' restarts the computer."
    setsid -c bash </dev/tty1 >/dev/tty1 2>&1 || bash
}

# --- drivers -------------------------------------------------------------------------------
# Three independent routes, so one failing cannot leave us without a disk or CD-ROM:
#  1. udev coldplug (also handles hotplug later)
#  2. a driver for every device alias that sysfs shows (works without udev)
#  3. a fixed list of storage / USB / input drivers, loaded whether or not hardware is announced yet
MEDIA=/opt/ouatos-installer/media.py
BOOTLOG=/run/boot.log
DRIVERS="scsi_mod libata ahci ata_piix ata_generic pata_acpi sr_mod sd_mod cdrom sg virtio_pci virtio_blk virtio_scsi nvme usb_storage uas xhci_pci ehci_pci ohci_pci uhci_hcd usbhid hid_generic psmouse virtio_input mptspi mptsas vmw_pvscsi hv_storvsc sdhci_pci mmc_block isofs evdev"
load_modules() {
    find /sys/devices -name modalias -type f 2>/dev/null | xargs cat 2>/dev/null | sort -u | xargs modprobe -b -a -q 2>/dev/null
}
load_drivers() {
    for m in $DRIVERS; do modprobe -q $m 2>/dev/null; done
}
/usr/lib/systemd/systemd-udevd --daemon --resolve-names=never 2>>$BOOTLOG
udevadm trigger --type=subsystems --action=add 2>>$BOOTLOG
udevadm trigger --type=devices --action=add 2>>$BOOTLOG
udevadm settle --timeout=30 2>>$BOOTLOG
load_modules
load_drivers

case "$CMDLINE" in *ouatos.shell=1*) emergency "ouatos.shell=1 was given";; esac

# The medium is found by scanning every block device for an ISO 9660 volume labelled ouatos_install and
# read straight from the device by the installer: no mount, so no dependence on the kernel's isofs driver.
find_media() {
    for dev in $(python3 $MEDIA find 2>>$BOOTLOG); do
        if python3 $MEDIA check "$dev" >>$BOOTLOG 2>&1; then
            echo "$dev" > /run/media.dev
            return 0
        fi
    done
    return 1
}

diagnose() {
    echo "--- what the installer can see ---"; python3 $MEDIA list
    echo "--- loaded kernel modules: $(wc -l < /proc/modules) ---"; lsmod | head -30
    echo "--- boot log (tail) ---"; tail -12 $BOOTLOG
    echo "--- kernel messages (tail) ---"; dmesg | tail -25
}

tries=0
until find_media; do
    tries=$((tries + 1))
    [ $tries -ge 60 ] && break
    echo "Waiting for the installation medium ($tries)..." >/dev/tty1 2>/dev/null
    [ $((tries % 10)) -eq 0 ] && { python3 $MEDIA list >/dev/tty1 2>&1; load_drivers; }
    udevadm trigger --action=add 2>/dev/null; udevadm settle --timeout=5 2>/dev/null
    load_modules
    sleep 1
done
if [ ! -s /run/media.dev ]; then
    diagnose >/dev/tty1 2>&1
    emergency "The installation medium was not found (looked for an ISO 9660 volume labelled ouatos_install)."
fi

while :; do
    python3 /opt/ouatos-installer/installer.py
    emergency "The installer stopped (exit status $?)."
done
'''

SBIN_REBOOT = '#!/bin/sh\nsync\necho 1 > /proc/sys/kernel/sysrq\necho %s > /proc/sysrq-trigger\n'


def build_live(rootfs, live, kver, installer_dir, hostmke2fs='/usr/sbin/mke2fs'):
    if os.path.exists(live):
        shutil.rmtree(live)
    os.makedirs(live)
    for link, tgt in (('bin', 'usr/bin'), ('sbin', 'usr/sbin'), ('lib', 'usr/lib'), ('lib64', 'usr/lib64')):
        os.symlink(tgt, os.path.join(live, link))
    for d in ('proc', 'sys', 'dev', 'run', 'tmp', 'mnt', 'root', 'etc', 'var/tmp', 'opt', 'usr/local/sbin', 'usr/local/bin'):
        os.makedirs(os.path.join(live, d), exist_ok=True)
    os.chmod(os.path.join(live, 'tmp'), 0o1777)
    seen = set()

    log('binaries + libraries')
    missing = []
    for b in BINARIES:
        rel = find_bin(rootfs, b)
        if rel:
            install_elf(rootfs, live, rel, seen)
        else:
            missing.append(b)
    log('(not in rootfs, skipped: %s)' % ' '.join(missing))
    for rel in ('usr/lib/systemd/systemd-udevd',):
        install_elf(rootfs, live, rel, seen)
    # udev dlopen()s libkmod to load drivers; ldd cannot see dlopen(), so ship it explicitly
    for rel in ('usr/lib/x86_64-linux-gnu/libkmod.so.2',):
        install_elf(rootfs, live, rel, seen)

    pyver = os.path.basename(os.path.realpath(os.path.join(rootfs, 'usr/bin/python3')))     # e.g. python3.13
    log('python3 + standard library (%s)' % pyver)
    install_elf(rootfs, live, 'usr/bin/' + pyver, seen)
    install_path(rootfs, live, 'usr/bin/python3', seen)
    shutil.copytree(os.path.join(rootfs, 'usr/lib', pyver), os.path.join(live, 'usr/lib', pyver),
                    symlinks=True, ignore=PY_IGNORE, dirs_exist_ok=True)
    dyn = os.path.join(live, 'usr/lib', pyver, 'lib-dynload')
    for so in sorted(os.listdir(dyn)):
        for d in ldd(rootfs, 'usr/lib/%s/lib-dynload/%s' % (pyver, so)):
            install_path(rootfs, live, d, seen)

    log('udev + modprobe configuration')
    for d in ('usr/lib/udev', 'usr/lib/modprobe.d'):
        shutil.copytree(os.path.join(rootfs, d), os.path.join(live, d), symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('hwdb.bin', 'hwdb.d'))
    for root_, _, files in os.walk(os.path.join(live, 'usr/lib/udev')):
        for f in files:
            p = os.path.join(root_, f)
            if is_elf(p):
                for d in ldd(rootfs, os.path.relpath(p, live)):
                    install_path(rootfs, live, d, seen)
    for f in ('etc/passwd', 'etc/group', 'etc/nsswitch.conf', 'usr/lib/os-release'):
        if os.path.lexists(os.path.join(rootfs, f)):
            install_path(rootfs, live, f, seen)
    os.symlink('../usr/lib/os-release', os.path.join(live, 'etc/os-release'))
    os.symlink('/proc/self/mounts', os.path.join(live, 'etc/mtab'))
    open(os.path.join(live, 'etc/fstab'), 'w').write('')

    log('kernel modules')
    want = initrd_modules(rootfs, kver)
    chosen = module_closure(rootfs, kver, want, EXTRA_MODULES)
    mdir = 'usr/lib/modules/' + kver
    for k in chosen:
        dst = os.path.join(live, mdir, k)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(rootfs, mdir, k), dst)
    for f in ('modules.builtin', 'modules.builtin.modinfo', 'modules.order'):
        shutil.copy2(os.path.join(rootfs, mdir, f), os.path.join(live, mdir, f))
    mnt = os.path.join(rootfs, 'mnt/live')
    os.makedirs(mnt, exist_ok=True)
    subprocess.run(['mount', '--bind', live, mnt], check=True)
    try:
        subprocess.run(['chroot', rootfs, '/usr/sbin/depmod', '-b', '/mnt/live', kver], check=True)
    finally:
        subprocess.run(['umount', mnt], check=True)
        os.rmdir(mnt)
    msize = sum(os.path.getsize(os.path.join(live, mdir, k)) for k in chosen)
    log('%d modules (%.1f MiB)' % (len(chosen), msize / 2**20))

    log('mke2fs (bundled from the build host: the OS image ships no e2fsprogs)')
    hl = os.path.join(live, 'opt/hostlibs')
    os.makedirs(hl)
    shutil.copy2(hostmke2fs, os.path.join(hl, 'mke2fs'))
    out = subprocess.run(['ldd', hostmke2fs], capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.search(r'=>\s+(/\S+)', line) or re.match(r'\s*(/\S+)\s+\(0x', line)
        if m:
            shutil.copy2(os.path.realpath(m.group(1)), os.path.join(hl, os.path.basename(m.group(1))))
    shutil.copy2('/etc/mke2fs.conf', os.path.join(hl, 'mke2fs.conf'))
    wrapper = os.path.join(live, 'usr/local/sbin/mkfs.ext4')
    open(wrapper, 'w').write('#!/bin/sh\nexport MKE2FS_CONFIG=/opt/hostlibs/mke2fs.conf\n'
                             'exec /opt/hostlibs/ld-linux-x86-64.so.2 --library-path /opt/hostlibs /opt/hostlibs/mke2fs "$@"\n')
    os.chmod(wrapper, 0o755)

    log('installer')
    idst = os.path.join(live, 'opt/ouatos-installer')
    os.makedirs(idst)
    for f in ('engine.py', 'fatfs.py', 'fbgui.py', 'fontspec.py', 'installer.py', 'media.py', 'policy.py', 'font.bin'):
        shutil.copy2(os.path.join(installer_dir, f), idst)
    log('pre-compiling Python bytecode')
    mnt = os.path.join(rootfs, 'mnt/live')
    os.makedirs(mnt, exist_ok=True)
    subprocess.run(['mount', '--bind', live, mnt], check=True)
    try:
        for d, shown in (('/mnt/live/usr/lib/' + pyver, '/usr/lib/' + pyver),
                         ('/mnt/live/opt/ouatos-installer', '/opt/ouatos-installer')):
            subprocess.run(['chroot', rootfs, '/usr/bin/python3', '-m', 'compileall', '-q', '-d', shown, d], check=True)
    finally:
        subprocess.run(['umount', mnt], check=True)
        os.rmdir(mnt)
    for name, arg in (('reboot', 'b'), ('poweroff', 'o')):
        p = os.path.join(live, 'usr/local/sbin', name)
        open(p, 'w').write(SBIN_REBOOT % arg)
        os.chmod(p, 0o755)
    open(os.path.join(live, 'init'), 'w').write(INIT)
    os.chmod(os.path.join(live, 'init'), 0o755)
    total = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(live) for f in fs if not os.path.islink(os.path.join(r, f)))
    log('live tree: %.1f MiB' % (total / 2**20))
    return chosen
