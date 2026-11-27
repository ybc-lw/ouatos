#!/usr/bin/env python3
"""Build the ouatos root filesystem.

Project layout (this file lives in scripts/ under the project root):

    ouatos/
    ├── scripts/
    │   └── build.py    this program
    ├── installer/
    ├── assets/         inputs: etc/, usr/, packages.txt, torch*.whl
    └── rootfs/         generated output

Progress is written to __pycache__/build-progress.json (in the project root)
after every completed step. If the build is interrupted or killed, run the
same command again and it resumes from the first unfinished step.

Usage (must run as root, from the project root):
    sudo python3 scripts/build.py
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS = PROJECT_ROOT / "assets"
ROOTFS = PROJECT_ROOT / "rootfs"

# Progress lives inside the __pycache__ folder in the project root.
STATE_DIR = PROJECT_ROOT / "__pycache__"
STATE_FILE = STATE_DIR / "build-progress.json"

SUITE = "trixie"  # Debian 13
ARCH = "amd64"
VARIANT = "minbase"
DEFAULT_SNAPSHOT_DATE = "2026-09-18"
SNAPSHOT_MIRROR = "http://snapshot.debian.org/archive/debian/{stamp}/"

# Packages from packages.txt are installed from the latest archive, not the snapshot.
LATEST_SOURCES = (
    f"deb http://deb.debian.org/debian {SUITE} main\n"
    f"deb http://deb.debian.org/debian {SUITE}-updates main\n"
    f"deb http://deb.debian.org/debian-security {SUITE}-security main\n"
)
LATEST_SOURCES_FILE = "/tmp/ouatos-latest.list"  # path as seen inside the chroot

HOST_KEYRING = Path("/usr/share/keyrings/debian-archive-keyring.gpg")

NETWORK_CHECK_HOST = "deb.debian.org"
NETWORK_CHECK_PORT = 80
NETWORK_CHECK_TIMEOUT = 10

# Environment used for every command executed inside the chroot.
CHROOT_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "TERM": "dumb",
    "DEBIAN_FRONTEND": "noninteractive",
    "DEBCONF_NONACTIVE_SEEN": "true",
    "PIP_ROOT_USER_ACTION": "ignore",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    # Never wait for a confirmation prompt when corepack downloads pnpm.
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
}

APT_OPTIONS = [
    "-o", "Dpkg::Use-Pty=0",
    "-o", "APT::Color=0",
    # Keep files that came from assets/etc when a package ships the same conffile.
    "-o", "Dpkg::Options::=--force-confdef",
    "-o", "Dpkg::Options::=--force-confold",
]

# Directories whose contents are removed (the directories themselves stay).
PURGE_DIRS = ["sys", "dev", "proc", "tmp", "var/tmp", "var/cache", "var/lib/apt/lists"]

# Per-home cache locations (relative to /root and every /home/<user>).
HOME_CACHE_DIRS = [
    ".cache",              # pip, corepack (.cache/node/corepack), pnpm, node-gyp, ...
    ".npm/_cacache",
    ".npm/_logs",
    ".node/corepack",      # legacy corepack home
    ".local/share/pnpm/store",
    ".pnpm-store",
]
# System-wide cache locations outside /var/cache.
SYSTEM_CACHE_DIRS = ["usr/local/share/.cache"]

# Documentation directories under usr/share whose contents are removed.
DOC_DIRS = ["usr/share/doc", "usr/share/info", "usr/share/man"]

TORCH_CHECK = r'''
import sys
import torch

torch.manual_seed(0)
model = torch.nn.Sequential(
    torch.nn.Linear(8, 16),
    torch.nn.ReLU(),
    torch.nn.Linear(16, 4),
).eval()
x = torch.randn(2, 8)
with torch.inference_mode():
    y = model(x)
    ref = torch.relu(x @ model[0].weight.T + model[0].bias) @ model[2].weight.T + model[2].bias
if tuple(y.shape) != (2, 4):
    sys.exit(f"unexpected output shape: {tuple(y.shape)}")
if not torch.isfinite(y).all():
    sys.exit("output contains non-finite values")
if not torch.allclose(y, ref, atol=1e-5):
    sys.exit("output does not match the reference computation")
print(f"torch {torch.__version__}: inference ok, output shape {tuple(y.shape)}")
'''

LLAMA_TEST_MODEL = "ouatlm-Q4_K_M.gguf"

# (display name, command, regex that must match the output)
VERSION_CHECKS = [
    ("node", ["node", "--version"], r"^v\d+\.\d+\.\d+"),
    ("pnpm", ["pnpm", "--version"], r"^\d+\.\d+\.\d+"),
    # llama-server prints "version: <build> (<commit>)", usually on stderr.
    ("llama-server", ["llama-server", "--version"], r"version\W{0,3}v?\d+|\d+\.\d+\.\d+"),
]

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

_BRAND_RE = re.compile("ouatos", re.IGNORECASE)


def sanitize(text: str) -> str:
    """Force the project name to lowercase in everything shown to the user."""
    return _BRAND_RE.sub("ouatos", text)


def log(message: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    print(sanitize(message), file=stream, flush=True)


class BuildError(Exception):
    """A fatal build error; progress made so far is kept."""


# --------------------------------------------------------------------------
# Progress tracking
# --------------------------------------------------------------------------


@dataclass
class Options:
    snapshot_date: str

    def as_dict(self) -> dict:
        return {"snapshot_date": self.snapshot_date}


class Progress:
    """Completed-step list persisted atomically as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.options: dict = {}
        self.completed: list[str] = []

    @classmethod
    def load(cls, path: Path) -> "Progress":
        progress = cls(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return progress
        if isinstance(data, dict) and data.get("schema") == 1:
            progress.options = dict(data.get("options", {}))
            progress.completed = [str(s) for s in data.get("completed", [])]
        return progress

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"schema": 1, "options": self.options, "completed": self.completed}
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def mark_done(self, name: str) -> None:
        if name not in self.completed:
            self.completed.append(name)
        self.save()

    def reset(self, options: dict) -> None:
        self.options = options
        self.completed = []
        self.save()


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def run(cmd: list[str], *, check: bool = True, label: str | None = None) -> tuple[int, str]:
    """Run a command, stream its merged output through the logger, return (rc, output)."""
    shown = label or shlex.join(str(c) for c in cmd)
    if len(shown) > 160:
        shown = shown[:157] + "..."
    log(f"$ {shown}")
    proc = subprocess.Popen(
        [str(c) for c in cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        start_new_session=True,  # lets us kill the whole process group on abort
    )
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            log(f"    {line}")
        rc = proc.wait()
    except BaseException:
        _kill_group(proc)
        raise
    if check and rc != 0:
        raise BuildError(f"command failed with exit code {rc}: {shown}")
    return rc, "\n".join(lines)


def chroot_run(
    cmd: list[str], *, check: bool = True, extra_env: dict[str, str] | None = None
) -> tuple[int, str]:
    env = {**CHROOT_ENV, **(extra_env or {})}
    full = [
        "chroot", str(ROOTFS), "/usr/bin/env", "-i",
        *[f"{key}={value}" for key, value in env.items()],
        *cmd,
    ]
    return run(full, check=check, label="chroot: " + shlex.join(cmd))


# --------------------------------------------------------------------------
# Mount handling
# --------------------------------------------------------------------------


def _unescape_mountinfo(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mounts_under(root: Path) -> list[str]:
    """Mount points strictly below root, deepest first."""
    prefix = str(root.resolve()) + "/"
    found: set[str] = set()
    with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) > 4:
                mount_point = _unescape_mountinfo(fields[4])
                if mount_point.startswith(prefix):
                    found.add(mount_point)
    return sorted(found, key=len, reverse=True)


def unmount_all() -> None:
    """Unmount everything mounted below rootfs; fall back to lazy unmount."""
    for _ in range(3):
        targets = mounts_under(ROOTFS)
        if not targets:
            return
        for target in targets:
            result = subprocess.run(
                ["umount", target], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                subprocess.run(
                    ["umount", "-l", target], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
    remaining = mounts_under(ROOTFS)
    if remaining:
        raise BuildError("could not unmount: " + ", ".join(remaining))


def prepare_chroot() -> None:
    """Idempotently mount proc/sys/dev inside rootfs."""
    unmount_all()  # start from a known state, e.g. after a killed run
    for sub in ("proc", "sys", "dev", "tmp"):
        (ROOTFS / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(ROOTFS / "tmp", 0o1777)
    run(["mount", "-t", "proc", "proc", ROOTFS / "proc"])
    run(["mount", "-t", "sysfs", "sysfs", ROOTFS / "sys"])
    run(["mount", "--rbind", "/dev", ROOTFS / "dev"])
    # Slave propagation: unmounting inside rootfs can never affect the host.
    run(["mount", "--make-rslave", ROOTFS / "dev"])


# --------------------------------------------------------------------------
# Filesystem helpers
# --------------------------------------------------------------------------


def remove_path(path: Path) -> None:
    """Remove a file, symlink or directory tree without following symlinks."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def delete_children(directory: Path) -> None:
    """Delete everything inside directory but keep the directory itself."""
    if not os.path.lexists(directory):
        return
    root = ROOTFS.resolve()
    resolved = directory.resolve()
    if resolved != root and root not in resolved.parents:
        raise BuildError(f"refusing to delete outside rootfs: {directory}")
    if resolved == Path("/"):
        raise BuildError("refusing to delete the host root")
    # Never delete through a live mount: that would hit host files.
    leftover = mounts_under(ROOTFS)
    if leftover:
        raise BuildError("rootfs still has mounts: " + ", ".join(leftover))
    with os.scandir(resolved) as entries:
        children = [Path(entry.path) for entry in entries]
    for child in children:
        remove_path(child)


def wipe_rootfs() -> None:
    unmount_all()
    delete_children(ROOTFS)


def merge_tree(src: Path, dst: Path, skip: frozenset[str] = frozenset()) -> None:
    """Copy src into dst: merge directories, overwrite files and symlinks.

    Destination entries are removed before writing so nothing is ever written
    through a symlink or an existing hard link. Everything is owned by root.
    Metadata of directories that already exist in dst is left untouched.
    """
    if dst.is_symlink() or (os.path.lexists(dst) and not dst.is_dir()):
        remove_path(dst)
    created = not dst.exists()
    dst.mkdir(parents=True, exist_ok=True)
    with os.scandir(src) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        if entry.name in skip:
            continue
        source = Path(entry.path)
        target = dst / entry.name
        if entry.is_symlink():
            remove_path(target)
            os.symlink(os.readlink(source), target)
            os.lchown(target, 0, 0)
        elif entry.is_dir(follow_symlinks=False):
            merge_tree(source, target)
        elif entry.is_file(follow_symlinks=False):
            remove_path(target)
            shutil.copyfile(source, target, follow_symlinks=False)
            os.chown(target, 0, 0)  # chown first: it may clear setuid bits
            shutil.copystat(source, target, follow_symlinks=False)
        else:
            log(f"warning: skipping special file {source}")
    if created:
        os.chown(dst, 0, 0)
        shutil.copystat(src, dst)


# --------------------------------------------------------------------------
# packages.txt parsing
# --------------------------------------------------------------------------

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9+.\-]*(?::[a-z0-9\-]+)?")
_VERSION_RE = re.compile(r"[0-9A-Za-z.+~:\-]+")


def read_package_list(path: Path) -> list[tuple[str, str | None]]:
    """Parse 'name<TAB>version' lines (dpkg-query -W format) into (name, version)."""
    packages: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        name = parts[0]
        version = parts[1] if len(parts) > 1 else None
        if not _NAME_RE.fullmatch(name):
            raise BuildError(f"{path.name}:{number}: invalid package name {name!r}")
        if version is not None and not _VERSION_RE.fullmatch(version):
            raise BuildError(f"{path.name}:{number}: invalid version {version!r}")
        if name in seen:
            continue
        seen.add(name)
        packages.append((name, version))
    if not packages:
        raise BuildError(f"{path.name} lists no packages")
    return packages


# --------------------------------------------------------------------------
# Build steps
# --------------------------------------------------------------------------


def step_debootstrap(opts: Options) -> None:
    if ROOTFS.exists() and any(ROOTFS.iterdir()):
        log("removing the partial rootfs left by an interrupted run")
        wipe_rootfs()
    ROOTFS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.fromisoformat(opts.snapshot_date).strftime("%Y%m%dT000000Z")
    mirror = SNAPSHOT_MIRROR.format(stamp=stamp)
    run([
        "debootstrap",
        f"--arch={ARCH}",
        f"--variant={VARIANT}",
        SUITE, str(ROOTFS), mirror,
    ])


def step_copy_etc(opts: Options) -> None:
    merge_tree(ASSETS / "etc", ROOTFS / "etc", skip=frozenset({"systemd"}))


def step_apt_install(opts: Options) -> None:
    packages = read_package_list(ASSETS / "packages.txt")
    specs = [
        f"{name}={version}" if version else name
        for name, version in packages
    ]
    log(f"{len(specs)} packages from packages.txt, installed from the latest archive")

    # Use the latest archive for this step only; rootfs/etc/apt is left untouched.
    sources = ROOTFS / LATEST_SOURCES_FILE.lstrip("/")
    sources.write_text(LATEST_SOURCES, encoding="utf-8")
    apt_options = [
        *APT_OPTIONS,
        "-o", f"Dir::Etc::SourceList={LATEST_SOURCES_FILE}",
        "-o", "Dir::Etc::SourceParts=/dev/null",
    ]
    # Keep service start-up scripts from running inside the chroot.
    policy = ROOTFS / "usr" / "sbin" / "policy-rc.d"
    policy.write_text("#!/bin/sh\nexit 101\n", encoding="utf-8")
    policy.chmod(0o755)
    try:
        # Repair a half-configured state left by an interrupted run.
        chroot_run(["dpkg", "--configure", "-a"])
        chroot_run(["apt-get", *apt_options, "update"])
        try:
            chroot_run([
                "apt-get", *apt_options, "install", "-y", "--no-install-recommends", *specs,
            ])
        except BuildError:
            if any(version for _, version in packages):
                log("hint: pinned versions in packages.txt may no longer exist in the latest archive; "
                    "remove the version from packages.txt to install the latest version",
                    err=True)
            raise
    finally:
        remove_path(policy)
        remove_path(sources)


def step_network_check(opts: Options) -> None:
    """Verify chroot DNS resolution and outbound TCP connectivity."""
    script = (
        "import socket; "
        f"s = socket.create_connection(({NETWORK_CHECK_HOST!r}, {NETWORK_CHECK_PORT}), "
        f"timeout={NETWORK_CHECK_TIMEOUT}); "
        "s.close()"
    )
    rc, _ = chroot_run(["python3", "-c", script], check=False)
    if rc != 0:
        raise BuildError(
            f"TCP connection to {NETWORK_CHECK_HOST}:{NETWORK_CHECK_PORT} failed; the network is unusable"
        )
    log(f"network ok: TCP connection to {NETWORK_CHECK_HOST}:{NETWORK_CHECK_PORT} succeeded")


def step_replace_systemd(opts: Options) -> None:
    target = ROOTFS / "etc" / "systemd"
    remove_path(target)
    merge_tree(ASSETS / "etc" / "systemd", target)


def step_copy_usr(opts: Options) -> None:
    merge_tree(ASSETS / "usr", ROOTFS / "usr")

def step_git_lfs_install(opts: Options) -> None:
    chroot_run(["git", "lfs", "install", "--system"])

def step_install_torch(opts: Options) -> None:
    wheels = sorted(ASSETS.glob("torch*.whl"))
    if not wheels:
        raise BuildError("no torch*.whl found in assets")
    stage = ROOTFS / "tmp" / "ouatos-wheels"
    remove_path(stage)
    stage.mkdir(parents=True)
    for wheel in wheels:
        shutil.copyfile(wheel, stage / wheel.name)
    try:
        chroot_run([
            "python3", "-m", "pip", "install",
            "--break-system-packages",  # global install into the externally managed python
            "--no-cache-dir", "--no-compile", "--progress-bar", "off",
            *[f"/tmp/ouatos-wheels/{wheel.name}" for wheel in wheels],
        ])
    finally:
        remove_path(stage)


def step_test_torch(opts: Options) -> None:
    chroot_run(["python3", "-c", TORCH_CHECK])


def step_test_llama(opts: Options) -> None:
    model = ASSETS / LLAMA_TEST_MODEL
    if not model.is_file():
        raise BuildError(f"missing {model}")
    stage = ROOTFS / "tmp" / LLAMA_TEST_MODEL
    remove_path(stage)
    shutil.copyfile(model, stage)
    try:
        _, output = chroot_run([
            "llama-completion",
            "-m", f"/tmp/{LLAMA_TEST_MODEL}",
            "-t", "1",
            "-p", "Hello",
            "-n", "16",
            "--temp", "0",
            "--no-display-prompt",
            "--jinja",
            "-st",
        ])
    finally:
        remove_path(stage)
    if not re.search(r"^I don[\u2019']t know\b", output, re.MULTILINE):
        raise BuildError("llama.cpp inference produced unexpected output")
    log("llama.cpp: inference ok")


def step_check_versions(opts: Options) -> None:
    failures: list[str] = []
    for name, cmd, pattern in VERSION_CHECKS:
        rc, output = chroot_run(cmd, check=False)
        if rc == 0 and re.search(pattern, output, re.MULTILINE | re.IGNORECASE):
            log(f"{name}: version check ok")
        else:
            failures.append(name)
    if failures:
        raise BuildError("no version number from: " + ", ".join(failures))


def step_remove_grub_i386_pc(opts: Options) -> None:
    target = ROOTFS / "usr" / "lib" / "grub" / "i386-pc"
    if os.path.lexists(target):
        log(f"removing {target.relative_to(ROOTFS)}")
        remove_path(target)
    else:
        log(f"{target.relative_to(ROOTFS)} does not exist; nothing to remove")

def step_remove_machine_id(opts: Options) -> None:
    """Remove generated machine IDs so each installed system gets its own ID."""
    for rel in ("etc/machine-id", "var/lib/dbus/machine-id"):
        target = ROOTFS / rel
        if os.path.lexists(target):
            log(f"removing {rel}")
            remove_path(target)
        else:
            log(f"{rel} does not exist; nothing to remove")

def _kernel_sort_key(release: str) -> tuple[int, ...]:
    """Sortable version of a kernel release; flavor suffixes such as -amd64 are ignored."""
    version = re.sub(r"(?:-[a-z][a-z0-9]*)+$", "", release)
    return tuple(int(number) for number in re.findall(r"\d+", version))


def step_purge_old_kernels(opts: Options) -> None:
    """Purge every kernel package older than the newest installed kernel."""
    _, output = chroot_run(["dpkg-query", "-W", "-f=${Package}\\n"])
    names = [line.strip() for line in output.splitlines() if line.strip()]

    releases: set[str] = set()
    for name in names:
        match = re.fullmatch(r"linux-image-(\d\S*)", name)
        if match:
            releases.add(re.sub(r"-(?:unsigned|dbg)$", "", match.group(1)))
    if len(releases) < 2:
        log("only one kernel is installed; nothing to purge")
        return
    latest = max(releases, key=lambda r: (_kernel_sort_key(r), r))
    latest_key = _kernel_sort_key(latest)
    log(f"newest kernel: {latest}")

    old_packages = [
        name for name in names
        if (m := re.fullmatch(r"linux-(?:image|headers|modules(?:-extra)?)-(\d\S*)", name))
        and _kernel_sort_key(m.group(1)) < latest_key
    ]
    if not old_packages:
        return

    # The kernel prerm aborts when asked to remove the kernel of the host it runs
    # on; answer that question up front so the purge cannot be blocked by it.
    for name in old_packages:
        if name.startswith("linux-image-"):
            chroot_run([
                "sh", "-c",
                f"echo '{name} {name}/prerm/removing-running-kernel boolean false' | debconf-set-selections",
            ])
    chroot_run(["apt-get", *APT_OPTIONS, "purge", "-y", *old_packages])

    # Remove what the package scripts may have left behind.
    for directory in (ROOTFS / "usr" / "lib" / "modules", ROOTFS / "var" / "lib" / "initramfs-tools"):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            key = _kernel_sort_key(entry.name)
            if key and key < latest_key:
                log(f"removing leftover {entry.relative_to(ROOTFS)}")
                remove_path(entry)


def step_unmount(opts: Options) -> None:
    unmount_all()


def step_purge_dirs(opts: Options) -> None:
    for rel in PURGE_DIRS:
        log(f"emptying {rel}")
        delete_children(ROOTFS / rel)


def step_purge_caches(opts: Options) -> None:
    homes = [ROOTFS / "root"]
    home_root = ROOTFS / "home"
    if home_root.is_dir() and not home_root.is_symlink():
        homes += sorted(p for p in home_root.iterdir() if p.is_dir() and not p.is_symlink())
    for home in homes:
        if home.is_symlink() or not home.is_dir():
            continue
        for rel in HOME_CACHE_DIRS:
            remove_path(home / rel)
    for rel in SYSTEM_CACHE_DIRS:
        remove_path(ROOTFS / rel)


def step_purge_bytecode(opts: Options) -> None:
    if mounts_under(ROOTFS):
        raise BuildError("rootfs still has mounts")
    removed = 0
    for dirpath, dirnames, filenames in os.walk(ROOTFS, topdown=True, followlinks=False):
        for name in list(dirnames):
            if name == "__pycache__":
                remove_path(Path(dirpath) / name)
                dirnames.remove(name)
                removed += 1
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                remove_path(Path(dirpath) / name)
                removed += 1
    log(f"removed {removed} python bytecode entries")


def step_purge_docs(opts: Options) -> None:
    for rel in DOC_DIRS:
        log(f"emptying {rel}")
        delete_children(ROOTFS / rel)


def step_prune_boot(opts: Options) -> None:
    """Keep only the newest kernel and initrd; drop *.old links, config and System.map."""
    boot = ROOTFS / "boot"
    if not boot.is_dir():
        raise BuildError(f"missing directory: {boot}")

    releases = [
        entry.name[len("vmlinuz-"):]
        for entry in boot.iterdir()
        if re.fullmatch(r"vmlinuz-\d.*", entry.name)
    ]
    if not releases:
        raise BuildError("no kernel (vmlinuz-*) found in rootfs/boot")
    latest = max(releases, key=lambda r: (_kernel_sort_key(r), r))
    if not (boot / f"initrd.img-{latest}").is_file():
        raise BuildError(f"no initrd.img-{latest} in rootfs/boot for the newest kernel")
    keep = {f"vmlinuz-{latest}", f"initrd.img-{latest}"}
    log(f"keeping kernel {latest}")

    for entry in sorted(boot.iterdir()):
        name = entry.name
        is_kernel_file = re.match(r"(vmlinuz|initrd\.img|config|System\.map)(-|\.old$)", name)
        if is_kernel_file and name not in keep:
            log(f"removing boot/{name}")
            remove_path(entry)

    for name in ("initrd.img.old", "vmlinuz.old"):
        remove_path(ROOTFS / name)

    # Repoint the remaining initrd.img / vmlinuz links at the kept files.
    for directory, prefix in ((ROOTFS, "boot/"), (boot, "")):
        for link, target in (("vmlinuz", f"vmlinuz-{latest}"), ("initrd.img", f"initrd.img-{latest}")):
            path = directory / link
            if path.is_symlink() and os.readlink(path) != prefix + target:
                path.unlink()
                path.symlink_to(prefix + target)


@dataclass
class Step:
    name: str
    title: str
    func: Callable[[Options], None]
    needs_chroot: bool = False


STEPS = [
    Step("debootstrap", "debootstrap debian 13 minbase amd64 snapshot", step_debootstrap),
    Step("copy-etc", "copy assets/etc (except systemd) into rootfs/etc", step_copy_etc),
    Step("apt-install", "install the packages listed in packages.txt from the latest sources", step_apt_install, True),
    Step("network-check", f"network check: TCP {NETWORK_CHECK_HOST}:{NETWORK_CHECK_PORT}", step_network_check, True),
    Step("replace-systemd", "replace rootfs/etc/systemd with assets/etc/systemd", step_replace_systemd),
    Step("copy-usr", "copy assets/usr into rootfs/usr", step_copy_usr),
    Step("git-lfs-install", "configure git lfs system-wide inside the chroot", step_git_lfs_install, True),
    Step("install-torch", "pip install torch*.whl inside the chroot", step_install_torch, True),
    Step("test-torch", "run a torch inference test", step_test_torch, True),
    Step("test-llama", "run a llama.cpp inference test", step_test_llama, True),
    Step("check-versions", "check node, pnpm and llama-server versions", step_check_versions, True),
    Step("purge-old-kernels", "purge old kernel packages, keep the newest", step_purge_old_kernels, True),
    Step("unmount", "unmount everything below rootfs", step_unmount),
    Step("purge-dirs", "empty sys, dev, proc, tmp, var/tmp, var/cache, var/lib/apt/lists", step_purge_dirs),
    Step("purge-caches", "remove python, pip, npm, pnpm and corepack caches", step_purge_caches),
    Step("purge-bytecode", "remove python bytecode", step_purge_bytecode),
    Step("purge-docs", "empty usr/share/doc, info and man", step_purge_docs),
    Step("prune-boot", "keep only the newest kernel and initrd in boot", step_prune_boot),
    Step("remove-grub-i386-pc", "remove usr/lib/grub/i386-pc", step_remove_grub_i386_pc),
    Step("remove-machine-id", "remove etc/machine-id and var/lib/dbus/machine-id", step_remove_machine_id),
]

# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def ensure_debootstrap() -> None:
    """Install debootstrap on the host with apt when it is missing."""
    if shutil.which("debootstrap"):
        return
    if not shutil.which("apt-get"):
        raise BuildError("debootstrap is missing and apt-get is not available; install debootstrap manually")
    log("debootstrap not found on the host; installing it with apt-get")
    apt_env = ["env", "DEBIAN_FRONTEND=noninteractive"]
    try:
        run([*apt_env, "apt-get", "update"])
        run([*apt_env, "apt-get", "install", "-y", "--no-install-recommends", "debootstrap"])
    except BuildError as exc:
        raise BuildError(f"could not install debootstrap with apt-get ({exc})") from exc
    if not shutil.which("debootstrap"):
        raise BuildError("debootstrap is still missing after apt-get install")
    # Without recommends the debian keyring may be absent; debootstrap needs it
    # to verify the release signature. Failure here is not fatal (custom keyring).
    if not HOST_KEYRING.exists():
        try:
            run([*apt_env, "apt-get", "install", "-y", "--no-install-recommends", "debian-archive-keyring"])
        except BuildError:
            log("warning: could not install debian-archive-keyring", err=True)


def preflight() -> None:
    if os.geteuid() != 0:
        raise BuildError("this program must run as root (for example with sudo)")
    required = [
        (ASSETS / "etc", "directory"),
        (ASSETS / "etc" / "systemd", "directory"),
        (ASSETS / "usr", "directory"),
        (ASSETS / "packages.txt", "file"),
        (ASSETS / LLAMA_TEST_MODEL, "file"),
    ]
    for path, kind in required:
        ok = path.is_dir() if kind == "directory" else path.is_file()
        if not ok:
            raise BuildError(f"missing {kind}: {path}")
    if not list(ASSETS.glob("torch*.whl")):
        raise BuildError(f"no torch*.whl found in {ASSETS}")
    missing = [tool for tool in ("chroot", "mount", "umount") if not shutil.which(tool)]
    if missing:
        raise BuildError("missing host tools: " + ", ".join(missing))
    ensure_debootstrap()


def build(opts: Options, reset: bool) -> None:
    preflight()
    progress = Progress.load(STATE_FILE)

    if reset:
        log("resetting: wiping rootfs and forgetting progress")
        wipe_rootfs()
        progress.reset(opts.as_dict())
    elif progress.completed and progress.options != opts.as_dict():
        raise BuildError("saved progress was made with different options; use --reset to start over")
    elif progress.completed and "debootstrap" in progress.completed and not (ROOTFS / "usr").is_dir():
        log("progress file does not match the rootfs on disk; starting over")
        progress.reset(opts.as_dict())
    elif not progress.completed:
        progress.reset(opts.as_dict())

    total = len(STEPS)
    started = time.monotonic()
    try:
        for index, step in enumerate(STEPS, 1):
            if step.name in progress.completed:
                log(f"[ouatos] [{index}/{total}] {step.title}: already done, skipping")
                continue
            log(f"[ouatos] [{index}/{total}] {step.title}")
            begin = time.monotonic()
            if step.needs_chroot:
                prepare_chroot()
            step.func(opts)
            progress.mark_done(step.name)
            log(f"[ouatos] [{index}/{total}] done in {time.monotonic() - begin:.0f}s")
    finally:
        # Never leave host mounts inside rootfs, even after an error or Ctrl-C.
        try:
            unmount_all()
        except BuildError as exc:
            log(f"warning: {exc}", err=True)

    minutes = (time.monotonic() - started) / 60
    log(f"[ouatos] rootfs build finished: {ROOTFS} ({minutes:.1f} min this run)")


def _raise_interrupt(signum, frame) -> None:  # noqa: ANN001
    raise KeyboardInterrupt


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ouatos-build",
        description="build the ouatos root filesystem; an interrupted build resumes where it stopped.",
    )
    parser.add_argument(
        "--snapshot-date", default=DEFAULT_SNAPSHOT_DATE, metavar="YYYY-MM-DD",
        help="debian snapshot date to bootstrap from (default: %(default)s)",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="wipe rootfs and forget saved progress before building",
    )
    args = parser.parse_args(argv)
    try:
        datetime.date.fromisoformat(args.snapshot_date)
    except ValueError:
        parser.error("--snapshot-date must look like 2026-09-18")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    signal.signal(signal.SIGHUP, _raise_interrupt)
    opts = Options(snapshot_date=args.snapshot_date)
    try:
        build(opts, args.reset)
    except BuildError as exc:
        log(f"error: {exc}", err=True)
        log("progress is saved; run the same command again to resume", err=True)
        return 1
    except KeyboardInterrupt:
        log("interrupted; progress is saved, run the same command again to resume", err=True)
        return 130
    except Exception as exc:  # noqa: BLE001
        log(f"error: unexpected {type(exc).__name__}: {exc}", err=True)
        log("progress is saved; run the same command again to resume", err=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())