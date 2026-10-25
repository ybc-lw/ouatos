"""Reads the OS image once at build time and records what the installer needs to know."""
import os, re, sys, tarfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'installer'))
from engine import parse_os_release


def make_rootfs_info(tar_path):
    users, groups, total, osr, kernel = [], [], 0, None, None
    lfs = []                                    # Git LFS pointer stubs: a sign the image was built without `git lfs pull`
    with tarfile.open(tar_path, 'r|xz') as tf:
        for m in tf:
            n = m.name[2:] if m.name.startswith('./') else m.name
            data = None
            if m.isreg():
                total += m.size
                if m.size < 400 or n in ('etc/passwd', 'etc/group'):
                    data = tf.extractfile(m).read()          # a streaming tar can only be read once: keep the bytes
                if data is not None and 0 < m.size < 400 and data.startswith(b'version https://git-lfs.github.com/spec/v1'):
                    mm = re.search(rb'size (\d+)', data)
                    lfs.append(dict(path='/' + n, expected_bytes=int(mm.group(1)) if mm else None))
            if n == 'usr/lib/os-release':
                osr = parse_os_release(data.decode())
            elif n == 'etc/passwd':
                users = [l.split(':')[0] for l in data.decode().splitlines() if l.strip()]
            elif n == 'etc/group':
                groups = [l.split(':')[0] for l in data.decode().splitlines() if l.strip()]
            elif n.startswith('boot/vmlinuz-'):
                kernel = n[len('boot/vmlinuz-'):]
    if not osr:
        raise SystemExit('rootfs has no /usr/lib/os-release')
    return dict(os_release=osr, system_users=users, system_groups=groups,
                installed_bytes=total, kernel=kernel, lfs_pointers=lfs, tar_bytes=os.path.getsize(tar_path))

if __name__ == '__main__':
    import json
    print(json.dumps(make_rootfs_info(sys.argv[1]), indent=1))
