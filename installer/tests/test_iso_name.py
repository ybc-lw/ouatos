"""Default ISO name (ouatos-<VERSION_ID>-installer-amd64.iso) and the image directory check of build_iso.py."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'installer'), str(ROOT / 'build')]
import engine
spec = importlib.util.spec_from_file_location('build_iso', ROOT / 'build_iso.py')
build_iso = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_iso)


def image(root, etc=None, usr=None, etc_link=None):
    (root / 'usr/lib').mkdir(parents=True)
    (root / 'etc').mkdir()
    if usr is not None:
        (root / 'usr/lib/os-release').write_text(usr)
    if etc is not None:
        (root / 'etc/os-release').write_text(etc)
    if etc_link is not None:
        (root / 'etc/os-release').symlink_to(etc_link)


class VersionIdTests(unittest.TestCase):
    def read(self, **kw):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image(root, **kw)
            return build_iso.read_version_id(str(root))

    def test_regular_file(self):
        self.assertEqual(self.read(etc='NAME=ouatos\nVERSION_ID="1.2"\n'), '1.2')

    def test_etc_wins_over_usr_lib(self):
        self.assertEqual(self.read(etc='VERSION_ID=7\n', usr='VERSION_ID=13\n'), '7')

    def test_relative_symlink(self):
        self.assertEqual(self.read(usr='VERSION_ID="13"\n', etc_link='../usr/lib/os-release'), '13')

    def test_absolute_symlink_stays_inside_the_image(self):
        host = engine.parse_os_release(Path('/etc/os-release').read_text()).get('VERSION_ID') if Path('/etc/os-release').exists() else None
        version = self.read(usr='VERSION_ID="999.0-image"\n', etc_link='/usr/lib/os-release')
        self.assertEqual(version, '999.0-image')
        self.assertNotEqual(version, host)

    def test_falls_back_to_usr_lib_only_when_etc_is_missing(self):
        self.assertEqual(self.read(usr='VERSION_ID=5\n'), '5')

    def test_missing_version_id_is_an_error_not_a_guess(self):
        with self.assertRaisesRegex(SystemExit, 'no VERSION_ID'):
            self.read(etc='NAME=ouatos\n', usr='VERSION_ID=13\n')
        with self.assertRaisesRegex(SystemExit, 'no VERSION_ID'):
            self.read()


class NameTests(unittest.TestCase):
    def test_default_name(self):
        self.assertEqual(build_iso.default_iso_name('13.1'), 'ouatos-13.1-installer-amd64.iso')
        self.assertEqual(build_iso.default_iso_name('2026.09'), 'ouatos-2026.09-installer-amd64.iso')

    def test_name_is_a_safe_file_name(self):
        name = build_iso.default_iso_name('1 2/../3')
        self.assertNotIn('/', name)
        self.assertNotIn(' ', name)
        self.assertTrue(name.startswith('ouatos-') and name.endswith('-installer-amd64.iso'))

    def test_brand_is_lowercase(self):
        self.assertEqual(build_iso.default_iso_name('1'), 'ouatos-1-installer-amd64.iso')
        self.assertTrue(build_iso.default_iso_name('1').islower())


class ImageDirTests(unittest.TestCase):
    def make(self, root, dirs):
        for d in dirs:
            (root / d).mkdir(parents=True, exist_ok=True)

    def test_complete_image_passes_silently(self):
        with tempfile.TemporaryDirectory() as td:
            self.make(Path(td), build_iso.REQUIRED_DIRS + build_iso.WANTED_DIRS)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                build_iso.check_image_dirs(td)
            self.assertEqual(out.getvalue(), '')

    def test_missing_required_directories_are_listed(self):
        with tempfile.TemporaryDirectory() as td:
            self.make(Path(td), ['dev', 'proc', 'boot'])
            with self.assertRaises(SystemExit) as cm:
                build_iso.check_image_dirs(td)
            msg = str(cm.exception)
            for d in ('/sys', '/run', '/tmp'):
                self.assertIn(d, msg)
            self.assertNotIn('/dev,', msg)

    def test_a_symlink_to_the_host_does_not_count_as_a_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make(root, [d for d in build_iso.REQUIRED_DIRS if d != 'run'])
            (root / 'run').symlink_to('/run')              # re-rooted: /run does not exist inside the image
            with self.assertRaisesRegex(SystemExit, '/run'):
                build_iso.check_image_dirs(td)

    def test_missing_efi_mount_point_only_warns(self):
        with tempfile.TemporaryDirectory() as td:
            self.make(Path(td), build_iso.REQUIRED_DIRS)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                build_iso.check_image_dirs(td)
            self.assertIn('/boot/efi is missing', out.getvalue())


if __name__ == '__main__':
    unittest.main()
