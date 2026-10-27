"""Text confirmation regressions: disk and installation operations are mocked."""
import contextlib
import io
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'installer'))
import engine
import installer


class TextConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.frontend = installer.TextFrontend(SimpleNamespace(media=None),
            dict(os_release={}, system_users=['root'], system_groups=['root']))
        self.disks = [engine.Disk('sda', 8 * engine.GiB, 512, 'A', '', False),
                      engine.Disk('sdb', 8 * engine.GiB, 512, 'B', '', False)]

    def run_with(self, answers, scan):
        self.frontend.ask = Mock(side_effect=answers)
        with patch.object(installer, 'list_disks', scan), \
             patch.object(engine, 'Installer') as job, \
             patch('getpass.getpass', return_value='secret'), \
             contextlib.redirect_stdout(io.StringIO()):
            result = self.frontend.run()
        return result, job

    def test_wrong_confirmation_restarts_options_and_uses_final_selection(self):
        answers = []
        for confirmation in ('yes', 'YSE', ''):
            answers.extend(['1', 'first', 'alice', '', confirmation])
        answers.extend(['2', 'final', 'bob', 'Bob', 'YES'])
        scan = Mock(return_value=self.disks)
        result, job = self.run_with(answers, scan)
        self.assertEqual(result, 0)
        self.assertEqual(scan.call_count, 4)
        job.assert_called_once()
        cfg = job.call_args.args[0]
        self.assertIs(cfg.disk, self.disks[1])
        self.assertEqual((cfg.hostname, cfg.username), ('final', 'bob'))
        job.return_value.run.assert_called_once()

    def test_repeated_mistakes_do_not_recurse(self):
        answers = ['1', 'host', 'alice', '', 'no'] * 1100
        answers.extend(['1', 'host', 'alice', '', 'YES'])
        scan = Mock(return_value=self.disks)
        result, job = self.run_with(answers, scan)
        self.assertEqual(result, 0)
        self.assertEqual(scan.call_count, 1101)
        job.assert_called_once()

    def test_missing_disks_after_restart_never_starts_installation(self):
        scan = Mock(side_effect=[self.disks, []])
        result, job = self.run_with(['1', 'host', 'alice', '', 'no'], scan)
        self.assertEqual(result, 1)
        job.assert_not_called()


if __name__ == '__main__':
    unittest.main()
