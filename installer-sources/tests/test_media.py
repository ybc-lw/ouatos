"""Raw ISO access: find the medium by label, locate + hash the payload, survive a missing Rock Ridge."""
import hashlib, os, shutil, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, '..', 'installer'), HERE]
import media, isocheck, engine
ISO, TAR = sys.argv[1], sys.argv[2]
res = []
def check(c, m): print(('  PASS  ' if c else '  FAIL  ') + m); res.append(bool(c))
def sha(f, n):
    h = hashlib.sha256(); left = n
    while left:
        b = f.read(min(left, 1 << 20)); h.update(b); left -= len(b)
    return h.hexdigest()
want = hashlib.sha256(open(TAR, 'rb').read()).hexdigest()

def run(path, tag):
    check(media.probe_label(path) == 'ouatos_install', '[%s] volume label found by raw probe' % tag)
    m = media.RawMedia(path)
    check(m.exists() and m.payload_size() == os.path.getsize(TAR), '[%s] payload located, %d bytes' % (tag, m.payload_size()))
    with m.open_payload() as f:
        first = f.read(6); check(first == b'\xfd7zXZ\x00', '[%s] payload starts with the xz magic' % tag)
        f.read(1000); check(f.tell() == 1006, '[%s] ExtentReader.tell() is relative' % tag)
    with m.open_payload() as f:
        check(sha(f, m.payload_size()) == want, '[%s] payload read raw is byte-identical to the input (sha256 %s...)' % (tag, want[:12]))
    info = engine.load_media_info(m)
    check(engine.system_name(info['os_release']) == 'ouatos GNU/Linux' and info['installed_bytes'] > 300 << 20, '[%s] os-release + image facts read from the medium: %r' % (tag, engine.system_name(info['os_release'])))
    check(m.read_small(media.OSREL_NAMES).startswith(b'PRETTY_NAME="ouatos GNU/Linux"'), '[%s] os_release content' % tag)

run(ISO, 'real ISO')
# Same ISO with every Rock Ridge NM entry destroyed: only plain ISO 9660 names remain (what the kernel
# shows when Rock Ridge is not honoured).  The reader must still find the payload.
tmp = '/tmp/iso_norr.iso'; shutil.copyfile(ISO, tmp)
iso = isocheck.Iso(tmp); iso.walk()
patched = 0
with open(tmp, 'r+b') as f:
    for path, lba, length, parent in iso.dirs:
        f.seek(lba * 2048); raw = bytearray(f.read(length)); i = 0
        while True:
            i = raw.find(b'NM', i)
            if i < 0: break
            if raw[i + 3] == 1 and raw[i + 4] == 0 and 5 < raw[i + 2] < 120: raw[i:i + 2] = b'XX'; patched += 1
            i += 2
        f.seek(lba * 2048); f.write(raw)
check(patched > 100, 'destroyed %d Rock Ridge NM entries in a copy of the ISO' % patched)
try:
    isocheck.Iso(tmp).walk(); check(False, 'checker should have rejected the RR-less copy')
except isocheck.IsoError:
    check(True, 'sanity: my strict checker confirms Rock Ridge names are gone in the copy')
run(tmp, 'ISO without Rock Ridge')
os.unlink(tmp)
print('\nRESULT:', 'ALL MEDIA TESTS PASSED' if all(res) else 'FAILURES')
sys.exit(0 if all(res) else 1)
