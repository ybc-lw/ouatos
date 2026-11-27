"""Safe access to the freshly extracted system (the "target").

The OS image archive is trusted to be complete: it carries every directory, and every owner and mode is already
what it has to be.  The installer therefore creates no directories and sets no ownership or modes on the image;
this module only makes sure that nothing the installer does to the target can reach the machine it runs on.

The target is a foreign file system: an absolute symlink in it (/var/run -> /run) points into the TARGET once the
system runs, but the installer's own open()/unlink()/mount() would resolve it against the LIVE system.  Every path
into the target therefore goes through tpath().

Also here: init_machine_id(), because /etc/machine-id is the one file whose content must be unique per installation.
"""
import errno
import os
import uuid


def tpath(root, rel, follow=False):
    """The real path, on the machine the installer runs on, of `rel` inside the target at `root`.

    Every directory component is resolved inside `root` (absolute links are re-rooted, '..' cannot leave it) and
    the result never leaves it.  The last component is not followed unless follow=True (use that to read a file or
    test that something exists; leave it off to replace or unlink the entry itself).
    Raises OSError(ELOOP) for a symlink loop."""
    rel = rel.lstrip('/')
    full = os.path.join(root, rel) if rel else root
    if follow or not rel:
        real = resolve_in_root(root, full)
    else:
        parent = resolve_in_root(root, os.path.dirname(full))
        real = None if parent is None else os.path.join(parent, os.path.basename(full))
    if real is None:
        raise OSError(errno.ELOOP, 'too many levels of symbolic links', full)
    return real


def resolve_in_root(root, path, limit=40):
    """Resolve `path` the way the installed system will see it: an absolute symlink target is taken relative to
    `root`, not to the live system, and '..' never leaves `root`.  Returns the resolved path, or None for a loop."""
    root = os.path.normpath(root)
    cur = root
    todo = [c for c in os.path.relpath(os.path.normpath(path), root).split('/') if c]
    todo.reverse()
    hops = 0
    while todo:
        part = todo.pop()
        if part == '.':
            continue
        if part == '..':
            if cur != root:
                cur = os.path.dirname(cur)
            continue
        nxt = os.path.join(cur, part)
        if os.path.islink(nxt):
            hops += 1
            if hops > limit:
                return None
            link = os.readlink(nxt)
            if link.startswith('/'):
                cur = root
            todo.extend(reversed([c for c in link.split('/') if c]))
            continue
        cur = nxt
    return cur


def init_machine_id(root):
    """Give this installation its own random machine-id (never the image's).

    A regular file is rewritten in place, so the owner and mode the image gave it are kept; a symlink is replaced
    by a regular file (never written through: it could point anywhere)."""
    mid = uuid.uuid4().hex                                  # 32 lowercase hex digits, a random (version 4) UUID
    path = tpath(root, 'etc/machine-id')                    # parents resolved inside the target, the entry itself not followed
    if os.path.islink(path):
        os.unlink(path)
    with open(path, 'w') as f:
        f.write(mid + '\n')
    dbus = tpath(root, 'var/lib/dbus', follow=True)         # D-Bus shares the same ID
    if os.path.isdir(dbus):
        link = os.path.join(dbus, 'machine-id')
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink('/etc/machine-id', link)
    return mid
