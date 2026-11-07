"""Synthetic media and failure-cleanup regressions; no real disks or mounts."""
import io
import lzma
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / p) for p in ('installer', 'build')]
import engine
import media
import mklive
import rootfs_info
import fbgui
import test_live


class IntegrityTests(unittest.TestCase):
    def test_multistream_metadata_and_extraction(self):
        raw = io.BytesIO()
        files = [('usr/lib/os-release', b'NAME=ouatos\n'), ('boot/vmlinuz-test', b'kernel'),
                 ('usr/bin/bash', b'bash'), ('usr/bin/extra', b'required data')]
        with tarfile.open(fileobj=raw, mode='w') as tf:
            for name, data in files:
                entry = tarfile.TarInfo(name); entry.size = len(data)
                tf.addfile(entry, io.BytesIO(data))
        data = raw.getvalue()
        encoded = lzma.compress(data[:3072]) + lzma.compress(data[3072:])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); archive = root/'multi.tar.xz'; archive.write_bytes(encoded)
            info = rootfs_info.make_rootfs_info(archive)
            self.assertEqual(info['installed_bytes'], sum(len(d) for _, d in files))
            source = Mock(); source.payload_size.return_value = len(encoded)
            source.open_payload.side_effect = lambda: io.BytesIO(encoded)
            job = engine.Installer.__new__(engine.Installer)
            job.env = SimpleNamespace(target=str(root/'target'), media=source)
            job.root_dev = '/dev/mock'; job._progress = Mock()
            with patch.object(engine, 'sh'), patch.object(engine, 'log'):
                job.step_extract()
            for name, contents in files:
                self.assertEqual((root/'target'/name).read_bytes(), contents)
            archive.write_bytes(encoded[:-8])
            with self.assertRaises((EOFError, lzma.LZMAError)):
                rootfs_info.make_rootfs_info(archive)

    def test_malformed_iso_records(self):
        for raw in (b'\x01' + bytes(2047), bytes([34]) + bytes(31) + b'\xff\0' + bytes(2014)):
            reader = media.IsoReader.__new__(media.IsoReader)
            reader.f = io.BytesIO(raw); reader.nsectors = 1
            with self.assertRaises(media.MediaError): list(reader._entries(0, 2048))
        # NM must include a flags byte; a four-byte record used to index past EOF.
        record = bytearray(38); record[0] = 38; record[32] = 1; record[33] = ord('A')
        record[34:] = b'NM\x04\x01'
        reader.f = io.BytesIO(record + bytes(2048-len(record)))
        with self.assertRaises(media.MediaError): list(reader._entries(0, 2048))

    def test_truncated_extent_and_metadata(self):
        with media.ExtentReader(io.BytesIO(b'x'), 0, 2) as f:
            self.assertEqual(f.read(), b'x')
            with self.assertRaises(media.MediaError): f.read()
        src = Mock(); src.read_small.return_value = b'[]'
        self.assertEqual(engine.load_media_info(src)['os_release'], {})

    def test_config_validation_precedes_disk_changes(self):
        info = dict(os_release={}, system_users=[], system_groups=[])
        for hostname, password in [('bad host', 'secret'), ('host', 'bad\npassword')]:
            cfg = engine.Config(SimpleNamespace(path='/dev/mock'), hostname, 'alice', '', password)
            with patch.object(engine, 'load_media_info', return_value=info), patch.object(engine, 'log'):
                job = engine.Installer(cfg, SimpleNamespace(media=Mock(), uefi=False))
                with patch.object(job, 'step_prepare_disk') as prepare, patch.object(job, 'cleanup'):
                    with self.assertRaises(engine.InstallError): job.run()
                    prepare.assert_not_called()

    def test_stale_loop_record_is_not_used(self):
        with tempfile.TemporaryDirectory() as td:
            live = Path(td); (live/'tmp').mkdir()
            stale = live/'tmp/devname-bios'; stale.write_text('/dev/loop99')
            with patch.object(test_live, 'LIVE', td), \
                 patch.object(test_live.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['mount'])) as run:
                with self.assertRaises(subprocess.CalledProcessError): test_live.run_installs('/unused')
            self.assertFalse(any(c.args[0][0] == 'losetup' for c in run.call_args_list))
            self.assertEqual(stale.read_text(), '/dev/loop99')
            self.assertEqual(list((live/'tmp').iterdir()), [stale])

    def test_absolute_image_symlink_stays_inside_image(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)/'root'; live = Path(td)/'live'
            (root/'actual').mkdir(parents=True); live.mkdir()
            (root/'actual/item').write_bytes(b'image data')
            (root/'alias').symlink_to('/actual', target_is_directory=True)
            mklive.install_path(str(root), str(live), 'alias/item')
            self.assertEqual((live/'actual/item').read_bytes(), b'image data')
            (root/'loop').symlink_to('/loop')
            with self.assertRaises(OSError): mklive._resolve(str(root), 'loop/item')

    def test_click_in_scrolled_field(self):
        fonts = fbgui.FontSet(str(HERE.parent/'installer/font.bin'))
        ui = fbgui.UI(fbgui.Screen.virtual(300, 100), fonts, 1.0)
        field = fbgui.Field('abcdefghijklmnopqrstuvwxyz' * 3)
        field.cur = len(field.text)
        ui.field('name', 0, 0, 100, field)
        ui.prev = list(ui.cur)
        ui.mouse_down(12, 10)
        self.assertGreater(field.cur, 50)


if __name__ == '__main__': unittest.main()
