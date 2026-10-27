"""ouatos installer engine (no UI code in here).

Everything the installer does to a machine lives in this module so it can be
driven by the graphical front-end, the text fallback, or the test-suite.
"""
import ctypes
import errno
import fcntl
import glob
import json
import lzma
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
import zlib

import fatfs
import media as _media
import policy

BRAND = 'ouatos'                      # the system name is ALWAYS written in lowercase
ISO_LABEL = 'ouatos_install'
LOG_FILE = '/tmp/ouatos-install.log'
MiB, GiB = 1 << 20, 1 << 30

# ---------------------------------------------------------------------------
# logging / process helpers
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()


def log(msg):
    line = '[%s] %s\n' % (time.strftime('%H:%M:%S'), msg)
    with _log_lock:
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(line)
        except OSError:
            pass


class InstallError(Exception):
    """An error whose `args[0]` is safe to show to the user."""

    def __init__(self, msg, detail=''):
        super().__init__(msg)
        self.detail = detail


def sh(cmd, input=None, check=True, env=None, what=None, timeout=None):
    """Run a command, log it (never logs `input`, which may hold a password)."""
    log('$ ' + ' '.join(str(c) for c in cmd))
    e = dict(os.environ)
    e.setdefault('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=e, timeout=timeout)
    except FileNotFoundError:
        raise InstallError('%s failed: program not found (%s)' % (what or cmd[0], cmd[0]))
    out = p.stdout.decode('utf-8', 'replace')
    if out.strip():
        log(out.rstrip())
    if check and p.returncode != 0:
        raise InstallError('%s failed (exit status %d)' % (what or cmd[0], p.returncode), out[-2000:])
    p.output = out
    return p


# ---------------------------------------------------------------------------
# branding: everything shown to the user is derived from the OS's own os-release
# ---------------------------------------------------------------------------
def parse_os_release(text):
    d = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
            v = v[1:-1]
        d[k.strip()] = re.sub(r'\\(["\\$`])', r'\1', v)
    return d


def enforce_brand(s):
    """'ouatos' is always lowercase, whatever case the source text used."""
    return re.sub('ouatos', BRAND, s, flags=re.I)


def system_name(osr):
    """Display name, e.g. 'ouatos GNU/Linux' (PRETTY_NAME, falling back to NAME / ID)."""
    return enforce_brand(osr.get('PRETTY_NAME') or osr.get('NAME') or osr.get('ID') or BRAND)


def brand_name(osr):
    """Short name, e.g. 'ouatos' (NAME without a trailing 'GNU/Linux')."""
    n = osr.get('NAME') or osr.get('ID') or BRAND
    n = re.sub(r'\s*GNU/Linux\s*$', '', n, flags=re.I).strip() or BRAND
    return enforce_brand(n)


def default_hostname(osr):
    h = re.sub(r'[^a-z0-9-]', '-', (osr.get('ID') or BRAND).lower()).strip('-')
    return h or BRAND


def as_media(m):
    return _media.MountedMedia(m) if isinstance(m, str) else m


def load_media_info(media):
    """Reads os-release + facts about the OS image that were recorded on the ISO."""
    media = as_media(media)
    info = {}
    try:
        raw = media.read_small(_media.INFO_NAMES)
        if raw:
            info = json.loads(raw.decode())
    except (OSError, ValueError, _media.MediaError):
        pass
    osr = info.get('os_release')
    if not osr:
        try:
            raw = media.read_small(_media.OSREL_NAMES)
            osr = parse_os_release(raw.decode()) if raw else {}
        except (OSError, _media.MediaError):
            osr = {}
    info['os_release'] = osr
    info.setdefault('installed_bytes', 1 * GiB)
    info.setdefault('system_users', [])
    info.setdefault('system_groups', [])
    return info


# ---------------------------------------------------------------------------
# validation (shared by the GUI and the text front-end)
# ---------------------------------------------------------------------------
def check_hostname(h):
    if not h:
        return 'Please enter a computer name.'
    if len(h) > 63:
        return 'The name is too long (63 characters at most).'
    if not re.fullmatch(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?', h):
        return 'Use only lowercase letters, digits and hyphens (not at the start or end).'
    return ''


def check_username(u, reserved=(), reserved_groups=()):
    if not u:
        return 'Please enter a username.'
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', u):
        return 'Start with a lowercase letter; then letters, digits, "-" or "_" (32 characters at most).'
    if u in set(reserved) | set(reserved_groups) or u in ('root', 'nobody', 'daemon', 'sudo'):
        return 'That name is reserved by the system. Please choose another.'
    return ''


def check_password(pw, pw2=None):
    if not pw:
        return 'Please enter a password.'
    if '\n' in pw or '\r' in pw or '\0' in pw:
        return 'The password contains an invalid character.'
    if len(pw) > 127:
        return 'The password is too long.'
    if pw2 is not None and pw != pw2:
        return 'The two passwords do not match.'
    return ''


def clean_fullname(s):
    return re.sub(r'[:,=\\\x00-\x1f\x7f]', '', s).strip()[:60]


# ---------------------------------------------------------------------------
# disks
# ---------------------------------------------------------------------------
class Disk:
    def __init__(self, name, size, lbs, model, transport, removable, path=None, reason=''):
        self.name, self.size, self.lbs = name, size, lbs
        self.model, self.transport, self.removable = model, transport, removable
        self.path = path or '/dev/' + name
        self.identity = _rd('/sys/class/block/' + name + '/diskseq')
        self.reason = reason            # non-empty => cannot be selected

    @property
    def usable(self):
        return not self.reason

    def size_text(self):
        return fmt_size(self.size)

    def title(self):
        return self.model or ('Disk ' + self.name)


def fmt_size(n):
    if n >= 1024 * GiB:
        return '%.1f TiB' % (n / (1024 * GiB))
    if n >= GiB:
        v = n / GiB
        return ('%.0f GiB' if v >= 100 else '%.1f GiB') % v
    return '%d MiB' % (n // MiB)


def _rd(path, default=''):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


_SKIP = ('loop', 'ram', 'zram', 'sr', 'fd', 'dm-', 'md', 'nbd', 'nullb', 'zd')


def mounted_sources():
    src = set()
    try:
        for line in open('/proc/mounts'):
            d = line.split()[0]
            if d.startswith('/dev/'):
                src.add(os.path.realpath(d))
    except OSError:
        pass
    return src


def disk_in_use(name, mounted):
    nodes = ['/dev/' + name]
    nodes += ['/dev/' + os.path.basename(os.path.dirname(p))
              for p in glob.glob('/sys/block/' + name + '/*/partition')]
    swaps = set()
    for line in _rd('/proc/swaps').splitlines()[1:]:
        swaps.add(os.path.realpath(line.split()[0]))
    return any(node in mounted or node in swaps or
               glob.glob('/sys/class/block/' + os.path.basename(node) + '/holders/*') for node in nodes)


def validate_disk(disk, media):
    current = next((d for d in list_disks(media) if d.path == disk.path), None)
    if (current is None or not current.usable or not disk.identity or
            current.identity != disk.identity or (current.size, current.lbs) != (disk.size, disk.lbs)):
        raise InstallError('The selected disk changed or is in use. Select the disk again.')


def list_disks(media=None, allow_loop=False, min_size=4 * GiB):
    skip_name = as_media(media).parent_disk() if media else None
    mounted = mounted_sources()
    disks = []
    for name in sorted(os.listdir('/sys/block')):
        if name.startswith(_SKIP) and not (allow_loop and name.startswith('loop')):
            continue
        base = '/sys/block/' + name
        try:
            sectors = int(_rd(base + '/size', '0'))
        except ValueError:
            continue
        if sectors == 0 or not os.path.exists('/dev/' + name):
            continue
        size = sectors * 512
        lbs = int(_rd(base + '/queue/logical_block_size', '512') or 512)
        model = ' '.join((_rd(base + '/device/vendor') + ' ' + _rd(base + '/device/model')).split())
        if not model:
            model = _rd(base + '/device/name')
        real = os.path.realpath(base)
        if name.startswith('nvme'):
            tran = 'NVMe'
        elif '/usb' in real:
            tran = 'USB'
        elif name.startswith('vd'):
            tran = 'VirtIO'
        elif name.startswith('mmcblk'):
            tran = 'MMC'
        else:
            tran = 'SATA/SCSI' if name.startswith('sd') else ''
        reason = ''
        if name == skip_name:
            reason = 'This is the installation medium'
        elif _rd(base + '/ro') == '1':
            reason = 'Read-only'
        elif size < min_size:
            reason = 'Too small (at least %s needed)' % fmt_size(min_size)
        else:
            if disk_in_use(name, mounted):
                reason = 'In use (mounted, swap, or storage mapping)'
        disks.append(Disk(name, size, lbs, model, tran, _rd(base + '/removable') == '1', reason=reason))
    return disks


def part_path(disk_path, n):
    base = os.path.basename(disk_path)
    sep = 'p' if base[-1].isdigit() else ''
    return '%s%s%d' % (disk_path, sep, n)


def ram_bytes():
    try:
        for line in open('/proc/meminfo'):
            if line.startswith('MemTotal:'):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 2 * GiB


def default_swap_bytes(disk_size):
    if disk_size < 24 * GiB:
        return 0
    return int(min(max(ram_bytes(), 1 * GiB), 4 * GiB) // MiB * MiB)


# ---------------------------------------------------------------------------
# partition layout + GPT
# ---------------------------------------------------------------------------
GUID_ESP = 'C12A7328-F81F-11D2-BA4B-00A0C93EC93B'
GUID_BIOS = '21686148-6449-6E6F-744E-656564454649'
GUID_LINUX = '0FC63DAF-8483-4772-8E79-3D69D8477DE4'
GUID_SWAP = '0657FD6D-A4AB-43C4-84E5-0933C84B4F4F'
ESP_SIZE = 512 * MiB


class Part:
    def __init__(self, kind, first, last, guid, name, fs, label=''):
        self.kind, self.first, self.last = kind, first, last
        self.guid, self.name, self.fs, self.label = guid, name, fs, label
        self.uuid = str(uuid.uuid4())           # GPT partition GUID
        self.number = 0
        self.fs_uuid = None

    def bytes(self, lbs):
        return (self.last - self.first + 1) * lbs


def gpt_entry_sectors(lbs):
    return math.ceil(128 * 128 / lbs)


def plan_layout(size, lbs, uefi, swap_bytes):
    """Partition plan: [boot (ESP or BIOS-boot), root, (swap)] — MiB aligned, GPT."""
    align = MiB // lbs if lbs <= MiB else 1
    total = size // lbs
    esec = gpt_entry_sectors(lbs)
    end = ((total - 1 - esec) // align) * align          # exclusive, aligned
    cur = align
    parts = []
    if uefi:
        n = ESP_SIZE // lbs
        parts.append(Part('esp', cur, cur + n - 1, GUID_ESP, 'EFI System Partition', 'vfat'))
    else:
        n = MiB // lbs
        parts.append(Part('bios', cur, cur + n - 1, GUID_BIOS, 'BIOS boot', None))
    cur += n
    swap_sec = (swap_bytes // MiB) * align if swap_bytes else 0
    root_end = end - swap_sec
    if root_end - cur < (1 * GiB) // lbs:
        raise InstallError('The disk is too small for this installation.')
    parts.append(Part('root', cur, root_end - 1, GUID_LINUX, BRAND, 'ext4', BRAND))
    if swap_sec:
        parts.append(Part('swap', root_end, end - 1, GUID_SWAP, 'swap', 'swap', 'swap'))
    for i, p in enumerate(parts, 1):
        p.number = i
    return parts


def build_gpt(total, lbs, parts, disk_guid):
    """Returns [(byte_offset, bytes)] covering protective MBR, both headers, both entry arrays."""
    esec = gpt_entry_sectors(lbs)
    ents = bytearray(128 * 128)
    for i, p in enumerate(parts):
        ents[i * 128:(i + 1) * 128] = (uuid.UUID(p.guid).bytes_le + uuid.UUID(p.uuid).bytes_le +
                                       struct.pack('<QQQ', p.first, p.last, 0) +
                                       p.name.encode('utf-16le').ljust(72, b'\0')[:72])
    ecrc = zlib.crc32(bytes(ents)) & 0xFFFFFFFF
    first_usable, last_usable = 2 + esec, total - 2 - esec

    def header(cur, alt, elba):
        h = bytearray(92)
        h[0:8] = b'EFI PART'
        struct.pack_into('<IIII', h, 8, 0x00010000, 92, 0, 0)
        struct.pack_into('<QQQQ', h, 24, cur, alt, first_usable, last_usable)
        h[56:72] = uuid.UUID(disk_guid).bytes_le
        struct.pack_into('<QIII', h, 72, elba, 128, 128, ecrc)
        struct.pack_into('<I', h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h).ljust(lbs, b'\0')

    mbr = bytearray(lbs)
    struct.pack_into('<B3sB3sII', mbr, 446, 0, b'\x00\x02\x00', 0xEE, b'\xFF\xFF\xFF', 1,
                     min(total - 1, 0xFFFFFFFF))
    mbr[510:512] = b'\x55\xAA'
    return [(0, bytes(mbr)),
            (lbs, header(1, total - 1, 2)),
            (2 * lbs, bytes(ents)),
            ((total - 1 - esec) * lbs, bytes(ents)),
            ((total - 1) * lbs, header(total - 1, 1, total - 1 - esec))]


# ---------------------------------------------------------------------------
# environment hooks (overridden by the test-suite)
# ---------------------------------------------------------------------------
BLKRRPART = 0x125F


class KernelPartitions:
    """Uses the kernel's own partition scanning (what a real disk needs)."""

    def rescan(self, disk, parts):
        fd = os.open(disk.path, os.O_RDONLY)
        try:
            for _ in range(50):
                try:
                    fcntl.ioctl(fd, BLKRRPART)
                    break
                except OSError as e:
                    if e.errno != errno.EBUSY:
                        raise InstallError('The kernel could not re-read the new partition table.', str(e))
                    time.sleep(0.2)
            else:
                raise InstallError('The disk is still busy; the new partition table could not be loaded.')
        finally:
            os.close(fd)
        for p in parts:
            path = part_path(disk.path, p.number)
            for _ in range(100):
                if os.path.exists(path):
                    break
                time.sleep(0.1)
            else:
                raise InstallError('Partition device %s did not appear.' % path)

    def path(self, disk, part):
        return part_path(disk.path, part.number)

    def release(self):
        pass


class Env:
    def __init__(self, media='/run/iso', target='/mnt/target', scanner=None, uefi=None, mke2fs=None):
        self.media, self.target = as_media(media), target
        self.scanner = scanner or KernelPartitions()
        self._uefi = uefi
        self._mke2fs = mke2fs

    @property
    def uefi(self):
        return os.path.isdir('/sys/firmware/efi') if self._uefi is None else self._uefi

    def mke2fs(self):
        if self._mke2fs:
            return list(self._mke2fs)
        for c in ('/usr/local/sbin/mkfs.ext4', '/usr/sbin/mkfs.ext4', '/sbin/mkfs.ext4', '/usr/sbin/mke2fs'):
            if os.path.exists(c):
                return [c]
        raise InstallError('No ext4 formatting tool is available in the installer.')


# ---------------------------------------------------------------------------
# chroot helper
# ---------------------------------------------------------------------------
class Chroot:
    def __init__(self, root):
        self.root = root
        self.mounts = []

    def _mount(self, args, dst):
        sh(['mount'] + args + [os.path.join(self.root, dst)], what='mount ' + dst)
        self.mounts.append(dst)

    def __enter__(self):
        for d in ('dev', 'proc', 'sys', 'run'):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        self._mount(['--rbind', '/dev'], 'dev')
        self._mount(['-t', 'proc', 'proc'], 'proc')
        self._mount(['--rbind', '/sys'], 'sys')
        self._mount(['-t', 'tmpfs', 'tmpfs'], 'run')
        return self

    def __exit__(self, *a):
        for d in reversed(self.mounts):
            p = os.path.join(self.root, d)
            if sh(['umount', '-R', p], check=False).returncode != 0:
                sh(['umount', '-R', '-l', p], check=False)
        self.mounts = []

    def run(self, cmd, **kw):
        return sh(['chroot', self.root] + list(cmd), **kw)


# ---------------------------------------------------------------------------
# the installation
# ---------------------------------------------------------------------------
class Config:
    def __init__(self, disk, hostname, username, fullname, password, swap_bytes=0, root_password=None):
        self.disk, self.hostname, self.username = disk, hostname, username
        self.fullname, self.password, self.swap_bytes = fullname, password, swap_bytes
        self.root_password = root_password          # None = leave the root account locked


STEPS = [
    ('Preparing the disk', 4),
    ('Creating file systems', 4),
    ('Copying the system', 70),
    ('Configuring the system', 6),
    ('Installing the bootloader', 12),
    ('Finishing up', 4),
]

GRUB_BIOS_MODS = ['biosdisk', 'part_gpt', 'part_msdos', 'ext2', 'search', 'search_fs_uuid', 'search_fs_file',
                  'normal', 'configfile', 'linux', 'boot', 'echo', 'test', 'gzio']
GRUB_EFI_MODS = ['part_gpt', 'part_msdos', 'fat', 'ext2', 'search', 'search_fs_uuid', 'search_fs_file',
                 'normal', 'configfile', 'linux', 'boot', 'echo', 'test', 'gzio', 'efi_gop', 'all_video',
                 'chain', 'loadenv', 'efifwsetup']
USER_GROUPS = ['sudo', 'adm', 'cdrom', 'audio', 'video', 'plugdev', 'users']


class Installer:
    def __init__(self, cfg, env, progress=None):
        """progress(step_index, fraction_of_step, message)"""
        self.cfg, self.env = cfg, env
        self._progress = progress or (lambda *a: None)
        self.info = load_media_info(env.media)
        self.osr = self.info['os_release']
        self.parts = []
        self.ch = None
        self.root_dev = None

    def progress(self, step, frac=0.0, msg=''):
        self._progress(step, max(0.0, min(1.0, frac)), msg)

    # -------------------------------------------------------------- driver
    def run(self):
        log('=== ouatos install started: %s -> %s (%s) ===' % (
            system_name(self.osr), self.cfg.disk.path, 'UEFI' if self.env.uefi else 'BIOS'))
        try:
            error = check_username(self.cfg.username, self.info['system_users'], self.info['system_groups'])
            if error:
                raise InstallError(error)
            if not self.env.media.exists():
                raise InstallError('The system image could not be found on the installation medium.')
            self.step_prepare_disk()
            self.step_make_filesystems()
            self.step_extract()
            self.step_configure()
            self.step_bootloader()
            self.step_finish()
        except InstallError:
            raise
        except Exception as e:                        # unexpected bug: still tell the user something useful
            log('UNEXPECTED: %r' % (e,))
            import traceback
            log(traceback.format_exc())
            raise InstallError('An unexpected error occurred: %s' % e, traceback.format_exc()[-1500:])
        finally:
            self.cleanup()

    def cleanup(self):
        if self.ch:
            self.ch.__exit__()
            self.ch = None
        sh(['umount', '-R', self.env.target], check=False)

    # -------------------------------------------------------------- 1. partitions
    def step_prepare_disk(self):
        d = self.cfg.disk
        self.progress(0, 0, 'Partitioning %s' % d.path)
        total = d.size // d.lbs
        self.parts = plan_layout(d.size, d.lbs, self.env.uefi, self.cfg.swap_bytes)
        root = next(p for p in self.parts if p.kind == 'root')
        need = int(self.info['installed_bytes'] * 1.3) + 256 * MiB
        if root.bytes(d.lbs) < need:
            raise InstallError('The disk is too small: %s are needed for the system.' % fmt_size(need))
        log('layout: ' + ', '.join('p%d %s %d-%d' % (p.number, p.kind, p.first, p.last) for p in self.parts))
        if isinstance(self.env.scanner, KernelPartitions):
            validate_disk(d, self.env.media)
        with open(d.path, 'r+b', buffering=0) as f:
            if isinstance(self.env.scanner, KernelPartitions):
                # Linux disk sequence identifies a device across path reuse after hotplug.
                seq = struct.unpack('Q', fcntl.ioctl(f.fileno(), 0x80081280, bytes(8)))[0]
                if str(seq) != d.identity:
                    raise InstallError('The selected disk was replaced. Select the disk again.')
                validate_disk(d, self.env.media)
            zero = bytes(MiB)
            for off in (0, d.size - MiB):                     # old partition tables / signatures
                f.seek(off)
                f.write(zero)
            for off, data in build_gpt(total, d.lbs, self.parts, str(uuid.uuid4())):
                f.seek(off)
                f.write(data)
            os.fsync(f.fileno())
        self.progress(0, 0.5, 'Re-reading the partition table')
        self.env.scanner.rescan(d, self.parts)
        for p in self.parts:                                  # clear stale signatures inside the new partitions
            path = self.env.scanner.path(d, p)
            size = p.bytes(d.lbs)
            with open(path, 'r+b', buffering=0) as f:
                f.write(bytes(MiB))
                f.seek(max(0, size - MiB))
                f.write(bytes(MiB if size >= 2 * MiB else size))
        self.progress(0, 1.0, 'Partition table written')

    # -------------------------------------------------------------- 2. filesystems
    def step_make_filesystems(self):
        d = self.cfg.disk
        for p in self.parts:
            dev = self.env.scanner.path(d, p)
            if p.kind == 'root':
                p.fs_uuid = str(uuid.uuid4())
                self.root_dev = dev
                self.progress(1, 0.1, 'Creating the ext4 file system')
                sh(self.env.mke2fs() + ['-q', '-F', '-t', 'ext4', '-O', '^orphan_file', '-L', BRAND,
                                        '-U', p.fs_uuid, '-E', 'lazy_itable_init=1,lazy_journal_init=1', dev],
                   what='Creating the ext4 file system')
            elif p.kind == 'swap':
                p.fs_uuid = str(uuid.uuid4())
                self.progress(1, 0.7, 'Creating swap space')
                sh(['mkswap', '-q', '-L', 'swap', '-U', p.fs_uuid, dev], what='Creating swap space')
            elif p.kind == 'esp':
                p.fs_uuid = '%04X-%04X' % (uuid.uuid4().int & 0xFFFF, (uuid.uuid4().int >> 16) & 0xFFFF)
        self.progress(1, 1.0, 'File systems ready')

    # -------------------------------------------------------------- 3. extract
    def step_extract(self):
        tgt = self.env.target
        os.makedirs(tgt, exist_ok=True)
        sh(['mount', '-t', 'ext4', '-o', 'noatime', self.root_dev, tgt], what='Mounting the new system')
        src = self.env.media
        if not src.exists():
            raise InstallError('The system image could not be found on the installation medium.', src.describe())
        total = src.payload_size()
        self.progress(2, 0, 'Copying files')
        cmd = ['tar', '-x', '-p', '--numeric-owner', '--xattrs', '--xattrs-include=*', '-C', tgt, '-f', '-']
        log('$ ' + ' '.join(cmd) + '   (fed by an xz decoder)')
        errf = open('/tmp/ouatos-tar.err', 'wb')
        tp = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
        dec = lzma.LZMADecompressor()
        last = 0.0
        try:
            with src.open_payload() as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    while True:
                        tp.stdin.write(dec.decompress(chunk, max_length=1 << 20))
                        chunk = b''
                        if dec.eof or dec.needs_input:
                            break
                    now = time.time()
                    if now - last > 0.25:
                        last = now
                        self.progress(2, f.tell() / total, 'Copying files')
            if not dec.eof:
                raise InstallError('The system image is damaged (unexpected end of data).')
            tp.stdin.close()
        except Exception as e:
            tp.kill()
            tp.wait()
            try:
                tp.stdin.close()
            except OSError:
                pass
            errf.close()
            err = Path('/tmp/ouatos-tar.err').read_bytes()[-800:].decode('utf-8', 'replace')
            raise InstallError('Copying the system failed.', '%s\n%s' % (e, err))
        rc = tp.wait()
        errf.close()
        err = Path('/tmp/ouatos-tar.err').read_bytes().decode('utf-8', 'replace')
        if err.strip():
            log('tar: ' + err[-1500:])
        if rc != 0:
            raise InstallError('Copying the system failed (tar exit status %d).' % rc, err[-1500:])
        if not glob.glob(os.path.join(tgt, 'boot', 'vmlinuz-*')) or not os.path.exists(os.path.join(tgt, 'usr/bin/bash')):
            raise InstallError('The copied system looks incomplete.')
        self.progress(2, 1.0, 'System copied')

    # -------------------------------------------------------------- 4. configure
    def _write(self, rel, text, mode=0o644):
        path = os.path.join(self.env.target, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.lexists(path):
            os.unlink(path)
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, mode)

    def step_configure(self):
        tgt, cfg = self.env.target, self.cfg
        self.progress(3, 0.05, 'Writing system settings')
        # target's own os-release is the source of truth for names from here on
        try:
            with open(os.path.join(tgt, 'etc/os-release')) as f:
                self.osr = parse_os_release(f.read()) or self.osr
        except OSError:
            pass
        # directories, ownership and permissions (the OS image carries no empty directories, flat owners/modes)
        self.progress(3, 0.02, 'Creating directories and setting ownership')
        created = policy.ensure_standard_dirs(tgt)
        log('created %d standard directories missing from the image: %s' % (len(created), ', '.join(created[:14]) + (' ...' if len(created) > 14 else '')))
        policy.fix_special_dirs(tgt)                      # /tmp, /var/tmp = root:root 1777 ; /root = 0700
        fixed = policy.enforce_root_ownership(tgt)        # everything outside /home/<user> is root:root
        log('ownership: %d entries were not root:root and were reset%s' % (len(fixed), (': ' + ', '.join(fixed[:8])) if fixed else ''))
        perms = policy.apply_baseline_permissions(tgt)
        log('permissions: ' + ('; '.join(perms) if perms else 'nothing to change'))
        # hostname / hosts
        self._write('etc/hostname', cfg.hostname + '\n')
        try:
            hosts = open(os.path.join(tgt, 'etc/hosts')).read().splitlines()
        except OSError:
            hosts = ['127.0.0.1\tlocalhost', '::1\t\tlocalhost ip6-localhost ip6-loopback']
        hosts = [h for h in hosts if not re.match(r'127\.0\.1\.1\s', h)]
        i = next((k + 1 for k, h in enumerate(hosts) if h.startswith('127.0.0.1')), 0)
        hosts.insert(i, '127.0.1.1\t%s' % cfg.hostname)
        self._write('etc/hosts', '\n'.join(hosts) + '\n')
        # fstab (no fsck: the target system ships no e2fsprogs, so fsck passes stay 0)
        fstab = ['# /etc/fstab: static file system information (written by the %s installer).' % BRAND,
                 '# <file system> <mount point> <type> <options> <dump> <pass>']
        for p in self.parts:
            if p.kind == 'root':
                fstab.append('UUID=%s\t/\text4\terrors=remount-ro\t0\t0' % p.fs_uuid)
            elif p.kind == 'esp':
                fstab.append('UUID=%s\t/boot/efi\tvfat\tumask=0077,nofail\t0\t0' % p.fs_uuid)
            elif p.kind == 'swap':
                fstab.append('UUID=%s\tnone\tswap\tsw\t0\t0' % p.fs_uuid)
        self._write('etc/fstab', '\n'.join(fstab) + '\n')
        if self.env.uefi:
            os.makedirs(os.path.join(tgt, 'boot/efi'), exist_ok=True)
        # machine-id: a fresh random ID for this installation
        self.progress(3, 0.3, 'Initialising the machine ID')
        mid = policy.init_machine_id(tgt)
        log('machine-id initialised (%s...)' % mid[:8])
        # accounts
        self.progress(3, 0.4, 'Creating the user account')
        self.ch = Chroot(tgt).__enter__()
        try:
            existing = {l.split(':')[0] for l in open(os.path.join(tgt, 'etc/group'))}
        except OSError:
            existing = set()
        if 'sudo' not in existing:
            self.ch.run(['/usr/sbin/groupadd', '-r', 'sudo'], what='Creating the sudo group')
            existing.add('sudo')
        groups = [g for g in USER_GROUPS if g in existing]
        # -U: the primary group is a new group named like the user, so /home/<user> is <user>:<user>
        cmd = ['/usr/sbin/useradd', '-m', '-U', '-s', '/bin/bash', '-G', ','.join(groups)]
        fn = clean_fullname(cfg.fullname)
        if fn:
            cmd += ['-c', fn]
        self.ch.run(cmd + [cfg.username], what='Creating the user account')
        creds = '%s:%s\n' % (cfg.username, cfg.password)
        if cfg.root_password is not None:
            creds += 'root:%s\n' % cfg.root_password
        self.progress(3, 0.7, 'Setting passwords')
        self.ch.run(['/usr/sbin/chpasswd'], input=creds.encode(), what='Setting the passwords')
        # /home/<user> belongs to <user>:<user>: enforce it explicitly, whatever useradd did
        pw = next(r for r in policy.parse_colon_file(os.path.join(tgt, 'etc/passwd')) if r[0] == cfg.username)
        uid, gid = int(pw[2]), int(pw[3])
        home = os.path.join(tgt, 'home', cfg.username)
        for r, ds, fs in os.walk(home):
            for n in ds + fs:
                os.lchown(os.path.join(r, n), uid, gid)
        os.chown(home, uid, gid)
        problems = policy.verify_policy(tgt, cfg.username)
        if problems:
            log('POLICY PROBLEMS: ' + '; '.join(problems))
            raise InstallError('The installed system does not match the required ownership rules.', '\n'.join(problems))
        self.progress(3, 1.0, 'User "%s" created' % cfg.username)

    # -------------------------------------------------------------- 5. bootloader
    def _grub_modules(self, platform, wanted):
        have = lambda m: os.path.exists(os.path.join(self.env.target, 'usr/lib/grub', platform, m + '.mod'))
        return [m for m in wanted if have(m)]

    def step_bootloader(self):
        tgt, d = self.env.target, self.cfg.disk
        root = next(p for p in self.parts if p.kind == 'root')
        brand = brand_name(self.osr)
        self.progress(4, 0.05, 'Preparing the bootloader')
        self._write('etc/default/grub',
                    '# Written by the %s installer.  Run `grub-mkconfig -o /boot/grub/grub.cfg` after changing it.\n'
                    'GRUB_DEFAULT=0\nGRUB_TIMEOUT=5\nGRUB_DISTRIBUTOR="%s"\n'
                    'GRUB_CMDLINE_LINUX_DEFAULT="quiet"\nGRUB_CMDLINE_LINUX=""\nGRUB_DISABLE_OS_PROBER=true\n'
                    % (BRAND, brand))
        self._write('tmp/ouatos-early.cfg',
                    'if search --no-floppy --fs-uuid --set=root %s; then\n  set prefix=($root)/boot/grub\nfi\n'
                    % root.fs_uuid)
        ch = self.ch
        plat = 'x86_64-efi' if self.env.uefi else 'i386-pc'
        ch.run(['mkdir', '-p', '/boot/grub/' + plat])
        ch.run(['sh', '-c', 'cp /usr/lib/grub/%s/*.mod /usr/lib/grub/%s/*.lst /boot/grub/%s/' % (plat, plat, plat)],
               what='Copying bootloader modules')
        if self.env.uefi:
            self._install_efi(root)
        else:
            self._install_bios(root)
        self.progress(4, 0.8, 'Creating the boot menu')
        self._grub_cfg(root, brand)
        self.progress(4, 1.0, 'Bootloader installed')

    def _install_bios(self, root):
        d = self.cfg.disk
        mods = self._grub_modules('i386-pc', GRUB_BIOS_MODS)
        self.progress(4, 0.2, 'Building the BIOS boot image')
        self.ch.run(['/usr/bin/grub-mkimage', '-O', 'i386-pc', '-d', '/usr/lib/grub/i386-pc',
                     '-o', '/boot/grub/i386-pc/core.img', '-p', '(,gpt%d)/boot/grub' % root.number,
                     '-c', '/tmp/ouatos-early.cfg'] + mods, what='Building the bootloader image')
        self.ch.run(['cp', '/usr/lib/grub/i386-pc/boot.img', '/boot/grub/i386-pc/boot.img'])
        self.progress(4, 0.5, 'Writing the bootloader to the disk')
        self.ch.run(['/usr/lib/grub/i386-pc/grub-bios-setup', '-d', '/boot/grub/i386-pc', '-b', 'boot.img',
                     '-c', 'core.img', '--skip-fs-probe', '-f', d.path], what='Installing the BIOS bootloader')

    def _install_efi(self, root):
        d, tgt = self.cfg.disk, self.env.target
        esp = next(p for p in self.parts if p.kind == 'esp')
        mods = self._grub_modules('x86_64-efi', GRUB_EFI_MODS)
        self.progress(4, 0.2, 'Building the UEFI boot image')
        self.ch.run(['/usr/bin/grub-mkimage', '-O', 'x86_64-efi', '-d', '/usr/lib/grub/x86_64-efi',
                     '-o', '/tmp/ouatos-grubx64.efi', '-p', '/boot/grub', '-c', '/tmp/ouatos-early.cfg'] + mods,
                    what='Building the bootloader image')
        with open(os.path.join(tgt, 'tmp/ouatos-grubx64.efi'), 'rb') as f:
            efi = f.read()
        self.progress(4, 0.45, 'Writing the EFI system partition')
        dev = self.env.scanner.path(d, esp)
        volid = int(esp.fs_uuid.replace('-', ''), 16)
        with open(dev, 'r+b', buffering=0) as f:
            fs = fatfs.FatFS(f, 0, esp.bytes(d.lbs), d.lbs, label='EFI', volid=volid, fat_bits=32,
                             hidden_sectors=esp.first)
            fs.write_file('EFI/BOOT/BOOTX64.EFI', efi)          # removable-media path: works on any firmware
            fs.write_file('EFI/%s/grubx64.efi' % BRAND, efi)   # our own entry
            fs.close()
        self.progress(4, 0.6, 'Registering the boot entry')
        r = self.ch.run(['/usr/bin/efibootmgr', '-c', '-d', d.path, '-p', str(esp.number), '-L', BRAND,
                         '-l', '\\EFI\\%s\\grubx64.efi' % BRAND], check=False)
        if r.returncode != 0:
            log('efibootmgr failed; relying on the removable-media boot path (EFI/BOOT/BOOTX64.EFI)')

    def _wait_uuid_link(self, root):
        """grub-mkconfig only emits root=UUID=... once udev has published /dev/disk/by-uuid/<uuid>."""
        link = '/dev/disk/by-uuid/' + root.fs_uuid
        if os.path.exists(link) or not os.path.isdir('/run/udev'):
            return
        sh(['udevadm', 'trigger', '--action=change', self.root_dev], check=False)
        sh(['udevadm', 'settle', '--timeout=10'], check=False)
        for _ in range(50):
            if os.path.exists(link):
                return
            time.sleep(0.1)
        log('udev did not publish %s; a fallback boot configuration may be used' % link)

    def _grub_cfg(self, root, brand):
        ch, tgt = self.ch, self.env.target
        ok = False
        self._wait_uuid_link(root)
        r = ch.run(['/usr/sbin/grub-mkconfig', '-o', '/boot/grub/grub.cfg'], check=False)
        try:
            cfg = open(os.path.join(tgt, 'boot/grub/grub.cfg')).read()
            ok = (r.returncode == 0 and 'menuentry' in cfg and 'root=UUID=%s' % root.fs_uuid in cfg)
        except OSError:
            pass
        if ok:
            log('grub.cfg generated by grub-mkconfig')
            return
        log('grub-mkconfig output unusable; writing a minimal grub.cfg instead')
        kernels = sorted(glob.glob(os.path.join(tgt, 'boot/vmlinuz-*')))
        if not kernels:
            raise InstallError('No kernel was found in the installed system.')
        ver = os.path.basename(kernels[-1])[len('vmlinuz-'):]
        name = system_name(self.osr)
        self._write('boot/grub/grub.cfg', (
            '# Minimal configuration written by the %(b)s installer; regenerate with grub-mkconfig.\n'
            'set default=0\nset timeout=5\ninsmod part_gpt\ninsmod ext2\n'
            'search --no-floppy --fs-uuid --set=root %(u)s\n'
            'menuentry \'%(n)s\' {\n  linux /boot/vmlinuz-%(v)s root=UUID=%(u)s ro quiet\n  initrd /boot/initrd.img-%(v)s\n}\n'
            'menuentry \'%(n)s (recovery mode)\' {\n  linux /boot/vmlinuz-%(v)s root=UUID=%(u)s ro single\n'
            '  initrd /boot/initrd.img-%(v)s\n}\n') % dict(b=BRAND, u=root.fs_uuid, n=name, v=ver))

    # -------------------------------------------------------------- 6. finish
    def step_finish(self):
        tgt = self.env.target
        self.progress(5, 0.1, 'Cleaning up')
        for f in ('tmp/ouatos-early.cfg', 'tmp/ouatos-grubx64.efi'):
            try:
                os.unlink(os.path.join(tgt, f))
            except OSError:
                pass
        try:
            shutil.copy(LOG_FILE, os.path.join(tgt, 'var/log/ouatos-install.log'))
        except OSError:
            pass
        if self.ch:
            self.ch.__exit__()
            self.ch = None
        self.progress(5, 0.4, 'Writing data to disk')
        os.sync()
        sh(['umount', '-R', tgt], what='Unmounting the new system')
        os.sync()
        self.progress(5, 1.0, 'Done')


# ---------------------------------------------------------------------------
# power
# ---------------------------------------------------------------------------
def power(action):
    os.sync()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.reboot({'reboot': 0x01234567, 'poweroff': 0x4321FEDC}[action])
