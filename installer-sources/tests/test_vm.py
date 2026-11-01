#!/usr/bin/env python3
"""Install the ouatos ISO inside a QEMU VM, then boot the installed disk.

usage: test_vm.py ISO [--firmware bios|uefi] [--workdir DIR] [--build-dir DIR]

Phase 1  QEMU boots the kernel + initramfs that build_iso.py put on the ISO
         (BUILD_DIR/iso/boot/{vmlinuz,initrd.img}) with
         `ouatos.text=1 console=ttyS0`, with the ISO attached as CD-ROM and an
         empty disk.  The installer's text frontend is driven over the serial
         console until it reports success.
         (The default GRUB entry starts the framebuffer GUI, which nothing can
         drive over a serial line; the live /init also prints only to tty1.)
Phase 2  The installed disk is patched so its GRUB entries log to ttyS0
         (CI only; the installer itself is not changed), then QEMU boots from
         that disk alone (BIOS or UEFI firmware, real GRUB) and we wait for a
         login prompt on the serial console.

Needs root (loop-mounts the installed disk), qemu-system-x86, ovmf, Pillow.
Serial logs land in WORKDIR/{install,boot}.log; on failure the VM screen is
saved to WORKDIR/{install,boot}.png.
"""
import argparse
import glob
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

USERNAME, USER_PW, ROOT_PW = "tester", "tester-pw-1", "root-pw-1"
CONSOLE_ARGS = "console=tty0 console=ttyS0,115200n8"   # last console= becomes /dev/console
BOOT_OK = r"(login:)|(Kernel panic[^\n]*)|(VFS: Unable to mount root[^\n]*)"
BOOT_TIMEOUT = 300

OVMF_CODE = "/usr/share/OVMF/OVMF_CODE_4M.fd"
OVMF_VARS = "/usr/share/OVMF/OVMF_VARS_4M.fd"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")


class Console:
    """QEMU process with the guest's serial port on stdin/stdout."""

    def __init__(self, cmd, log_path, mon=None):
        self.mon = mon
        self.log = open(log_path, "wb")
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT)
        self.q = queue.Queue()
        self.buf = ""
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        fd = self.p.stdout.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                self.q.put(None)
                return
            self.log.write(chunk)
            self.log.flush()
            self.q.put(ANSI.sub("", chunk.decode("utf-8", "replace")))

    def expect(self, pattern, timeout):
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while True:
            m = rx.search(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                return m
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"timeout ({timeout:.0f}s) waiting for /{pattern}/")
            try:
                chunk = self.q.get(timeout=left)
            except queue.Empty:
                continue
            if chunk is None:
                raise EOFError(f"QEMU exited while waiting for /{pattern}/")
            self.buf += chunk

    def send(self, text):
        self.p.stdin.write(text.encode())
        self.p.stdin.flush()

    def screenshot(self, out_png):
        """Dump the VM screen to a PNG (debugging aid)."""
        if not self.mon or self.p.poll() is not None:
            return
        ppm = out_png[:-4] + ".ppm"
        try:
            import socket
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(5)
            s.connect(self.mon)
            s.sendall(f"screendump {ppm}\n".encode())
            time.sleep(1.5)
            s.close()
            from PIL import Image
            Image.open(ppm).save(out_png)
            os.remove(ppm)
            print(f"screenshot saved: {out_png}")
        except Exception as e:
            print(f"screenshot failed: {e}")

    def stop(self, grace=0):
        if grace and self.p.poll() is None:
            try:
                self.p.wait(grace)
            except subprocess.TimeoutExpired:
                pass
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        self.log.close()


def qemu_cmd(firmware, disk, iso, vars_path, kvm, mon, direct=None):
    cmd = ["qemu-system-x86_64", "-machine", "q35", "-m", "2048", "-smp", "2",
           "-display", "none", "-serial", "stdio",
           "-monitor", f"unix:{mon},server,nowait",
           "-no-reboot", "-nic", "user,model=virtio-net-pci"]
    cmd += ["-enable-kvm", "-cpu", "host"] if kvm else ["-cpu", "max"]
    if firmware == "uefi":
        cmd += ["-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
                "-drive", f"if=pflash,format=raw,file={vars_path}"]
    cmd += ["-drive", f"file={disk},format=raw,if=ide,index=0"]
    if iso:
        cmd += ["-drive", f"file={iso},media=cdrom,format=raw,if=ide,index=1"]
    if direct:
        kernel, initrd, append = direct
        cmd += ["-kernel", kernel, "-initrd", initrd, "-append", append]
    return cmd


def drive_install(con, scale=1):
    """Answer the installer's text frontend (installer/installer.py: TextFrontend)."""
    m = con.expect(r"(\d+)\) [^\n]*?/dev/((?:sd|vd|nvme)[a-z0-9]+)", 300 * scale)
    print(f"installer is up; target disk /dev/{m.group(2)} (menu entry {m.group(1)})")
    con.expect(r"Install on which disk\? \(number\): ", 30)
    con.send(m.group(1) + "\n")
    for prompt, reply in [
        (r"Computer name \[[^\]\n]*\]: ", "\n"),
        (r"Username: ", USERNAME + "\n"),
        (r"Root password: ", ROOT_PW + "\n"),
        (r"Confirm root password: ", ROOT_PW + "\n"),
        (r"Your name \(optional\): ", "\n"),
        (r"Password: ", USER_PW + "\n"),
        (r"Confirm password: ", USER_PW + "\n"),
        (r"Type YES to continue: ", "YES\n"),
    ]:
        con.expect(prompt, 60 * scale)
        con.send(reply)
    m = con.expect(r"(is installed\. Remove the installation medium)|(Installation failed:[^\n]*)",
                   900 * scale)
    if m.group(2):
        raise RuntimeError(m.group(2))


