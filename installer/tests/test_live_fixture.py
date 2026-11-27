"""Media fixture regressions: synthetic input, no mounts or block devices."""
import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_live


class LiveFixtureTests(unittest.TestCase):
    def test_private_medium_preserves_input_at_old_destination(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'live/tmp/ouatos-rootfs.tar.xz'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'synthetic archive')
            with patch.object(test_live, 'TAR', str(source)), \
                 patch.object(test_live, 'ISO', None), \
                 patch.object(test_live, 'make_rootfs_info', return_value={'os_release': {'NAME': 'test'}}):
                with test_live.prepare_media() as first, test_live.prepare_media() as second:
                    self.assertNotEqual(first, second)
                    payload = Path(first)/'ouatos/rootfs.txz'
                    self.assertFalse(payload.is_symlink())
                    self.assertEqual(payload.read_bytes(), source.read_bytes())
                    self.assertEqual(json.loads((Path(first)/'ouatos/rootfsinfo.json').read_text())['os_release']['NAME'], 'test')
                self.assertFalse(Path(first).exists())
                self.assertFalse(Path(second).exists())
            self.assertEqual(source.read_bytes(), b'synthetic archive')

    def test_copy_fallback_and_failure_cleanup_preserve_source(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td)/'custom.tar.xz'
            source.write_bytes(b'archive')
            with patch.object(test_live, 'TAR', str(source)), \
                 patch.object(test_live, 'ISO', None), \
                 patch.object(test_live, 'make_rootfs_info', return_value={}), \
                 patch.object(test_live.os, 'link', side_effect=OSError(errno.EXDEV, 'cross-device')):
                with self.assertRaisesRegex(RuntimeError, 'installation failed'):
                    with test_live.prepare_media() as medium:
                        self.assertEqual((Path(medium)/'ouatos/rootfs.txz').read_bytes(), b'archive')
                        raise RuntimeError('installation failed')
                self.assertFalse(Path(medium).exists())
            self.assertEqual(source.read_bytes(), b'archive')


if __name__ == '__main__':
    unittest.main()
