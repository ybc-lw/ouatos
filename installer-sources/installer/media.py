#!/usr/bin/env python3
"""Finding and reading the installation medium.

The medium is located by scanning every block device for an ISO 9660 volume whose label is
`ouatos_install`, and the payload is read straight from the raw device with a small ISO 9660 reader.
Nothing here depends on the kernel's isofs driver, on Rock Ridge being honoured, on blkid, or on udev:
only on the block device node existing and being readable.

CLI (used by the live /init script):
    media.py find            print matching devices, one per line
    media.py check DEV       verify that DEV contains the payload (exit 0 = yes)
    media.py list            human readable report of every block device (for diagnostics)
"""
import io
import json
import os
import sys

LABEL = 'ouatos_install'
SECTOR = 2048
PAYLOAD_NAMES = ('rootfs.txz', 'rootfs.tar.xz')
INFO_NAMES = ('rootfsinfo.json', 'rootfs-info.json')
OSREL_NAMES = ('os_release', 'os-release')
DEVFILE = '/run/media.dev'


class MediaError(Exception):
    pass


# --------------------------------------------------------------------------- ISO 9660 reader
class IsoReader:
    def __init__(self, f):
        self.f = f
        pvd = self.read(16 * SECTOR, SECTOR)
        if len(pvd) < SECTOR or pvd[0] != 1 or pvd[1:6] != b'CD001':
            raise MediaError('no ISO 9660 volume')
        self.label = pvd[40:72].decode('latin1').strip()
        self.nsectors = int.from_bytes(pvd[80:84], 'little')
        root = pvd[156:190]
        self.root = (int.from_bytes(root[2:6], 'little'), int.from_bytes(root[10:14], 'little'))

    def read(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def _entries(self, lba, length):
        raw = self.read(lba * SECTOR, length)
        off = 0
        while off < len(raw):
            ln = raw[off]
            if ln == 0:
                off = (off // SECTOR + 1) * SECTOR
                continue
            rec, off = raw[off:off + ln], off + ln
            idl = rec[32]
            ident = rec[33:33 + idl]
            if ident in (b'\x00', b'\x01'):
                continue
            su = rec[33 + idl + (1 if idl % 2 == 0 else 0):]
            rr, i = b'', 0
            while i + 4 <= len(su):                      # Rock Ridge alternate name, if present
                sig, sl = su[i:i + 2], su[i + 2]
                if sl < 4:
                    break
                if sig == b'NM' and not su[i + 4] & 6:
                    rr += su[i + 5:i + sl]
                i += sl
            if rr:
                name = rr.decode('utf-8', 'replace')
            else:                                         # plain ISO name: drop ;1 and a trailing dot, lower-case
                name = ident.decode('latin1').split(';')[0].rstrip('.').lower()
            yield name, bool(rec[25] & 2), int.from_bytes(rec[2:6], 'little'), int.from_bytes(rec[10:14], 'little')

    def lookup(self, path):
        """-> (lba, size) of a file, comparing names case-insensitively."""
        lba, length = self.root
        parts = [p for p in path.split('/') if p]
        for n, part in enumerate(parts):
            for name, isdir, elba, esize in self._entries(lba, length):
                if name.lower() == part.lower():
                    if n == len(parts) - 1:
                        return elba, esize
                    if isdir:
                        lba, length = elba, esize
                        break
            else:
                raise MediaError('not found on the medium: ' + path)
        raise MediaError('not a file: ' + path)


class ExtentReader(io.RawIOBase):
    """A read-only view of `size` bytes at `offset` of an open device; tell() is relative."""

    def __init__(self, f, offset, size):
        self.f, self.offset, self.size, self.pos = f, offset, size, 0

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def read(self, n=-1):
        left = self.size - self.pos
        n = left if n is None or n < 0 else min(n, left)
        if n <= 0:
            return b''
        self.f.seek(self.offset + self.pos)
        data = self.f.read(n)
        self.pos += len(data)
        return data

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)

    def close(self):
        try:
            self.f.close()
        finally:
            super().close()


# --------------------------------------------------------------------------- media sources
def _parent_disk(name):
    real = os.path.realpath('/sys/class/block/' + name)
    return os.path.basename(os.path.dirname(real)) if os.path.exists(real + '/partition') else os.path.basename(real)


class MountedMedia:
    """The medium is a directory (mounted ISO, or a test fixture)."""
    kind = 'mounted'

    def __init__(self, path):
        self.path = path

    def _find(self, names):
        for n in names:
            p = os.path.join(self.path, 'ouatos', n)
            if os.path.exists(p):
                return p
        return None

    def exists(self):
        return self._find(PAYLOAD_NAMES) is not None

    def payload_size(self):
        return os.path.getsize(self._find(PAYLOAD_NAMES))

    def open_payload(self):
        return open(self._find(PAYLOAD_NAMES), 'rb')

    def read_small(self, names):
        p = self._find(names)
        return open(p, 'rb').read() if p else None

    def parent_disk(self):
        try:
            for line in open('/proc/mounts'):
                dev, mp = line.split()[:2]
                if os.path.realpath(mp) == os.path.realpath(self.path) and dev.startswith('/dev/'):
                    return _parent_disk(os.path.basename(os.path.realpath(dev)))
        except (OSError, ValueError):
            pass
        return None

    def describe(self):
        return 'directory ' + self.path


class RawMedia:
    """The medium is a block device (or an image file) containing an ISO 9660 volume, read directly."""
    kind = 'raw'

    def __init__(self, device):
        self.device = device
        self._loc = {}

    def _locate(self, names):
        for n in names:
            if n in self._loc:
                return self._loc[n]
        with open(self.device, 'rb', buffering=0) as f:
            iso = IsoReader(f)
            for n in names:
                try:
                    self._loc[n] = iso.lookup('ouatos/' + n)
                    return self._loc[n]
                except MediaError:
                    continue
        return None

    def exists(self):
        try:
            return self._locate(PAYLOAD_NAMES) is not None
        except (OSError, MediaError):
            return False

    def payload_size(self):
        return self._locate(PAYLOAD_NAMES)[1]

    def open_payload(self):
        lba, size = self._locate(PAYLOAD_NAMES)
        return ExtentReader(open(self.device, 'rb', buffering=0), lba * SECTOR, size)

    def read_small(self, names):
        loc = self._locate(names)
        if not loc:
            return None
        with open(self.device, 'rb', buffering=0) as f:
            f.seek(loc[0] * SECTOR)
            return f.read(loc[1])

    def parent_disk(self):
        return _parent_disk(os.path.basename(os.path.realpath(self.device)))

    def describe(self):
        return 'device ' + self.device


def open_media(default_dir='/run/iso'):
    """Decide how to read the medium: explicit env vars, then the device found by /init, then a mounted dir."""
    if os.environ.get('OUATOS_MEDIA_DEV'):
        return RawMedia(os.environ['OUATOS_MEDIA_DEV'])
    if os.environ.get('OUATOS_MEDIA'):
        return MountedMedia(os.environ['OUATOS_MEDIA'])
    try:
        dev = open(DEVFILE).read().strip()
        if dev:
            return RawMedia(dev)
    except OSError:
        pass
    return MountedMedia(default_dir)


# --------------------------------------------------------------------------- scanning
def candidate_devices():
    names = []
    try:
        for line in open('/proc/partitions').read().splitlines()[2:]:
            p = line.split()
            if len(p) == 4:
                names.append(p[3])
    except OSError:
        pass
    return ['/dev/' + n for n in sorted(names, key=lambda n: (not n.startswith('sr'), n))]


def probe_label(dev):
    """Volume label of the ISO 9660 volume on dev, '' if none, None if it cannot be read."""
    try:
        with open(dev, 'rb', buffering=0) as f:
            f.seek(16 * SECTOR)
            d = f.read(SECTOR)
    except OSError:
        return None
    if len(d) == SECTOR and d[0] == 1 and d[1:6] == b'CD001':
        return d[40:72].decode('latin1').strip()
    return ''


def find_media(label=LABEL):
    return [d for d in candidate_devices() if probe_label(d) == label]


def report():
    lines = []
    devs = candidate_devices()
    if not devs:
        lines.append('no block devices listed in /proc/partitions')
    for d in devs:
        lab = probe_label(d)
        what = 'unreadable (no medium or no permission)' if lab is None else (
            'ISO 9660, label %r%s' % (lab, '  <== installation medium' if lab == LABEL else '') if lab else 'no ISO 9660 volume')
        lines.append('%-14s %s' % (d, what))
    return '\n'.join(lines)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else 'find'
    if cmd == 'find':
        for d in find_media():
            print(d)
    elif cmd == 'check':
        m = RawMedia(argv[2])
        ok = m.exists()
        print('%s: payload %s' % (argv[2], ('found, %d bytes' % m.payload_size()) if ok else 'NOT FOUND'))
        return 0 if ok else 1
    elif cmd == 'list':
        print(report())
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
