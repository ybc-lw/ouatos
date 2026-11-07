"""Independent GPT validator."""
import struct, zlib, uuid

def read_gpt(f, lbs=512):
    f.seek(0, 2); total = f.tell() // lbs
    def rd(lba, n=1): f.seek(lba * lbs); return f.read(lbs * n)
    mbr = rd(0)
    assert mbr[510:512] == b'\x55\xAA', 'no MBR signature'
    assert mbr[446 + 4] == 0xEE, 'protective MBR type != EE'
    assert struct.unpack_from('<I', mbr, 446 + 8)[0] == 1
    res = {}
    for name, lba in (('primary', 1), ('backup', total - 1)):
        h = rd(lba)
        assert h[:8] == b'EFI PART', name + ' signature'
        hs = struct.unpack_from('<I', h, 12)[0]; assert hs == 92
        crc = struct.unpack_from('<I', h, 16)[0]
        hh = bytearray(h[:hs]); hh[16:20] = b'\0\0\0\0'
        assert zlib.crc32(bytes(hh)) & 0xFFFFFFFF == crc, name + ' header CRC'
        my, alt, fu, lu = struct.unpack_from('<QQQQ', h, 24)
        assert my == lba and alt == (total - 1 if lba == 1 else 1), name + ' LBA fields'
        eLBA, n, sz, ecrc = struct.unpack_from('<QIII', h, 72)
        assert n == 128 and sz == 128
        ents = rd(eLBA, (n * sz + lbs - 1) // lbs)[:n * sz]
        assert zlib.crc32(ents) & 0xFFFFFFFF == ecrc, name + ' entries CRC'
        res[name] = dict(first_usable=fu, last_usable=lu, entries_lba=eLBA, guid=str(uuid.UUID(bytes_le=h[56:72])), ents=ents)
    assert res['primary']['ents'] == res['backup']['ents'], 'primary/backup entries differ'
    assert res['backup']['entries_lba'] == total - 1 - (128 * 128 + lbs - 1) // lbs
    parts = []
    e = res['primary']['ents']
    for i in range(128):
        r = e[i * 128:(i + 1) * 128]
        if r[:16] == b'\0' * 16: continue
        t = str(uuid.UUID(bytes_le=r[:16])).upper(); first, last = struct.unpack_from('<QQ', r, 32)
        name = r[56:128].decode('utf-16le').rstrip('\0')
        assert res['primary']['first_usable'] <= first <= last <= res['primary']['last_usable'], 'partition outside usable area'
        parts.append(dict(n=i + 1, type=t, first=first, last=last, name=name))
    for a, b in zip(parts, parts[1:]):
        assert a['last'] < b['first'], 'overlap'
    for p in parts:
        assert (p['first'] * lbs) % (1 << 20) == 0, 'partition %d not 1MiB aligned' % p['n']
    return parts
