"""Independent FAT reader/validator (spec-driven, shares no code with fatfs.py)."""
import struct


class FatError(Exception):
    pass


def read_fat(f, off=0):
    f.seek(off)
    bs = f.read(512)
    if bs[510:512] != b'\x55\xAA':
        raise FatError('missing 55AA boot signature')
    (bps, spc, rsvd, nfats, rootent, tot16, media, fatsz16, _spt, _heads, hidden, tot32) = \
        struct.unpack_from('<HBHBHHBHHHII', bs, 11)
    if bps not in (512, 1024, 2048, 4096):
        raise FatError('bad bytes/sector %d' % bps)
    if spc == 0 or spc & (spc - 1):
        raise FatError('bad sectors/cluster %d' % spc)
    if nfats != 2 or media != 0xF8:
        raise FatError('nfats/media unexpected')
    fatsz = fatsz16 or struct.unpack_from('<I', bs, 36)[0]
    total = tot16 or tot32
    rootsecs = (rootent * 32 + bps - 1) // bps
    data_start = rsvd + nfats * fatsz + rootsecs
    clusters = (total - data_start) // spc
    bits = 12 if clusters < 4085 else 16 if clusters < 65525 else 32
    info = dict(bps=bps, spc=spc, rsvd=rsvd, fatsz=fatsz, total=total, clusters=clusters, bits=bits,
                hidden=hidden, data_start=data_start, rootent=rootent)
    if bits == 32:
        if fatsz16 != 0 or rootent != 0:
            raise FatError('FAT32 must have fatsz16==0 and rootent==0')
        info['rootclus'], info['fsinfo'], info['bkboot'] = \
            struct.unpack_from('<I', bs, 44)[0], *struct.unpack_from('<HH', bs, 48)
        if bs[82:90] != b'FAT32   ':
            raise FatError('fs type string')
        info['volid'] = struct.unpack_from('<I', bs, 67)[0]
        info['label'] = bs[71:82].decode().rstrip()
        # FSInfo + backup
        fsi = _rd(f, off, info['fsinfo'] * bps, bps)
        if struct.unpack_from('<I', fsi, 0)[0] != 0x41615252 or struct.unpack_from('<I', fsi, 484)[0] != 0x61417272 \
                or struct.unpack_from('<I', fsi, 508)[0] != 0xAA550000:
            raise FatError('FSInfo signatures wrong')
        info['fsinfo_free'] = struct.unpack_from('<I', fsi, 488)[0]
        if _rd(f, off, info['bkboot'] * bps, bps) != _rd(f, off, 0, bps):
            raise FatError('backup boot sector differs')
    else:
        info['label'] = bs[43:54].decode().rstrip()
    # FAT copies
    fats = [_rd(f, off, (rsvd + i * fatsz) * bps, fatsz * bps) for i in range(nfats)]
    if fats[0] != fats[1]:
        raise FatError('FAT copies differ')
    info['_fat'] = fats[0]
    info['_f'], info['_off'] = f, off
    return info


def _rd(f, off, pos, n):
    f.seek(off + pos)
    return f.read(n)


def fat_entry(info, n):
    fat = info['_fat']
    b = info['bits']
    if b == 32:
        return struct.unpack_from('<I', fat, n * 4)[0] & 0x0FFFFFFF
    if b == 16:
        return struct.unpack_from('<H', fat, n * 2)[0]
    raise FatError('FAT12 unsupported in checker')


def chain(info, start):
    out, c, seen = [], start, set()
    eoc = 0x0FFFFFF8 if info['bits'] == 32 else 0xFFF8
    while True:
        if c < 2 or c > info['clusters'] + 1:
            raise FatError('cluster %d out of range' % c)
        if c in seen:
            raise FatError('cluster loop')
        seen.add(c)
        out.append(c)
        n = fat_entry(info, c)
        if n >= eoc:
            return out
        c = n


def read_clusters(info, cl):
    cb = info['spc'] * info['bps']
    out = bytearray()
    for c in cl:
        out += _rd(info['_f'], info['_off'], (info['data_start'] + (c - 2) * info['spc']) * info['bps'], cb)
    return bytes(out)


def read_dir(info, cluster):
    if cluster == 0 and info['bits'] != 32:
        raw = _rd(info['_f'], info['_off'], (info['rsvd'] + 2 * info['fatsz']) * info['bps'], info['rootent'] * 32)
    else:
        raw = read_clusters(info, chain(info, cluster))
    ents = []
    for i in range(0, len(raw), 32):
        e = raw[i:i + 32]
        if e[0] == 0:
            break
        if e[0] == 0xE5 or e[11] == 0x0F:
            continue
        name, attr, nt = e[:11], e[11], e[12]
        hi, lo, size = struct.unpack_from('<H', e, 20)[0], struct.unpack_from('<H', e, 26)[0], struct.unpack_from('<I', e, 28)[0]
        base, ext = name[:8].decode().rstrip(), name[8:].decode().rstrip()
        if nt & 0x08:
            base = base.lower()
        if nt & 0x10:
            ext = ext.lower()
        ents.append(dict(name=base + ('.' + ext if ext else ''), attr=attr, cluster=(hi << 16) | lo, size=size))
    return ents


def walk(info):
    """Yield (path, entry, data|None) for everything reachable; validates structure."""
    root = info.get('rootclus', 0)
    used = set()
    out = {}

    def rec(path, cl):
        for e in read_dir(info, cl):
            if e['attr'] & 0x08:
                continue
            if e['name'] in ('.', '..'):
                continue
            p = path + '/' + e['name']
            if e['attr'] & 0x10:
                ch = chain(info, e['cluster'])
                if used & set(ch):
                    raise FatError('cross-linked dir ' + p)
                used.update(ch)
                # verify . and ..
                ents = read_dir(info, e['cluster'])
                if ents[0]['name'] != '.' or ents[0]['cluster'] != e['cluster']:
                    raise FatError("bad '.' in " + p)
                expect_parent = cl if cl != root else 0
                if ents[1]['name'] != '..' or ents[1]['cluster'] != expect_parent:
                    raise FatError("bad '..' in %s (%d != %d)" % (p, ents[1]['cluster'], expect_parent))
                out[p] = (e, None)
                rec(p, e['cluster'])
            else:
                if e['size']:
                    ch = chain(info, e['cluster'])
                    if used & set(ch):
                        raise FatError('cross-linked file ' + p)
                    used.update(ch)
                    data = read_clusters(info, ch)[:e['size']]
                    if len(ch) * info['spc'] * info['bps'] < e['size']:
                        raise FatError('chain too short ' + p)
                else:
                    data = b''
                out[p] = (e, data)
    if info['bits'] == 32:
        used.update(chain(info, root))
    rec('', root)
    # every allocated FAT entry must belong to something we found
    allocated = {c for c in range(2, info['clusters'] + 2) if fat_entry(info, c) != 0}
    if allocated != used:
        raise FatError('FAT has %d allocated clusters, reachable %d' % (len(allocated), len(used)))
    return out
