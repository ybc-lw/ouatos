"""Regression tests for disk safety, media failures and build metadata."""
import io
import lzma
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / d) for d in ('installer', 'build')]
import engine
import fbgui
import rootfs_info
import mklive

class AuditTests(unittest.TestCase):
    def test_mapped_and_swap_partitions_are_busy(self):
        def paths(p):
            if p.endswith('/*/partition'): return ['/sys/block/sda/sda1/partition']
            if p.endswith('/sda1/holders/*'): return ['/sys/class/block/sda1/holders/dm-0']
            return []
        with patch.object(engine.glob, 'glob', side_effect=paths), patch.object(engine, '_rd', return_value=''):
            self.assertTrue(engine.disk_in_use('sda', {'/dev/dm-0'}))
        with patch.object(engine.glob, 'glob', return_value=[]), patch.object(engine, '_rd', return_value='Filename Type\n/dev/sda partition'):
            self.assertTrue(engine.disk_in_use('sda', set()))

    def test_hotplug_or_new_mount_rejected(self):
        d = engine.Disk('sda', 8 << 30, 512, '', '', False)
        d.identity = '12'
        other = engine.Disk('sda', 8 << 30, 512, '', '', False)
        other.identity = '13'
        with patch.object(engine, 'list_disks', return_value=[other]):
            with self.assertRaises(engine.InstallError): engine.validate_disk(d, None)
            other.identity = '12'
            other.reason = 'mounted'
            with self.assertRaises(engine.InstallError): engine.validate_disk(d, None)

    def test_extraction_signal_and_truncation_fail(self):
        for truncated in (False, True):
            with self.subTest(truncated=truncated), tempfile.TemporaryDirectory() as target:
                data = lzma.compress(b'payload')
                if truncated: data = data[:-8]
                src = Mock()
                src.open_payload.side_effect = lambda: io.BytesIO(data)
                src.payload_size.return_value = len(data)
                proc = Mock(stdin=io.BytesIO())
                proc.wait.return_value = -9 if not truncated else 0
                job = engine.Installer.__new__(engine.Installer)
                job.env = SimpleNamespace(target=target, media=src)
                job.root_dev = '/dev/mock'
                job._progress = Mock()
                with patch.object(engine, 'sh'), patch.object(engine.subprocess, 'Popen', return_value=proc):
                    with self.assertRaises(engine.InstallError): job.step_extract()
                self.assertTrue(proc.stdin.closed)
                proc.wait.assert_called_once()
                if truncated: proc.kill.assert_called_once()

    def test_motion_precedes_click(self):
        inp = fbgui.EvdevInput.__new__(fbgui.EvdevInput)
        inp.w = inp.h = 1000
        inp.x = inp.y = 0
        inp.accel = 1
        dev = fbgui._Dev(-1, '', '', {'mouse'})
        events = [(2,0,100), (2,1,200), (0,0,0), (1,0x110,1), (1,0x110,0)]
        self.assertEqual(inp._parse(dev, b''.join(fbgui._EV.pack(0,0,*e) for e in events)),
                         [('move',100,200), ('down',1), ('up',1)])

    def test_ten_bit_framebuffer(self):
        scr = fbgui.Screen(1,1,32,4,((20,10),(10,10),(0,10)))
        scr.fill(0,0,1,1,(255,128,0))
        self.assertEqual(scr.to_ppm(), bytes((255,128,0)))

    def test_large_os_release(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td)/'rootfs.tar.xz'
            with tarfile.open(archive,'w:xz') as tf:
                data = b'NAME=ouatos\n#' + b'x'*512
                entry = tarfile.TarInfo('usr/lib/os-release'); entry.size=len(data)
                tf.addfile(entry,io.BytesIO(data))
            self.assertEqual(rootfs_info.make_rootfs_info(archive)['os_release']['NAME'], 'ouatos')

    def test_initrd_modules_from_compressed_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'boot').mkdir(); live=root/'live'; live.mkdir()
            module=live/'usr/lib/modules/test/kernel/drivers/demo.ko'
            module.parent.mkdir(parents=True); module.write_bytes(b'module')
            mklive.write_cpio_gz(str(live), str(root/'boot/initrd.img-test'))
            self.assertEqual(mklive.initrd_modules(td,'test'), ['kernel/drivers/demo.ko'])
            # Early uncompressed cpio precedes the compressed main archive.
            import gzip
            early=root/'early'; early.mkdir()
            prefix=root/'early.gz'
            mklive.write_cpio_gz(str(early), str(prefix))
            image=root/'boot/initrd.img-test'
            image.write_bytes(gzip.decompress(prefix.read_bytes()) + image.read_bytes())
            self.assertEqual(mklive.initrd_modules(td,'test'), ['kernel/drivers/demo.ko'])

if __name__ == '__main__': unittest.main()
