import os, sys, io, hashlib, tempfile
sys.path.insert(0,'installer'); sys.path.insert(0,'tests')
import fatfs, fatcheck

def check(info, expect):
    tree = fatcheck.walk(info)
    for path, data in expect.items():
        e, got = tree[path]
        assert got == data, path
    return tree

# --- FAT32 on a 512 MiB sparse file, both 512 and 4096 byte sectors
for lbs in (512, 4096):
    with tempfile.NamedTemporaryFile(delete=False) as t:
        t.truncate(512 << 20); p = t.name
    f = open(p, 'r+b')
    # put it at a non-zero offset like a real partition
    off = 1 << 20
    f.truncate((512 << 20) + off)
    fs = fatfs.FatFS(f, off, 512 << 20, lbs, label='EFI', volid=0xABCD1234, fat_bits=32, hidden_sectors=off // lbs)
    a, b = os.urandom(745472), os.urandom(123457)
    fs.write_file('EFI/BOOT/BOOTX64.EFI', a)
    fs.write_file('EFI/ouatos/grubx64.efi', b)      # lowercase via NT flags
    fs.close(); f.close()
    f = open(p, 'rb'); info = fatcheck.read_fat(f, off)
    tree = check(info, {'/EFI/BOOT/BOOTX64.EFI': a, '/EFI/ouatos/grubx64.efi': b})
    print("FAT32 lbs=%d: bits=%d spc=%d clusters=%d volid=%08X label=%r free(fsinfo)=%d files=%s"
          % (lbs, info['bits'], info['spc'], info['clusters'], info['volid'], info['label'], info['fsinfo_free'],
             [k for k, v in tree.items() if v[1] is not None]))
    assert info['bits'] == 32 and info['volid'] == 0xABCD1234 and info['hidden'] == off // lbs
    f.close(); os.unlink(p)

# --- FAT16 8 MiB ESP image for the ISO
img = fatfs.make_fat_image({'EFI/BOOT/BOOTX64.EFI': os.urandom(3_000_000), 'EFI/BOOT/GRUB.CFG': b'x'*100}, 8 << 20)
info = fatcheck.read_fat(io.BytesIO(img)); tree = fatcheck.walk(info)
print("FAT16 image: bits=%d spc=%d clusters=%d files=%s" % (info['bits'], info['spc'], info['clusters'], [k for k,v in tree.items() if v[1] is not None]))
assert info['bits'] == 16
# too-small partitions rejected
try: fatfs.FatFS(io.BytesIO(bytes(10<<20)),0,10<<20,512,fat_bits=32); print("BUG: 10MiB FAT32 accepted")
except ValueError as e: print("10 MiB FAT32 correctly rejected:", e)
print("ALL FAT TESTS PASSED")
