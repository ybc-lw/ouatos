"""Minimal FAT16/FAT32 formatter + writer for boot partitions.

Only what an EFI System Partition needs: format, create directories, write
contiguous files.  Works on regular files and block devices alike (seek/write
based) so it needs no kernel vfat driver and no mtools/dosfstools.
"""
import io
import math
import os
import struct
import time

ATTR_DIR, ATTR_VOL, ATTR_ARCHIVE = 0x10, 0x08, 0x20
LCASE_BASE, LCASE_EXT = 0x08, 0x10           # Windows-NT "display as lowercase" flags


def _dos_datetime(ts):
    t = time.gmtime(ts)
    date = ((max(t.tm_year, 1980) - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return date, tm


def short_name(name):
    """'grubx64.efi' -> (b'GRUBX64 EFI', lcase_flags).  Raises if not a pure 8.3 name."""
    if name in ('.', '..'):
        return name.encode().ljust(11, b' '), 0
    base, _, ext = name.partition('.')
    if len(base) > 8 or len(ext) > 3 or not base or '.' in ext:
        raise ValueError('not an 8.3 name: %r' % name)
    for part in (base, ext):
        if part != part.upper() and part != part.lower():
            raise ValueError('mixed case not representable in 8.3: %r' % name)
    flags = 0
    if base != base.upper():
        flags |= LCASE_BASE
    if ext and ext != ext.upper():
        flags |= LCASE_EXT
    return (base.upper().ljust(8) + ext.upper().ljust(3)).encode('ascii'), flags


class FatFS:
    def __init__(self, f, offset, size, lbs=512, label='NO NAME', volid=None,
                 fat_bits=32, hidden_sectors=0, mtime=None):
        self.f, self.off, self.size = f, offset, size
        self.bps = lbs
        self.fat_bits = fat_bits
        self.label = label.upper().ljust(11)[:11]
        self.volid = volid if volid is not None else (int(time.time()) & 0xFFFFFFFF)
        self.hidden = hidden_sectors
        self.mtime = mtime if mtime is not None else time.time()
        self._geometry()
        self.fat = bytearray(self.fatsz * self.bps)
        self._set_fat(0, 0x0FFFFFF8 if fat_bits == 32 else 0xFFF8)
        self._set_fat(1, 0x0FFFFFFF if fat_bits == 32 else 0xFFFF)
        self.next_free = 2
        self.used = 0
        self.dirs = {}
        self._format_root()

    # ------------------------------------------------------------ geometry
    def _geometry(self):
        bps, total = self.bps, self.size // self.bps
        self.total = total
        if self.fat_bits == 32:
            self.reserved, self.root_entries = 32, 0
            spc = max(1, 4096 // bps)
            clusters = 0
            while spc >= 1:
                clusters = (total - self.reserved) // spc
                fatsz = math.ceil((clusters + 2) * 4 / bps)
                clusters = (total - self.reserved - 2 * fatsz) // spc
                fatsz = math.ceil((clusters + 2) * 4 / bps)
                clusters = (total - self.reserved - 2 * fatsz) // spc
                if clusters >= 65525:
                    break
                spc //= 2
            if spc < 1 or clusters < 65525:
                raise ValueError('partition too small for FAT32')
            if clusters > 0x0FFFFFF5:
                raise ValueError('partition too large for this writer')
        else:
            self.reserved, self.root_entries = 1, 512
            root_sectors = (self.root_entries * 32 + bps - 1) // bps
            spc = 1
            while True:
                if spc > 128:
                    raise ValueError('cannot fit FAT16 geometry')
                clusters = (total - self.reserved - root_sectors) // spc
                fatsz = math.ceil((clusters + 2) * 2 / bps)
                clusters = (total - self.reserved - root_sectors - 2 * fatsz) // spc
                if 4085 <= clusters <= 65524:
                    break
                if clusters < 4085:
                    raise ValueError('image too small for FAT16')
                spc *= 2
        self.spc, self.fatsz, self.clusters = spc, fatsz, clusters
        self.cluster_bytes = spc * bps
        self.root_sectors = (self.root_entries * 32 + bps - 1) // bps
        self.fat_start = self.reserved
        self.root_start = self.reserved + 2 * fatsz          # FAT16 fixed root directory
        self.data_start = self.root_start + self.root_sectors

    # ------------------------------------------------------------ low level
    def _pwrite(self, pos, data):
        self.f.seek(self.off + pos)
        self.f.write(data)

    def _set_fat(self, n, v):
        if self.fat_bits == 32:
            struct.pack_into('<I', self.fat, n * 4, v & 0x0FFFFFFF)
        else:
            struct.pack_into('<H', self.fat, n * 2, v & 0xFFFF)

    def _eoc(self):
        return 0x0FFFFFFF if self.fat_bits == 32 else 0xFFFF

    def _alloc(self, nclusters):
        start = self.next_free
        if start + nclusters - 1 > self.clusters + 1:
            raise IOError('FAT volume full')
        for i in range(nclusters):
            c = start + i
            self._set_fat(c, c + 1 if i < nclusters - 1 else self._eoc())
        self.next_free += nclusters
        self.used += nclusters
        return start

    def _cluster_pos(self, c):
        return (self.data_start + (c - 2) * self.spc) * self.bps

    # ------------------------------------------------------------ format
    def _format_root(self):
        bps = self.bps
        bs = bytearray(bps)
        bs[0:3] = b'\xEB\x58\x90' if self.fat_bits == 32 else b'\xEB\x3C\x90'
        bs[3:11] = b'MSWIN4.1'
        small16 = self.fat_bits == 16 and self.total < 65536
        struct.pack_into('<HBHBHHBHHHII', bs, 11,
                         bps, self.spc, self.reserved, 2, self.root_entries,
                         self.total if small16 else 0, 0xF8,
                         0 if self.fat_bits == 32 else self.fatsz, 32, 64,
                         self.hidden, 0 if small16 else self.total)
        if self.fat_bits == 32:
            struct.pack_into('<IHHI', bs, 36, self.fatsz, 0, 0, 2)
            struct.pack_into('<HH', bs, 48, 1, 6)
            struct.pack_into('<BBBI', bs, 64, 0x80, 0, 0x29, self.volid)
            bs[71:82] = self.label.encode('ascii')
            bs[82:90] = b'FAT32   '
        else:
            struct.pack_into('<BBBI', bs, 36, 0x80, 0, 0x29, self.volid)
            bs[43:54] = self.label.encode('ascii')
            bs[54:62] = b'FAT16   '
        bs[510:512] = b'\x55\xAA'
        self.bootsector = bytes(bs)
        if self.fat_bits == 32:
            fs = bytearray(bps)
            struct.pack_into('<I', fs, 0, 0x41615252)
            struct.pack_into('<I', fs, 484, 0x61417272)
            struct.pack_into('<II', fs, 488, 0xFFFFFFFF, 0xFFFFFFFF)
            struct.pack_into('<I', fs, 508, 0xAA550000)
            self.fsinfo = fs
            c = self._alloc(1)                                # root directory cluster
            assert c == 2
            self.dirs['/'] = dict(cluster=2, entries=[], max=self.cluster_bytes // 32, root=False)
        else:
            self.dirs['/'] = dict(cluster=0, entries=[], max=self.root_entries, root=True)
        self._add_entry('/', self.label.encode('ascii'), ATTR_VOL, 0, 0, 0)

    # ------------------------------------------------------------ directories
    def _entry_bytes(self, name11, attr, lcase, cluster, size):
        d, t = _dos_datetime(self.mtime)
        return struct.pack('<11sBBBHHHHHHHI', name11, attr, lcase, 0, t, d, d,
                           (cluster >> 16) & 0xFFFF, t, d, cluster & 0xFFFF, size)

    def _add_entry(self, dirpath, name11, attr, lcase, cluster, size):
        dd = self.dirs[dirpath]
        if len(dd['entries']) >= dd['max']:
            raise IOError('directory full: %s' % dirpath)
        dd['entries'].append(self._entry_bytes(name11, attr, lcase, cluster, size))

    def mkdir(self, path):
        path = '/' + path.strip('/')
        if path in self.dirs:
            return
        parent, _, name = path.rpartition('/')
        parent = parent or '/'
        self.mkdir(parent)
        n11, lc = short_name(name)
        c = self._alloc(1)
        self._add_entry(parent, n11, ATTR_DIR, lc, c, 0)
        pc = 0 if parent == '/' else self.dirs[parent]['cluster']   # '..' of a top-level dir is cluster 0
        self.dirs[path] = dict(cluster=c, entries=[], max=self.cluster_bytes // 32, root=False)
        self.dirs[path]['entries'] += [self._entry_bytes(b'.          ', ATTR_DIR, 0, c, 0),
                                       self._entry_bytes(b'..         ', ATTR_DIR, 0, pc, 0)]

    def write_file(self, path, data):
        path = '/' + path.strip('/')
        parent, _, name = path.rpartition('/')
        parent = parent or '/'
        self.mkdir(parent)
        n11, lc = short_name(name)
        n = math.ceil(len(data) / self.cluster_bytes) if data else 0
        c = self._alloc(n) if n else 0
        if n:
            self._pwrite(self._cluster_pos(c), bytes(data))
        self._add_entry(parent, n11, ATTR_ARCHIVE, lc, c, len(data))

    # ------------------------------------------------------------ flush
    def close(self):
        bps = self.bps
        for d in self.dirs.values():
            raw = b''.join(d['entries'])
            if d['root']:
                self._pwrite(self.root_start * bps, raw.ljust(self.root_entries * 32, b'\0'))
            else:
                self._pwrite(self._cluster_pos(d['cluster']), raw.ljust(self.cluster_bytes, b'\0'))
        if self.fat_bits == 32:
            struct.pack_into('<II', self.fsinfo, 488, self.clusters - self.used, self.next_free)
            for base in (0, 6):
                self._pwrite(base * bps, self.bootsector)
                self._pwrite((base + 1) * bps, bytes(self.fsinfo))
        else:
            self._pwrite(0, self.bootsector)
        for i in range(2):
            self._pwrite((self.fat_start + i * self.fatsz) * bps, bytes(self.fat))
        self.f.flush()
        try:
            os.fsync(self.f.fileno())
        except (OSError, AttributeError, io.UnsupportedOperation):
            pass


def make_fat_image(files, size, fat_bits=16, label='EFIBOOT', mtime=None):
    """files: {path: bytes}.  Returns the bytes of a FAT image of `size` bytes."""
    buf = io.BytesIO(bytes(size))
    fs = FatFS(buf, 0, size, 512, label=label, volid=0x4F55415F, fat_bits=fat_bits, mtime=mtime)
    for p, data in files.items():
        fs.write_file(p, data)
    fs.close()
    return buf.getvalue()
