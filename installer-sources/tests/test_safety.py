"""Safety regressions; all disk access and installation commands are mocked."""
import contextlib
import errno
import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'installer'))
import engine
import installer


class SafetyTests(unittest.TestCase):
    def test_busy_partition_table_never_accepts_old_device_nodes(self):
        with patch.object(engine.os, 'open', return_value=42), \
             patch.object(engine.os, 'close') as close, \
             patch.object(engine.fcntl, 'ioctl', side_effect=OSError(errno.EBUSY, 'busy')) as ioctl, \
             patch.object(engine.time, 'sleep'), \
             patch.object(engine.os.path, 'exists', return_value=True) as exists:
            with self.assertRaises(engine.InstallError):
                engine.KernelPartitions().rescan(SimpleNamespace(path='/dev/sda'), [SimpleNamespace(number=1)])
            self.assertEqual(ioctl.call_count, 50)
            exists.assert_not_called()
            close.assert_called_once_with(42)

    def test_transient_busy_can_recover(self):
        with patch.object(engine.os, 'open', return_value=42), \
             patch.object(engine.os, 'close'), \
             patch.object(engine.fcntl, 'ioctl', side_effect=[OSError(errno.EBUSY, 'busy'), 0]) as ioctl, \
             patch.object(engine.time, 'sleep'), \
             patch.object(engine.os.path, 'exists', return_value=True):
            engine.KernelPartitions().rescan(SimpleNamespace(path='/dev/sda'), [SimpleNamespace(number=1)])
            self.assertEqual(ioctl.call_count, 2)

    def test_group_conflict_stops_engine_before_partitioning(self):
        info = dict(os_release={}, system_users=['root'], system_groups=['root', 'audio'])
        cfg = engine.Config(SimpleNamespace(path='/dev/sda'), 'host', 'audio', '', 'secret')
        with patch.object(engine, 'load_media_info', return_value=info), patch.object(engine, 'log'):
            job = engine.Installer(cfg, SimpleNamespace(media=None, uefi=False))
            with patch.object(job, 'step_prepare_disk') as prepare, patch.object(job, 'cleanup'):
                with self.assertRaises(engine.InstallError):
                    job.run()
                prepare.assert_not_called()
        self.assertEqual(engine.check_username('alice', ['root'], ['audio']), '')

    def test_gui_rejects_group_name_on_account_page(self):
        wizard = installer.Wizard.__new__(installer.Wizard)
        wizard.page = 'account'
        wizard.info = dict(system_users=['root'], system_groups=['audio'])
        wizard.user = SimpleNamespace(text='audio')
        wizard.pw = wizard.pw2 = SimpleNamespace(text='secret')
        wizard.ui = SimpleNamespace(focus=None)
        wizard.goto = Mock()
        wizard.go_next()
        self.assertTrue(wizard.err)
        self.assertEqual(wizard.ui.focus, 'user')
        wizard.goto.assert_not_called()

    def test_text_reprompts_invalid_disk_numbers_and_group_names(self):
        disks = [engine.Disk('sda', 8 * engine.GiB, 512, 'A', '', False),
                 engine.Disk('sdb', 8 * engine.GiB, 512, 'B', '', False)]
        frontend = installer.TextFrontend(SimpleNamespace(media=None),
            dict(os_release={}, system_users=['root'], system_groups=['audio']))
        # Reject zero, negative, too-large and nonnumeric selections before choosing disk 1.
        frontend.ask = Mock(side_effect=['0', '-1', '3', 'bad', '1', 'host', 'audio', 'alice', '', 'YES'])
        with patch.object(installer, 'list_disks', return_value=disks), \
             patch('getpass.getpass', return_value='secret'), \
             patch.object(engine, 'Installer') as job, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(frontend.run(), 0)
        cfg = job.call_args.args[0]
        self.assertIs(cfg.disk, disks[0])
        self.assertEqual(cfg.username, 'alice')
        job.return_value.run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
