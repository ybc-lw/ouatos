"""Pure-Python ISO 9660 writer: Rock Ridge names, El Torito (BIOS + UEFI), hybrid MBR for UEFI USB boot.

Written from ECMA-119, SUSP 1.12, RRIP 1.12 and the El Torito 1.0 specification because no xorriso is
available in the build sandbox.  tests/isocheck.py is an independent reader used to verify the output.
"""
import os
import re
import shutil
import struct
import time

SECTOR = 2048


def b16(v):
    return struct.pack('<H', v) + struct.pack('>H', v)


def b32(v):
    return struct.pack('<I', v) + struct.pack('>I', v)


def rec_date(ts):
    t = time.gmtime(ts)
    return bytes((t.tm_year - 1900, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, 0))


def vol_date(ts):
    t = time.gmtime(ts)
    return ('%04d%02d%02d%02d%02d%02d00' % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)).encode() + b'\0'


def pad(s, n):
    return s.encode('ascii', 'replace')[:n].ljust(n, b' ')


class Node:
    def __init__(self, name, parent, is_dir, src=None, data=None, mode=None, mtime=0):
        self.name, self.parent, self.is_dir = name, parent, is_dir
        self.src, self.data = src, data
        self.mode = mode if mode is not None else (0o755 if is_dir else 0o644)
        self.mtime = mtime
        self.children = {}
        self.iso_id = b'\0'
        self.lba = 0
        self.size = 0
        self.dir_bytes = 0
        self.dnum = 1

    def path(self):
        return '/' if self.parent is None else (self.parent.path().rstrip('/') + '/' + self.name)


