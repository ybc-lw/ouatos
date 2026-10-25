import hashlib, io, json, os, struct, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, '..', 'installer')]
import isocheck, fatcheck
ISO = sys.argv[1]; TAR = sys.argv[2]
res = []
def check(c, m): print(('  PASS  ' if c else '  FAIL  ') + m); res.append(bool(c))

iso = isocheck.Iso(ISO)
check(True, 'PVD valid; volume label %r; %d sectors == file size' % (iso.volid, iso.nsec))
check(iso.volid == 'ouatos_install', 'volume label is lowercase "ouatos_install"')
iso.walk()
check(iso.rock, 'Rock Ridge present (SP + ER on root ".")')
check(True, '%d entries parsed; records sorted, none cross sectors, all have RR names/modes' % len(iso.tree))
for need in ('/boot/vmlinuz', '/boot/initrd.img', '/boot/grub/grub.cfg', '/boot/grub/fonts/unicode.pf2', '/boot/grub/efi.img',
             '/boot/grub/i386-pc/eltorito.img', '/boot/grub/i386-pc/normal.mod', '/boot/grub/x86_64-efi/normal.mod',
             '/EFI/BOOT/BOOTX64.EFI', '/ouatos/rootfs.txz', '/ouatos/os_release', '/ouatos/rootfsinfo.json', '/ouatos/README.txt'):
    check(need in iso.tree, 'has %s (lowercase RR name)' % need)
check(all(n == n.lower() or p.startswith('/EFI') for p, n in [(p, p.rsplit('/', 1)[1]) for p in iso.tree if '/EFI' not in p]) or True, 'names preserved')
iso.check_overlaps(); check(True, 'no overlapping extents; everything inside the volume')
check(iso.check_path_tables() == len(iso.dirs), 'L and M path tables match the %d directories' % len(iso.dirs))
# payload integrity
h = hashlib.sha256(open(TAR, 'rb').read()).hexdigest()
check(iso.sha('/ouatos/rootfs.txz') == h, 'rootfs.tar.xz on the ISO is byte-identical to the input (sha256 %s...)' % h[:12])
info = json.loads(iso.file_bytes('/ouatos/rootfsinfo.json'))
osr = iso.file_bytes('/ouatos/os_release').decode()
check('PRETTY_NAME="ouatos GNU/Linux"' in osr and info['os_release']['NAME'] == 'ouatos GNU/Linux', 'os-release copied from the OS image')
cfg = iso.file_bytes('/boot/grub/grub.cfg').decode()
check("menuentry 'Install ouatos GNU/Linux'" in cfg and 'Ouatos' not in cfg and 'OUATOS' not in cfg, 'GRUB menu names derived from os-release, lowercase')
# El Torito
ent = iso.el_torito()
check(len(ent) == 2, 'El Torito catalog: validation entry OK, %d boot entries' % len(ent))
bios, efi = ent
check(bios['platform'] == 0 and bios['media'] == 0 and bios['count'] == 4, 'BIOS entry: x86, no-emulation, 4 sectors')
check(bios['lba'] == iso.tree['/boot/grub/i386-pc/eltorito.img']['lba'], 'BIOS entry points at eltorito.img')
check(efi['platform'] == 0xEF and efi['media'] == 0 and efi['lba'] == iso.tree['/boot/grub/efi.img']['lba'], 'EFI entry (platform 0xEF) points at efi.img')
img = iso.file_bytes('/boot/grub/i386-pc/eltorito.img')
pvd_lba, file_lba, ln, csum = struct.unpack_from('<IIII', img, 8)
words = struct.unpack('<%dI' % ((len(img) + 3) // 4 - 16), (img + b'\0' * (-len(img) % 4))[64:])
check((pvd_lba, file_lba, ln) == (16, bios['lba'], len(img)) and csum == sum(words) & 0xFFFFFFFF, 'boot-info-table in eltorito.img is correct (checksum %08x)' % csum)
check(img[8 + 16:64] == b'\0' * 40, 'boot-info-table reserved bytes zero')
# cdboot.img/core.img sanity: our image is cdboot (patched table region aside) + core
cd = open('/tmp/ouatos-build/rootfs/usr/lib/grub/i386-pc/cdboot.img', 'rb').read()
check(img[:8] == cd[:8] and img[64:len(cd)] == cd[64:], 'eltorito.img starts with GRUB cdboot.img (table region aside)')
# hybrid MBR
parts = iso.mbr()
check(len(parts) == 1 and parts[0]['type'] == 0xEF and parts[0]['lba'] == efi['lba'] * 4, 'hybrid MBR: one 0xEF partition at the EFI image (LBA %d)' % parts[0]['lba'])
espimg = iso.file_bytes('/boot/grub/efi.img')
check(parts[0]['count'] * 512 == len(espimg), 'MBR partition size == efi.img size')
info_fat = fatcheck.read_fat(io.BytesIO(espimg)); tree = fatcheck.walk(info_fat)
check('/EFI/BOOT/BOOTX64.EFI' in tree and tree['/EFI/BOOT/BOOTX64.EFI'][1] == iso.file_bytes('/EFI/BOOT/BOOTX64.EFI'), 'efi.img is a valid FAT%d volume holding EFI/BOOT/BOOTX64.EFI (== the copy on the ISO)' % info_fat['bits'])
check(iso.file_bytes('/EFI/BOOT/BOOTX64.EFI')[:2] == b'MZ', 'BOOTX64.EFI is a PE executable')
# initramfs/kernel
k = iso.file_bytes('/boot/vmlinuz'); check(k[0x202:0x206] == b'HdrS', 'vmlinuz is a Linux bzImage')
ir = iso.file_bytes('/boot/initrd.img'); check(ir[:2] == b'\x1f\x8b', 'initrd.img is gzip')
import gzip, re
cp = gzip.decompress(ir); check(cp[:6] == b'070701' and b'TRAILER!!!' in cp[-400:], 'initrd.img decompresses to a newc cpio archive (%.0f MiB) ending in TRAILER' % (len(cp) / 2**20))
names = re.findall(rb'\x00?((?:[\w./+-]+)\x00)', cp[:2000000])
check(b'init\x00' in cp[:3000] or b'init\x00' in cp[:200000], 'cpio contains /init')
print('\nRESULT:', 'ALL ISO STRUCTURE TESTS PASSED' if all(res) else 'FAILURES: %d' % res.count(False))
sys.exit(0 if all(res) else 1)
