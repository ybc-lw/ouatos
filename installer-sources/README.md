# ouatos installer ISO — build sources

`ouatos-1-installer-amd64.iso` boots on BIOS and UEFI machines, shows a graphical installer and installs the
OS image it carries (`/ouatos/rootfs.txz`, byte-identical to the rootfs.tar.xz it was built from).

The installer finds the medium by scanning every block device for an ISO 9660 volume labelled `ouatos_install` and
reads the payload straight from the device (`installer/media.py`): no mount, so it does not depend on the kernel's
isofs driver, Rock Ridge, blkid or udev. If the medium cannot be found, /init prints diagnostics and opens a shell.

## What the installer does
Welcome → Disk → Partitions → Computer name (default `ouatos`) → Account → Review → Install → Done.
* GPT, whole disk: BIOS = 1 MiB BIOS-boot + ext4 root (+ swap); UEFI = 512 MiB ESP (FAT32) + ext4 root (+ swap).
* Writes the OS to disk, sets `/etc/hostname`, `/etc/hosts`, `/etc/fstab` (UUIDs).
* Asks for a **root password** (own wizard step) and sets it; creates one user (bash, in group `sudo`, which the
  OS's sudoers already allows) whose primary group is a new group of the same name.
* Initialises `/etc/machine-id` with a fresh random ID for every installation.
* **Ownership / permission policy** (`installer/policy.py`, verified at the end of every install, which fails
  if violated): `/` and everything under it is `root:root`; `/tmp` and `/var/tmp` are `root:root` mode `1777`;
  `/home/<user>` is `<user>:<user>`.
* Installs GRUB (BIOS: core.img embedded; UEFI: `EFI/BOOT/BOOTX64.EFI` + `EFI/ouatos/grubx64.efi` + NVRAM entry).
* Every displayed name comes from the OS's own `os-release` (`engine.system_name`), and "ouatos" is forced to lowercase.

## Layout
* `build_iso.py`       – orchestrator (`sudo python3 build_iso.py rootfs.tar.xz out.iso`; needs root, xz, Pillow, mke2fs and lsinitramfs from initramfs-tools-core)
* `build/isowriter.py` – ISO 9660 + Rock Ridge + El Torito (BIOS+UEFI) + hybrid MBR writer (no xorriso needed)
* `build/mklive.py`    – builds the live initramfs from the OS image's own python/util-linux/kmod/udev + kernel modules
* `build/gen_font.py`  – pre-renders the glyph atlas used by the framebuffer GUI
* `installer/`         – `engine.py` (disks, GPT, mkfs, extraction, accounts, GRUB), `fatfs.py` (FAT writer),
                         `fbgui.py` (framebuffer toolkit + evdev input), `installer.py` (wizard + text fallback)
* `tests/`             – FAT / GPT / ISO validators, end-to-end engine test on a loop disk, GUI + input tests

## Boot menu extras
`Install … (text mode)` and `Debug shell (no installer)` (kernel options `ouatos.text=1`, `ouatos.shell=1`).

## Known limits
Secure Boot must be off (GRUB is unsigned). Automatic whole-disk partitioning only. US keyboard layout.
USB sticks: UEFI boot only (hybrid MBR exposes the EFI image; no legacy-BIOS USB boot code). Needs ~1 GiB RAM, 4 GiB disk.
The OS image ships no e2fsprogs/dosfstools: the installer bundles `mke2fs` from the build host (GPLv2, notice in
`/ouatos/licenses`) and writes FAT itself; fstab disables fsck passes accordingly.

## About the OS image archive (what the installer compensates for)
The archive has no empty directories (no /dev /proc /sys /run /home /tmp ...), every entry is 0:0 and modes are
flattened (no setuid bits; `/etc/shadow` 0644, `/etc/sudoers` 0644, `/root` 0755).  The installer therefore creates
the standard directories, only ever *tightens* the security-critical modes (shadow 0640, sudoers 0440, /root 0700)
and restores the setuid bit on sudo/su/passwd/mount/umount/chfn/chsh/gpasswd/newgrp (see `policy.py`).
`build_iso.py` warns when the archive contains Git LFS pointer stubs (real files that were not fetched).