def patch_installed_disk(disk):
    """Make the installed GRUB entries log to the serial port (CI only)."""
    loop = subprocess.check_output(["losetup", "-P", "--find", "--show", disk], text=True).strip()
    mnt = tempfile.mkdtemp(prefix="vmroot-")
    mounted = False
    try:
        subprocess.run(["udevadm", "settle", "--timeout=10"], check=False)
        root = None
        for _ in range(20):
            for part in sorted(glob.glob(loop + "p*")):
                fs = subprocess.run(["blkid", "-o", "value", "-s", "TYPE", part],
                                    capture_output=True, text=True).stdout.strip()
                if fs == "ext4":
                    root = part
                    break
            if root:
                break
            time.sleep(0.5)
        if not root:
            raise RuntimeError(f"no ext4 root partition found on {loop}")
        subprocess.check_call(["mount", root, mnt])
        mounted = True
        cfg_path = os.path.join(mnt, "boot/grub/grub.cfg")
        text = open(cfg_path).read()
        n = [0]

        def fix(m):
            n[0] += 1
            return re.sub(r"\s+quiet\b", "", m.group(1)).rstrip() + " " + CONSOLE_ARGS

        new = re.sub(r"^(\s*linux(?:efi)?\s.*)$", fix, text, flags=re.M)
        if not n[0]:
            raise RuntimeError("no linux line found in the installed grub.cfg")
        open(cfg_path, "w").write(new)
        print(f"patched {n[0]} linux line(s) in the installed grub.cfg")
    finally:
        if mounted:
            subprocess.run(["umount", mnt], check=False)
        subprocess.run(["losetup", "-d", loop], check=False)
        shutil.rmtree(mnt, ignore_errors=True)


def tail(path, n=40):
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iso")
    ap.add_argument("--firmware", choices=["bios", "uefi"], default="bios")
    ap.add_argument("--workdir", default="/tmp/vmtest")
    ap.add_argument("--build-dir", default="/tmp/ouatos-build",
                    help="build_iso.py work dir (holds iso/boot/vmlinuz and initrd.img)")
    ap.add_argument("--disk-size-gb", type=int, default=8)
    args = ap.parse_args()

    kernel = os.path.join(args.build_dir, "iso/boot/vmlinuz")
    initrd = os.path.join(args.build_dir, "iso/boot/initrd.img")
    for f in (kernel, initrd):
        if not os.path.exists(f):
            print(f"missing {f} (run build_iso.py first, or pass --build-dir)")
            return 1

    os.makedirs(args.workdir, exist_ok=True)
    disk = os.path.join(args.workdir, "disk.img")
    vars_path = os.path.join(args.workdir, "OVMF_VARS.fd")
    mon = os.path.join(args.workdir, "mon.sock")
    install_log = os.path.join(args.workdir, "install.log")
    boot_log = os.path.join(args.workdir, "boot.log")

    with open(disk, "wb") as f:
        f.truncate(args.disk_size_gb << 30)
    if args.firmware == "uefi":
        shutil.copy(OVMF_VARS, vars_path)

    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    scale = 1 if kvm else 5
    print(f"[{args.firmware}] KVM {'on' if kvm else 'OFF (TCG, timeouts x5)'}")
    try:
        return run(args, disk, vars_path, mon, install_log, boot_log, kernel, initrd, kvm, scale)
    finally:
        try:
            os.remove(disk)
        except OSError:
            pass


def run(args, disk, vars_path, mon, install_log, boot_log, kernel, initrd, kvm, scale):
    fw = args.firmware

    # ---- phase 1: install (text frontend over serial)
    print(f"[{fw}] phase 1: install")
    append = f"quiet loglevel=3 ouatos.text=1 {CONSOLE_ARGS}"
    if os.path.exists(mon):
        os.unlink(mon)
    con = Console(qemu_cmd(fw, disk, args.iso, vars_path, kvm, mon, (kernel, initrd, append)),
                  install_log, mon)
    try:
        drive_install(con, scale)
    except (TimeoutError, EOFError, RuntimeError) as e:
        print(f"FAIL install: {e}\n--- last serial output ---\n{tail(install_log)}")
        con.screenshot(os.path.join(args.workdir, "install.png"))
        con.stop()
        return 1
    con.stop(grace=60)       # the installer reboots; -no-reboot makes QEMU exit

    with open(disk, "rb") as f:
        sig = f.read(512)[510:512]
    if sig != b"\x55\xaa":
        print("FAIL install: disk has no MBR/protective-MBR signature after install")
        return 1

    # ---- phase 2: boot the installed system alone
    try:
        patch_installed_disk(disk)
    except Exception as e:
        print(f"FAIL: could not prepare the installed disk: {e}")
        return 1
    print(f"[{fw}] phase 2: boot installed disk")
    if os.path.exists(mon):
        os.unlink(mon)
    con = Console(qemu_cmd(fw, disk, None, vars_path, kvm, mon), boot_log, mon)
    try:
        m = con.expect(BOOT_OK, BOOT_TIMEOUT * scale)
        if m.group(2) or m.group(3):
            raise RuntimeError(m.group(2) or m.group(3))
    except (TimeoutError, EOFError, RuntimeError) as e:
        print(f"FAIL boot: {e}\n--- last serial output ---\n{tail(boot_log)}")
        con.screenshot(os.path.join(args.workdir, "boot.png"))
        con.stop()
        return 1
    con.stop()
    print(f"[{fw}] OK: installed system booted to a login prompt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