class ISO:
    def __init__(self, volid, sysid='LINUX', publisher='', preparer='', app='', mtime=None):
        self.volid, self.sysid, self.publisher, self.preparer, self.app = volid, sysid, publisher, preparer, app
        self.mtime = int(mtime if mtime is not None else time.time())
        self.root = Node('', None, True, mtime=self.mtime)
        self.order = []

    # ------------------------------------------------------------------ tree
    def _walk_to(self, parts, create=True):
        n = self.root
        for p in parts:
            c = n.children.get(p)
            if c is None:
                c = n.children[p] = Node(p, n, True, mtime=self.mtime)
            n = c
        return n

    def mkdir(self, path):
        return self._walk_to([p for p in path.split('/') if p])

    def add_file(self, path, src=None, data=None, mode=0o644):
        parts = [p for p in path.split('/') if p]
        parent = self._walk_to(parts[:-1])
        if src is not None:
            size = os.path.getsize(src)
        else:
            size = len(data)
        n = Node(parts[-1], parent, False, src=src, data=data, mode=mode, mtime=self.mtime)
        n.size = size
        parent.children[parts[-1]] = n
        self.order.append(n)
        return n

    def find(self, path):
        n = self.root
        for p in [p for p in path.split('/') if p]:
            n = n.children[p]
        return n

    # ------------------------------------------------------------------ identifiers
    @staticmethod
    def _iso_id(name, is_dir, used):
        clean = lambda s: re.sub(r'[^A-Z0-9_]', '_', s.upper())
        if is_dir:
            base, ident = clean(name)[:30], None
            cand, i = base, 0
            while cand in used:
                i += 1
                cand = base[:30 - len(str(i))] + str(i)
            used.add(cand)
            return cand.encode()
        stem, dot, ext = name.rpartition('.')
        if not dot:
            stem, ext = name, ''
        stem, ext = clean(stem), clean(ext)[:8]
        cand, i = stem[:max(1, 28 - len(ext))], 0
        while (cand + '.' + ext) in used:
            i += 1
            cand = stem[:max(1, 28 - len(ext) - len(str(i)))] + str(i)
        used.add(cand + '.' + ext)
        return (cand + '.' + ext + ';1').encode()

    # ------------------------------------------------------------------ system use (Rock Ridge)
    def _su(self, n, kind):
        def px(mode, links):
            return b'PX' + bytes((36, 1)) + b32(mode) + b32(links) + b32(0) + b32(0)

        def tf(ts):
            return b'TF' + bytes((12, 1, 0x02)) + rec_date(ts)
        links = (2 + sum(1 for c in n.children.values() if c.is_dir)) if n.is_dir else 1
        mode = (0o040000 | n.mode) if n.is_dir else (0o100000 | n.mode)
        su = b''
        if kind == 'root_dot':
            er_id, er_des = b'RRIP_1991A', b'THE ROCK RIDGE INTERCHANGE PROTOCOL PROVIDES SUPPORT FOR POSIX FILE SYSTEM SEMANTICS'
            su += b'SP' + bytes((7, 1, 0xBE, 0xEF, 0)) + b'ER' + bytes((8 + len(er_id) + len(er_des), 1, len(er_id), len(er_des), 0, 1)) + er_id + er_des
        su += px(mode, links) + tf(n.mtime)
        if kind == 'entry':
            nm = n.name.encode('utf-8')
            assert len(nm) <= 200, n.name
            su += b'NM' + bytes((5 + len(nm), 1, 0)) + nm
        return su

    def _record(self, ident, n, kind, lba=0, size=0, flags=0):
        su = self._su(n, kind)
        base = 33 + len(ident) + (1 if len(ident) % 2 == 0 else 0)
        if (base + len(su)) % 2:
            su += b'\0'
        ln = base + len(su)
        assert ln <= 255, (ident, ln)
        r = bytearray(ln)
        r[0] = ln
        r[2:10] = b32(lba)
        r[10:18] = b32(size)
        r[18:25] = rec_date(n.mtime)
        r[25] = flags
        r[28:32] = b16(1)
        r[32] = len(ident)
        r[33:33 + len(ident)] = ident
        r[base:] = su
        return bytes(r)

    # ------------------------------------------------------------------ layout + write
    def build(self, out_path, bios_boot=None, efi_boot=None, hybrid_mbr=True):
        dirs = []
        queue = [self.root]
        while queue:                                       # breadth first: also the path-table order
            d = queue.pop(0)
            d.dnum = len(dirs) + 1
            dirs.append(d)
            used_d, used_f = set(), set()
            for name in sorted(d.children):
                c = d.children[name]
                c.iso_id = self._iso_id(name, c.is_dir, used_d if c.is_dir else used_f)
            for c in sorted((c for c in d.children.values() if c.is_dir), key=lambda c: c.iso_id):
                queue.append(c)

        def sized_records(d):
            recs = [self._record(b'\0', d, 'root_dot' if d.parent is None else 'dot'),
                    self._record(b'\1', d.parent or d, 'dotdot')]
            for c in sorted(d.children.values(), key=lambda c: c.iso_id):
                recs.append(self._record(c.iso_id, c, 'entry'))
            return recs

        def pack(recs):
            out, off = [], 0
            for r in recs:
                if off % SECTOR + len(r) > SECTOR:
                    off = (off // SECTOR + 1) * SECTOR
                out.append((off, r))
                off += len(r)
            return out, ((off + SECTOR - 1) // SECTOR) * SECTOR

        for d in dirs:
            d.dir_bytes = pack(sized_records(d))[1]

        # path tables
        def pt_records(big):
            out = bytearray()
            for d in dirs:
                ident = d.iso_id if d.parent else b'\0'
                parent = d.parent.dnum if d.parent else 1
                rec = bytes((len(ident), 0)) + (struct.pack('>I', d.lba) if big else struct.pack('<I', d.lba)) + \
                    (struct.pack('>H', parent) if big else struct.pack('<H', parent)) + ident
                out += rec + (b'\0' if len(ident) % 2 else b'')
            return bytes(out)
        pt_size = len(pt_records(False))
        pt_sectors = (pt_size + SECTOR - 1) // SECTOR

        lba = 16 + 3                                       # PVD, boot record, terminator
        pt_l, pt_m = lba, lba + pt_sectors
        lba += 2 * pt_sectors
        for d in dirs:
            d.lba = lba
            lba += d.dir_bytes // SECTOR
        catalog_lba = lba
        lba += 1
        for n in self.order:
            n.lba = lba
            lba += (n.size + SECTOR - 1) // SECTOR
        total = lba

        # boot-info-table for the BIOS image (isolinux/GRUB convention: offset 8, 56 bytes)
        bios = self.find(bios_boot) if bios_boot else None
        efi = self.find(efi_boot) if efi_boot else None
        if bios:
            data = bytearray(bios.data if bios.data is not None else open(bios.src, 'rb').read())
            pad4 = bytes(data) + b'\0' * ((-len(data)) % 4)
            csum = sum(struct.unpack('<%dI' % ((len(pad4) - 64) // 4), pad4[64:])) & 0xFFFFFFFF
            data[8:64] = struct.pack('<IIII', 16, bios.lba, len(data), csum) + b'\0' * 40
            bios.data = bytes(data)

        # --- write
        with open(out_path, 'wb') as f:
            f.truncate(total * SECTOR)
            if hybrid_mbr and efi:
                f.seek(0)
                f.write(self._mbr(efi))
            pvd = bytearray(SECTOR)
            pvd[0:7] = b'\x01CD001\x01'
            pvd[8:40] = pad(self.sysid, 32)
            pvd[40:72] = pad(self.volid, 32)
            pvd[80:88] = b32(total)
            pvd[120:124] = b16(1)
            pvd[124:128] = b16(1)
            pvd[128:132] = b16(SECTOR)
            pvd[132:140] = b32(pt_size)
            pvd[140:144] = struct.pack('<I', pt_l)
            pvd[148:152] = struct.pack('>I', pt_m)
            rr = bytearray(34)                              # root record inside the PVD: exactly 34 bytes, no system use
            rr[0] = 34
            rr[2:10] = b32(self.root.lba)
            rr[10:18] = b32(self.root.dir_bytes)
            rr[18:25] = rec_date(self.root.mtime)
            rr[25] = 2
            rr[28:32] = b16(1)
            rr[32] = 1
            pvd[156:190] = rr
            pvd[190:318] = pad(self.volid, 128)
            pvd[318:446] = pad(self.publisher, 128)
            pvd[446:574] = pad(self.preparer, 128)
            pvd[574:702] = pad(self.app, 128)
            for off in (702, 739, 776):
                pvd[off:off + 37] = b' ' * 37
            vd = vol_date(self.mtime)
            pvd[813:830], pvd[830:847] = vd, vd
            pvd[847:864] = b'0' * 16 + b'\0'
            pvd[864:881] = vd
            pvd[881] = 1
            f.seek(16 * SECTOR)
            f.write(pvd)
            br = bytearray(SECTOR)
            br[0:7] = b'\x00CD001\x01'
            br[7:39] = b'EL TORITO SPECIFICATION'.ljust(32, b'\0')
            br[71:75] = struct.pack('<I', catalog_lba)
            f.seek(17 * SECTOR)
            f.write(br)
            term = bytearray(SECTOR)
            term[0:7] = b'\xffCD001\x01'
            f.seek(18 * SECTOR)
            f.write(term)
            f.seek(pt_l * SECTOR)
            f.write(pt_records(False))
            f.seek(pt_m * SECTOR)
            f.write(pt_records(True))
            for d in dirs:
                buf = bytearray(d.dir_bytes)
                recs = [self._record(b'\0', d, 'root_dot' if d.parent is None else 'dot', d.lba, d.dir_bytes, 2),
                        self._record(b'\1', d.parent or d, 'dotdot', (d.parent or d).lba, (d.parent or d).dir_bytes, 2)]
                for c in sorted(d.children.values(), key=lambda c: c.iso_id):
                    if c.is_dir:
                        recs.append(self._record(c.iso_id, c, 'entry', c.lba, c.dir_bytes, 2))
                    else:
                        recs.append(self._record(c.iso_id, c, 'entry', c.lba if c.size else 0, c.size, 0))
                placed, _ = pack(recs)
                for off, r in placed:
                    buf[off:off + len(r)] = r
                f.seek(d.lba * SECTOR)
                f.write(buf)
            f.seek(catalog_lba * SECTOR)
            f.write(self._catalog(bios, efi))
            for n in self.order:
                f.seek(n.lba * SECTOR)
                if n.data is not None:
                    f.write(n.data)
                else:
                    with open(n.src, 'rb') as s:
                        shutil.copyfileobj(s, f, 1 << 20)
        return dict(total_sectors=total, catalog_lba=catalog_lba, dirs=len(dirs))

    # ------------------------------------------------------------------ El Torito + MBR
    def _catalog(self, bios, efi):
        cat = bytearray(SECTOR)
        val = bytearray(32)
        val[0], val[1] = 1, 0                              # header id, platform: 80x86
        val[4:4 + 6] = b'ouatos'
        val[30], val[31] = 0x55, 0xAA
        s = sum(struct.unpack('<16H', bytes(val))) & 0xFFFF
        struct.pack_into('<H', val, 28, (-s) & 0xFFFF)
        cat[0:32] = val
        off = 32
        if bios:
            e = bytearray(32)
            e[0], e[1] = 0x88, 0                           # bootable, no emulation
            struct.pack_into('<HBBHI', e, 2, 0, 0, 0, 4, bios.lba)      # segment, sys type, -, sector count (4 x 512), LBA
            cat[off:off + 32] = e
            off += 32
        if efi:
            h = bytearray(32)
            h[0], h[1] = 0x91, 0xEF                        # final section header, platform: EFI
            struct.pack_into('<H', h, 2, 1)
            h[4:4 + 6] = b'UEFI'.ljust(6, b'\0')
            cat[off:off + 32] = h
            e = bytearray(32)
            e[0], e[1] = 0x88, 0
            struct.pack_into('<HBBHI', e, 2, 0, 0, 0, min(0xFFFF, (efi.size + 511) // 512), efi.lba)
            cat[off + 32:off + 64] = e
        return bytes(cat)

    def _mbr(self, efi):
        m = bytearray(512)
        m[440:444] = struct.pack('<I', 0x6F756174)         # disk signature
        ent = bytearray(16)
        ent[0] = 0x00
        ent[1:4] = b'\xFE\xFF\xFF'
        ent[4] = 0xEF                                      # EFI system partition
        ent[5:8] = b'\xFE\xFF\xFF'
        struct.pack_into('<II', ent, 8, efi.lba * 4, (efi.size + 511) // 512)
        m[446:462] = ent
        m[510:512] = b'\x55\xAA'
        return bytes(m)
