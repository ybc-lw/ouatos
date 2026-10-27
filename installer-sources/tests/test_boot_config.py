"""GRUB embedded configs run before the normal-mode shell is available."""
import importlib.util
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'installer'), str(ROOT/'build')]
import engine
spec = importlib.util.spec_from_file_location('build_iso', ROOT/'build_iso.py')
build_iso = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_iso)


def run_early(config, match, initial='hd0,gpt1'):
    """Check the rescue-parser contract: newline-separated registered commands.

    Unlike normal GRUB configuration, embedded scripts have no if/then parser.
    Model search results to check root/prefix when boot and system disks differ.
    """
    variables = dict(root=initial, prefix='(%s)/boot/grub' % initial)
    for line in config.splitlines():
        words = shlex.split(line)
        if not words or words[0].startswith('#'):
            continue
        if words[0] == 'search':
            if words[1:-1] not in (['--no-floppy','--fs-uuid','--set=root'],
                                  ['--no-floppy','--label','--set=root']):
                raise AssertionError('Unexpected search arguments')
            variables['root'] = match[words[-1]]
        elif words[0] == 'set' and len(words) == 2:
            key, value = words[1].split('=', 1)
            variables[key] = value.replace('$root', variables['root'])
        else:
            raise AssertionError('Unsupported early command: ' + words[0])
    return variables


class BootConfigTests(unittest.TestCase):
    def test_installed_root_is_found_independent_of_efi_disk(self):
        config = engine.grub_early_config('0123-abcd')
        for root in ('hd0,gpt2', 'hd1,gpt2'):
            variables = run_early(config, {'0123-abcd':root})
            self.assertEqual(variables['root'], root)
            self.assertEqual(variables['prefix'], '(%s)/boot/grub' % root)

    def test_installer_medium_is_found_by_label(self):
        variables = run_early(build_iso.EARLY_CFG % engine.ISO_LABEL,
                              {engine.ISO_LABEL:'cd0'})
        self.assertEqual(variables['root'], 'cd0')
        self.assertEqual(variables['prefix'], '(cd0)/boot/grub')

    def test_old_conditional_is_rejected_by_contract(self):
        with self.assertRaisesRegex(AssertionError, 'Unsupported early command: if'):
            run_early('if search --fs-uuid --set=root uuid; then\nset prefix=($root)/boot/grub\nfi\n', {})

    def test_engine_writes_correct_embedded_config_before_efi_build(self):
        job = engine.Installer.__new__(engine.Installer)
        job.env = SimpleNamespace(target='/mock', uefi=True)
        job.cfg = SimpleNamespace(disk=None)
        root = SimpleNamespace(kind='root', fs_uuid='0123-abcd')
        job.parts = [root]; job.osr = {}; job.ch = Mock(); job._progress = Mock()
        job._write = Mock(); job._grub_cfg = Mock()
        with patch.object(job, '_install_efi') as install:
            job.step_bootloader()
        install.assert_called_once_with(root)
        job._write.assert_any_call('tmp/ouatos-early.cfg', engine.grub_early_config('0123-abcd'))


if __name__ == '__main__': unittest.main()
