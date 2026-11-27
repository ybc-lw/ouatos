# ouatos installer ISO — build sources

The installer ISO boots on **UEFI** machines, shows a graphical installer and installs the OS image it carries
(`/ouatos/rootfs.txz`, byte-identical to the rootfs.tar.xz it was built from). BIOS (legacy) boot is not supported.

The installer finds the medium by scanning every block device for an ISO 9660 volume labelled `ouatos_install` and
reads the payload straight from the device (`installer/media.py`): no mount, so it does not depend on the kernel's
isofs driver, Rock Ridge, blkid or udev. If the medium cannot be found, /init prints diagnostics and opens a shell.

## Building

```
sudo python3 build_iso.py rootfs.tar.xz [OUTPUT.iso] [--work DIR]
```

Without `OUTPUT.iso` the file is written to the current directory as

```
ouatos-<VERSION_ID>-installer-amd64.iso
```

where `VERSION_ID` is read from the image's `/etc/os-release` (resolved inside the image, so an absolute symlink never
reads the build host's file; `/usr/lib/os-release` is used only if `/etc/os-release` does not exist). If the file has no
`VERSION_ID` the build stops and asks for an explicit `OUTPUT.iso`. Characters other than letters, digits and `._+-` in
`VERSION_ID` become `_`; the name always starts with lowercase `ouatos`. The name is decided right after the image is
unpacked, so a missing `VERSION_ID` fails the build in seconds, not at the end.

Needs root, xz, Pillow, mke2fs and `lsinitramfs` from initramfs-tools-core.

## What the installer does
Welcome → Disk → Partitions → Computer name (default `ouatos`) → Account → Review → Install → Done.
* GPT, whole disk: 512 MiB ESP (FAT32) + ext4 root (+ swap).
* Writes the OS to disk, sets `/etc/hostname`, `/etc/hosts`, `/etc/fstab` (UUIDs).
* Asks for a **root password** (own wizard step) and sets it; creates one user (bash, in group `sudo`, which the
  OS's sudoers already allows) whose primary group is a new group of the same name. The home directory and its
  ownership come from `useradd -m -U`.
* Initialises `/etc/machine-id` with a fresh random ID for every installation (the file is rewritten in place, so the
  owner and mode from the image stay).
* After the accounts exist, a `dpkg --audit` smoke test runs in the chroot (result in the install log only).
* Installs GRUB for UEFI: `EFI/BOOT/BOOTX64.EFI` + `EFI/ouatos/grubx64.efi` + NVRAM entry; the GRUB modules go to
  `/boot/grub/x86_64-efi`.
* Every displayed name comes from the OS's own `os-release` (`engine.system_name`), and "ouatos" is forced to lowercase.

**The installer creates no directories (apart from GRUB's own `/boot/grub/x86_64-efi`) and sets no ownership or modes
on the system it installs.** Everything that is in the image archive arrives exactly as the archive describes it.
The files it does write (`/etc/hostname`, `/etc/hosts`, `/etc/fstab`, `/etc/machine-id`, `/etc/default/grub`,
`/boot/grub/grub.cfg`, the account databases through `useradd`/`chpasswd`) get their mode from the installer's `umask 022`
or, for existing files, from the image.

## Layout
* `build_iso.py`       – orchestrator (see *Building*)
* `build/isowriter.py` – ISO 9660 + Rock Ridge + El Torito (UEFI) + hybrid MBR writer (no xorriso needed)
* `build/mklive.py`    – builds the live initramfs from the OS image's own python/util-linux/kmod/udev + kernel modules
* `build/gen_font.py`  – pre-renders the glyph atlas used by the framebuffer GUI
* `installer/`         – `engine.py` (disks, GPT, mkfs, extraction, accounts, GRUB), `fatfs.py` (FAT writer),
                         `fbgui.py` (framebuffer toolkit + evdev input), `installer.py` (wizard + text fallback),
                         `policy.py` (safe paths into the target, machine-id)
* `tests/`             – FAT / GPT / ISO validators, end-to-end engine test on a loop disk, GUI + input tests,
                         `test_vm.py` (QEMU: install the ISO, boot the result; `--guest-checks` also checks the memory
                         budget and the node / pnpm / llama-server versions inside the installed system)

## What the OS image archive must contain
Because the installer fixes nothing, the archive has to be right when it is made:
* **Empty directories, owners, modes, setuid/setgid bits and xattrs are kept.** The installer extracts with
  `tar -x -p --numeric-owner --xattrs --xattrs-include=*`, so create the archive the same way, for example
  `tar --numeric-owner --xattrs --xattrs-include='*' -C rootfs -cpf - . | xz -T0 > rootfs.tar.xz`.
* **Directories the installer works in must exist:** `/dev /proc /sys /run` (mounted for the chroot), `/tmp`
  (scratch files), `/boot` with the kernel (`vmlinuz-*`), `/usr/bin/bash`. `build_iso.py` refuses an image that lacks
  `/dev /proc /sys /run /tmp /boot`, and the installer stops with a clear message if a mount point is missing or is a
  symlink or a file.
* `/boot/efi` should exist as the mount point of the ESP (`/etc/fstab` mounts it with `nofail`). If it is missing,
  `build_iso.py` warns, the install log says so, and the installed system simply has no mount point until you create it.
* `/etc/os-release` must contain `VERSION_ID` if you want the default ISO name.
* `/usr/lib/os-release` is the file the installer medium records the system name from; the installed system's own
  `/etc/os-release` is read after extraction.
* `build_iso.py` warns when the archive contains Git LFS pointer stubs (real files that were not fetched).

## Boot menu extras
`Install … (text mode)` and `Debug shell (no installer)` (kernel options `ouatos.text=1`, `ouatos.shell=1`).

## Known limits
UEFI only: the ISO has no BIOS boot code, and the installer always writes an ESP, never a BIOS-boot partition.
Secure Boot must be off (GRUB is unsigned). Automatic whole-disk partitioning only. US keyboard layout.
USB sticks: UEFI boot only (the hybrid MBR exposes the EFI image). Needs ~1 GiB RAM, 4 GiB disk.
The OS image ships no e2fsprogs/dosfstools: the installer bundles `mke2fs` from the build host (GPLv2, notice in
`/ouatos/licenses`) and writes FAT itself; fstab disables fsck passes accordingly.

## The target is a foreign file system
Absolute symlinks in the OS image (`/var/run -> /run`, ...) mean something different inside the new system than on the
machine running the installer. All access to the target therefore goes through `policy.tpath(root, rel, follow=False)`,
which resolves every directory component *inside* the target root (absolute links are re-rooted, `..` cannot leave it,
loops raise `ELOOP`) and does not follow the last component unless asked to. `Chroot` refuses to mount over a
symlink or a non-directory, and checks all four mount points before mounting any. `tests/test_policy.py` builds a
target full of absolute links into a simulated host and asserts that the host is byte-for-byte unchanged, and that
`engine.py` calls no `chown`/`chmod`/`mkdir` while configuring the system.
