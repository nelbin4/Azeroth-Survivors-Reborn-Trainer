import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import updater


def executable():
    raw = bytearray(1024)
    raw[:2] = b'MZ'
    raw[60:64] = (128).to_bytes(4, 'little')
    raw[128:134] = b'PE\0\0\x64\x86'
    return bytes(raw)


def release(raw=None):
    raw = raw or executable()
    return {'tag_name': 'v0.7.0', 'assets': [{'name': 'Azeroth.Survivor.Reborn.Trainer.exe',
            'browser_download_url': f'https://github.com/{updater.REPOSITORY}/releases/download/v0.7.0/trainer.exe',
            'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'size': len(raw)}]}


class Response(io.BytesIO):
    def geturl(self):
        return 'https://release-assets.githubusercontent.com/file.exe'


class UpdaterTests(unittest.TestCase):
    def test_semantic_comparison_not_lexical(self):
        self.assertGreater(updater.version_tuple('v0.10.0'), updater.version_tuple('0.9.9'))
        self.assertTrue(updater.parse_release(release(), '0.6.0')['available'])
        self.assertFalse(updater.parse_release(release(), '0.7.0')['available'])

    def test_reject_wrong_repository_digest_size_and_unstable_versions(self):
        for change in ({'browser_download_url': 'https://example.com/app.exe'},
                       {'digest': 'sha256:bad'}, {'size': updater.MAX_DOWNLOAD + 1}):
            item = release()
            item['assets'][0].update(change)
            with self.assertRaises(ValueError):
                updater.parse_release(item, '0.6.0')
        for change in ({'draft': True}, {'prerelease': True}, {'tag_name': '../bad'}):
            item = release()
            item.update(change)
            with self.assertRaises(ValueError):
                updater.parse_release(item, '0.6.0')

    def test_download_verified_executable_only(self):
        item = updater.parse_release(release(), '0.6.0')
        with tempfile.TemporaryDirectory() as temp, patch('updater.request', return_value=Response(executable())):
            result = updater.download(item, temp)
            self.assertEqual(result.read_bytes(), executable())

    def test_corrupt_truncated_oversized_and_non_executable_downloads_fail(self):
        for raw in (b'bad', executable() + b'extra', b'x' * 1024):
            item = updater.parse_release(release(), '0.6.0')
            with tempfile.TemporaryDirectory() as temp, patch('updater.request', return_value=Response(raw)):
                with self.assertRaises(ValueError):
                    updater.download(item, temp)
        raw = b'x' * 1024
        item = updater.parse_release(release(raw), '0.6.0')
        with tempfile.TemporaryDirectory() as temp, patch('updater.request', return_value=Response(raw)):
            with self.assertRaisesRegex(ValueError, 'Windows x64'):
                updater.download(item, temp)

    def test_unexpected_redirect_fails(self):
        response = Response(executable())
        response.geturl = lambda: 'https://attacker.example/file'
        with tempfile.TemporaryDirectory() as temp, patch('updater.request', return_value=response):
            with self.assertRaisesRegex(ValueError, 'redirect'):
                updater.download(updater.parse_release(release(), '0.6.0'), temp)

    def test_installer_scope_quoting_and_backup(self):
        with tempfile.TemporaryDirectory(prefix="trainer's-") as temp:
            root = Path(temp)
            staged = root / '.updates/update.download'
            staged.parent.mkdir()
            staged.write_bytes(executable())
            cmd = updater.install_command(staged, root / 'Trainer.exe', 123)
            script = cmd[-1]
            self.assertIn("trainer''s-", script)
            self.assertIn('previous.exe', script)
            self.assertIn('Get-Process -Id 123', script)
            self.assertIn('[IO.File]::Copy', script)
            self.assertIn('-PassThru', script)
            self.assertIn('HasExited', script)
            self.assertIn('$p.Path', script)
            self.assertIn("$env:PYINSTALLER_RESET_ENVIRONMENT='1'", script)
            with self.assertRaises(ValueError):
                updater.install_command(staged, root / 'elsewhere/Trainer.exe', 123)

    def test_restart_environment_removes_deleted_runtime_metadata_and_preserves_settings(self):
        inherited = {'_PYI_APPLICATION_HOME_DIR': 'deleted-runtime', '_PYI_PARENT_PROCESS_LEVEL': '1',
                     '_PYI_ARCHIVE_FILE': 'Trainer.exe', '_MEIPASS2': 'old-runtime',
                     'APPDATA': 'user-settings', 'PATH': 'system-path'}
        with patch.dict('updater.os.environ', inherited, clear=True):
            environment = updater.restart_environment()
            self.assertEqual(environment['PYINSTALLER_RESET_ENVIRONMENT'], '1')
            self.assertEqual(environment['APPDATA'], 'user-settings')
            self.assertEqual(environment['PATH'], 'system-path')
            self.assertFalse(any(k.startswith('_PYI_') or k == '_MEIPASS2' for k in environment))
            self.assertEqual(updater.os.environ['_PYI_APPLICATION_HOME_DIR'], 'deleted-runtime')

    def test_frozen_update_helper_receives_fresh_restart_environment(self):
        with patch.object(updater.sys, 'frozen', True, create=True), \
                patch('updater.install_command', return_value=['helper']), \
                patch('updater.subprocess.Popen', return_value=Mock()) as popen, \
                patch.dict('updater.os.environ', {'_PYI_APPLICATION_HOME_DIR': 'deleted-runtime'}):
            updater.launch_install(Path('staged.download'))
        environment = popen.call_args.kwargs['env']
        self.assertEqual(environment['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        self.assertNotIn('_PYI_APPLICATION_HOME_DIR', environment)
