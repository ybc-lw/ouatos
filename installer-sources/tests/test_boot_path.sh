#!/bin/bash
# Regression test for the live boot path (run as root after build_iso.py).  Usage: test_boot_path.sh ISO [WORKDIR]
# 1. udev from the live tree must start AND be able to load libkmod (it dlopen()s it, ldd cannot see that)
# 2. init's modalias sweep must resolve drivers
# 3. /init must find the medium with the REAL blkid on the REAL ISO (only 'mount -t iso9660' is stubbed:
#    the build sandbox kernel has no isofs) and launch the installer
ISO=${1:?iso}; W=${2:-/tmp/ouatos-build}; L=$W/live; R=$W/rootfs; X=$R/tmp/initx; fail=0
pass() { echo "  PASS  $1"; }; bad() { echo "  FAIL  $1"; fail=1; }
umount -R $X/fakemedia 2>/dev/null; rm -rf $X; mkdir -p $X
zcat $W/iso/boot/initrd.img | chroot $R /usr/bin/cpio -idm --quiet -D /tmp/initx
sh -n $X/init && pass "/init passes sh -n" || bad "/init syntax"
cat > $X/tmp/t.sh <<'IN'
#!/bin/sh
export PATH=/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc; mount -t sysfs sysfs /sys; mount -t devtmpfs devtmpfs /dev; mount -t tmpfs tmpfs /run; mkdir -p /run/udev
/usr/lib/systemd/systemd-udevd --debug --resolve-names=never > /tmp/u.out 2>&1 &
sleep 1; udevadm trigger --type=subsystems --action=add; udevadm trigger --type=devices --action=add; udevadm settle --timeout=8; kill $! 2>/dev/null
echo "KMODBAD=$(grep -c 'is not installed\|Failed to initialize libkmod' /tmp/u.out)"
echo "UDEVLOADS=$(grep -c 'Running built-in command "kmod load"' /tmp/u.out)"
echo "SWEEP=$(find /sys/devices -name modalias -type f | xargs cat | sort -u | xargs modprobe -b -a -n -v 2>&1 | grep -c '^insmod')"
IN
chmod +x $X/tmp/t.sh; SK=$(uname -r); ln -sfn $(ls $X/usr/lib/modules | head -1) $X/usr/lib/modules/$SK   # test only: sandbox kernel version differs
OUT=$(timeout -s KILL 40 unshare --kill-child=SIGKILL -m -p -f --mount-proc chroot $X /tmp/t.sh 2>&1); rm -f $X/usr/lib/modules/$SK
echo "$OUT" | grep -q "KMODBAD=0" && pass "udev loads libkmod (can load drivers)" || bad "udev cannot load libkmod: $OUT"
[ "$(echo "$OUT" | sed -n 's/UDEVLOADS=//p')" -gt 0 ] && pass "udev ran 'kmod load' for devices" || bad "udev never tried to load a module"
[ "$(echo "$OUT" | sed -n 's/SWEEP=//p')" -gt 0 ] && pass "modalias sweep resolves driver modules" || bad "modalias sweep resolved nothing"
LO=$(losetup -f --show $ISO)
PYTHONUNBUFFERED=1 timeout -s KILL 60 unshare --kill-child=SIGKILL -m -p -f --mount-proc chroot $X /init </dev/null >/tmp/init3.log 2>&1
losetup -d $LO; rm -rf $X
grep -q "Using installation medium: device $LO" /tmp/init3.log && pass "/init found the ISO ($LO) by scanning devices and reading it raw (no mount, no blkid)" || bad "/init did not find the medium: $(head -c 600 /tmp/init3.log)"
grep -q "Install ouatos GNU/Linux (text mode)" /tmp/init3.log && pass "/init then launched the installer, which read os-release from the medium" || { bad "installer not launched"; echo "----- tail of /tmp/init3.log -----"; tail -n 200 /tmp/init3.log; echo "----- end -----"; }
exit $fail
