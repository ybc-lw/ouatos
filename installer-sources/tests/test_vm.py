#!/usr/bin/env python3
"""Install the ISO inside a QEMU VM, then boot the installed disk.

Phase 1: boot the ISO with an empty disk attached, drive the installer over the
         serial console until it reports success.
Phase 2: boot from the installed disk only (no ISO) and wait for a boot marker.

usage: test_vm.py ISO [--firmware bios|uefi] [--workdir DIR] [--disk-size 8G]

Everything between the "ADAPT" markers depends on the installer's actual
console output and must match what it prints. Serial logs are written to
<workdir>/install.log and <workdir>/boot.log so CI can upload them.
"""
import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------- ADAPT ----
# (regex to wait for, text to send afterwards or None, timeout in seconds)
INSTALL_STEPS = [
    (r"Install(er)? .*(ready|started)", "\n", 300),   # live system up, installer prompt
    # (r"Select (target )?disk.*:", "1\n", 60),
    # (r"Type .* to confirm.*:", "yes\n", 60),
]
INSTALL_DONE = r"Installation (complete|finished|succeeded)"
INSTALL_DONE_TIMEOUT = 900

# Printed on the serial console once the *installed* system is up.
BOOT_OK = r"login:"
BOOT_TIMEOUT = 300
# ------------------------------------------------------------------------

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
        """Dump the VM's screen to a PNG (for debugging when serial is silent)."""
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
        except Exception as e:  # debugging aid only
            print(f"screenshot failed: {e}")

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        self.log.close()


def qemu_cmd(firmware, disk, iso, vars_path, kvm, mon):
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
        cmd += ["-drive", f"file={iso},media=cdrom,format=raw,if=ide,index=1",
                "-boot", "order=d"]
    return cmd


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
    ap.add_argument("--disk-size", default="8G")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    disk = os.path.join(args.workdir, "disk.img")
    vars_path = os.path.join(args.workdir, "OVMF_VARS.fd")
    install_log = os.path.join(args.workdir, "install.log")
    boot_log = os.path.join(args.workdir, "boot.log")

    with open(disk, "wb") as f:
        f.truncate({"G": 1 << 30, "M": 1 << 20}[args.disk_size[-1]] * int(args.disk_size[:-1]))
    if args.firmware == "uefi":
        shutil.copy(OVMF_VARS, vars_path)

    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    scale = 1 if kvm else 5
    print(f"[{args.firmware}] KVM {'on' if kvm else 'OFF (TCG, timeouts x5)'}")

    # ---- phase 1: install from ISO
    print(f"[{args.firmware}] phase 1: install from ISO")
    mon = os.path.join(args.workdir, "mon.sock")
    con = Console(qemu_cmd(args.firmware, disk, args.iso, vars_path, kvm, mon),
                  install_log, mon)
    try:
        for pattern, reply, timeout in INSTALL_STEPS:
            con.expect(pattern, timeout * scale)
            if reply:
                con.send(reply)
        con.expect(INSTALL_DONE, INSTALL_DONE_TIMEOUT * scale)
    except (TimeoutError, EOFError) as e:
        print(f"FAIL install: {e}\n--- last serial output ---\n{tail(install_log)}")
        con.screenshot(os.path.join(args.workdir, "install.png"))
        return 1
    finally:
        con.stop()

    with open(disk, "rb") as f:
        sig = f.read(512)[510:512]
    if sig != b"\x55\xaa":
        print("FAIL install: disk has no MBR/protective-MBR signature after install")
        return 1

    # ---- phase 2: boot the installed system (no ISO attached)
    print(f"[{args.firmware}] phase 2: boot installed disk")
    con = Console(qemu_cmd(args.firmware, disk, None, vars_path, kvm, mon),
                  boot_log, mon)
    try:
        con.expect(BOOT_OK, BOOT_TIMEOUT * scale)
    except (TimeoutError, EOFError) as e:
        print(f"FAIL boot: {e}\n--- last serial output ---\n{tail(boot_log)}")
        con.screenshot(os.path.join(args.workdir, "boot.png"))
        return 1
    finally:
        con.stop()

    print(f"[{args.firmware}] OK: installed system booted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
