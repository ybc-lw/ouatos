"""Independent ISO 9660 / Rock Ridge / El Torito reader + validator (shares no code with isowriter.py)."""
import hashlib, struct, sys, os

S = 2048
def le32(b, o): return struct.unpack_from('<I', b, o)[0]
def be32(b, o): return struct.unpack_from('>I', b, o)[0]
def both32(b, o):
    a, c = le32(b, o), be32(b, o + 4)
    assert a == c, 'both-endian mismatch at %d' % o
    return a

class IsoError(Exception): pass

def parse_su(su):
    """Yield (sig, version, payload) for each SUSP entry."""
    i, out = 0, []
    while i + 4 <= len(su):
        sig, ln, ver = su[i:i+2], su[i+2], su[i+3]
        if ln < 4 or i + ln > len(su): break
        out.append((sig, ver, su[i+4:i+ln])); i += ln
    return out

class Iso:
    def __init__(self, path):
        self.f = open(path, 'rb'); self.size = os.path.getsize(path)
        self.pvd = self.sec(16)
        assert self.pvd[0] == 1 and self.pvd[1:6] == b'CD001' and self.pvd[6] == 1, 'bad PVD'
        assert self.pvd[881] == 1
        self.nsec = both32(self.pvd, 80)
        assert self.nsec * S == self.size, 'volume size %d != file size %d' % (self.nsec * S, self.size)
        assert struct.unpack_from('<H', self.pvd, 128)[0] == S
        self.volid = self.pvd[40:72].decode().rstrip()
        self.pt_size = both32(self.pvd, 132)
        self.root_rec = self.pvd[156:190]
        assert self.root_rec[0] == 34 and self.root_rec[32] == 1 and self.root_rec[33] == 0, 'PVD root record must be 34 bytes'
        self.root_lba, self.root_len = both32(self.root_rec, 2), both32(self.root_rec, 10)
        t = self.sec(18); assert t[0] == 255 and t[1:6] == b'CD001', 'terminator'
        self.extents = []       # (lba, nsectors, what)
        self.tree = {}          # path -> dict(lba,size,isdir,mode,id)
        self.dirs = []          # (path, lba, size, parent_path)

    def sec(self, n, c=1):
        self.f.seek(n * S); return self.f.read(S * c)

    def read(self, lba, size):
        self.f.seek(lba * S); return self.f.read(size)

    def walk(self):
        self.rock = False
        self._dir('/', self.root_lba, self.root_len, '/', first=True)

    def _dir(self, path, lba, length, parent, first=False):
        raw = self.read(lba, length)
        self.dirs.append((path, lba, length, parent))
        self.extents.append((lba, length // S, 'dir ' + path))
        off, recs = 0, []
        while off < len(raw):
            ln = raw[off]
            if ln == 0:                                   # padding to next sector
                off = (off // S + 1) * S; continue
            if off // S != (off + ln - 1) // S: raise IsoError('record crosses sector boundary in ' + path)
            if ln % 2: raise IsoError('odd record length')
            recs.append(raw[off:off+ln]); off += ln
        if len(recs[0]) < 34 or recs[0][32] != 1 or recs[0][33] != 0: raise IsoError('first record is not "."')
        if recs[1][32] != 1 or recs[1][33] != 1: raise IsoError('second record is not ".."')
        if both32(recs[0], 2) != lba: raise IsoError('"." extent wrong in ' + path)
        if first:
            su0 = recs[0][34:]
            ents = parse_su(su0)
            if not ents or ents[0][0] != b'SP' or ents[0][2][:2] != b'\xbe\xef': raise IsoError('root "." lacks SUSP SP entry')
            if not any(e[0] == b'ER' for e in ents): raise IsoError('root "." lacks ER entry')
            self.rock = True
        ids = []
        for r in recs[2:]:
            idl = r[32]; ident = r[33:33+idl]
            base = 33 + idl + (1 if idl % 2 == 0 else 0)
            su = parse_su(r[base:])
            name = b''.join(p[1:] for sig, v, p in su if sig == b'NM').decode() if any(s == b'NM' for s, _, _ in su) else None
            px = next((p for sig, v, p in su if sig == b'PX'), None)
            mode = struct.unpack_from('<I', px, 0)[0] if px else None
            flags, elba, esz = r[25], both32(r, 2), both32(r, 10)
            if name is None: raise IsoError('no Rock Ridge NM for ' + ident.decode())
            ids.append(ident)
            full = (path.rstrip('/') + '/' + name)
            isdir = bool(flags & 2)
            if isdir != bool(mode & 0o040000 == 0o040000) and mode is not None and (mode & 0o170000) not in (0o040000, 0o100000):
                raise IsoError('mode/type mismatch for ' + full)
            if not isdir and not ident.endswith(b';1'): raise IsoError('file id without ;1: %r' % ident)
            if isdir and b'.' in ident: raise IsoError('dir id has dot')
            if not all(c in b'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.;' for c in ident): raise IsoError('invalid chars in id %r' % ident)
            self.tree[full] = dict(lba=elba, size=esz, isdir=isdir, mode=mode, id=ident)
            if isdir:
                self._dir(full, elba, esz, path)
            elif esz:
                self.extents.append((elba, (esz + S - 1) // S, 'file ' + full))
        if ids != sorted(ids): raise IsoError('directory records not sorted in ' + path)

    def check_path_tables(self):
        lba_l, lba_m = le32(self.pvd, 140), be32(self.pvd, 148)
        want = {}                                           # dir extent -> parent dir extent
        by_path = {d[0]: d[1] for d in self.dirs}
        for path, dl, _, parent in self.dirs:
            want[dl] = by_path[parent]
        tables = []
        for big, lba in ((False, lba_l), (True, lba_m)):
            raw = self.read(lba, self.pt_size); off, recs = 0, []
            while off < len(raw):
                ln = raw[off]; el = struct.unpack_from('>I' if big else '<I', raw, off + 2)[0]
                par = struct.unpack_from('>H' if big else '<H', raw, off + 6)[0]
                ident = raw[off+8:off+8+ln]; recs.append((ident, el, par)); off += 8 + ln + (ln % 2)
            if len(recs) != len(self.dirs): raise IsoError('path table has %d entries, %d dirs' % (len(recs), len(self.dirs)))
            if recs[0][0] != b'\0' or recs[0][2] != 1 or recs[0][1] != self.root_lba: raise IsoError('path table root entry wrong')
            for i, (ident, el, par) in enumerate(recs[1:], 2):
                if not (1 <= par < i): raise IsoError('parent number out of order')
                if want.get(el) != recs[par - 1][1]: raise IsoError('path table parent/extent relation wrong for entry %d' % i)
            pars = [r[2] for r in recs[1:]]
            if pars != sorted(pars): raise IsoError('path table not ordered by parent number')
            for par in set(pars):
                ids = [r[0] for r in recs[1:] if r[2] == par]
                if ids != sorted(ids): raise IsoError('siblings not sorted in path table')
            tables.append(recs)
        if tables[0] != tables[1]: raise IsoError('L and M path tables disagree')
        return len(self.dirs)

    def check_overlaps(self):
        ex = sorted(e for e in self.extents if e[1])
        ex = [(16, 3, 'volume descriptors')] + ex
        for a, b in zip(ex, ex[1:]):
            if a[0] + a[1] > b[0]: raise IsoError('overlap: %s and %s' % (a[2], b[2]))
        if ex[-1][0] + ex[-1][1] > self.nsec: raise IsoError('extent beyond volume end')

    def file_bytes(self, path):
        t = self.tree[path]; return self.read(t['lba'], t['size'])

    def sha(self, path):
        t = self.tree[path]; h = hashlib.sha256(); self.f.seek(t['lba'] * S); left = t['size']
        while left:
            b = self.f.read(min(left, 1 << 20)); h.update(b); left -= len(b)
        return h.hexdigest()

    def el_torito(self):
        br = self.sec(17)
        assert br[0] == 0 and br[1:6] == b'CD001' and br[7:30] == b'EL TORITO SPECIFICATION', 'no El Torito boot record'
        cat = self.sec(le32(br, 71))
        words = struct.unpack_from('<16H', cat, 0)
        assert cat[0] == 1 and sum(words) & 0xFFFF == 0 and cat[30:32] == b'\x55\xAA', 'validation entry checksum'
        entries, off, plat = [], 32, cat[1]
        while off < 2048 and cat[off] in (0x88, 0x00, 0x90, 0x91):
            e = cat[off:off+32]
            if e[0] in (0x90, 0x91):
                plat = e[1]
            elif e[0] == 0x88:
                entries.append(dict(platform=plat, media=e[1], segment=struct.unpack_from('<H', e, 2)[0],
                                    count=struct.unpack_from('<H', e, 6)[0], lba=le32(e, 8)))
            else:
                break
            off += 32
        return entries

    def mbr(self):
        m = self.sec(0)[:512]
        assert m[510:512] == b'\x55\xAA'
        parts = []
        for i in range(4):
            e = m[446+16*i:462+16*i]
            if e[4]: parts.append(dict(type=e[4], lba=le32(e, 8), count=le32(e, 12)))
        return parts
