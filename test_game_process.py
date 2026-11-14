from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import psutil

from game_process import GameLauncher, GameSession, PROCESS_NAME
import trainer


def process(pid, path, created=100):
    item = Mock(pid=pid, info={'name': PROCESS_NAME})
    item.create_time.return_value = created
    item.exe.return_value = str(Path(path).resolve())
    item.is_running.return_value = True
    return item


class GameProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.exe = Path(self.temp.name).resolve() / PROCESS_NAME
        self.exe.write_bytes(b'test fixture; never executed')
        self.first = process(123, self.exe)
        self.second = process(456, self.exe, 200)

    def test_launch_pins_returned_pid_and_working_directory(self):
        launcher = GameLauncher()
        with patch('game_process.psutil.process_iter', return_value=[]), \
                patch('game_process.subprocess.Popen', return_value=Mock(pid=123, poll=Mock(return_value=None))) as popen, \
                patch('game_process.psutil.Process', return_value=self.first):
            session = launcher.launch(self.exe)
        popen.assert_called_once_with([str(self.exe)], cwd=str(self.exe.parent))
        self.assertEqual(session.pid, 123)
        with patch('game_process.psutil.process_iter', return_value=[self.first, self.second]):
            self.assertEqual(launcher.refresh(), session)

    def test_running_process_blocks_duplicate_launch_before_spawning(self):
        with patch('game_process.psutil.process_iter', return_value=[self.first]), \
                patch('game_process.subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                GameLauncher().launch(self.exe)
        popen.assert_not_called()

    def test_ambiguous_instances_do_not_select_arbitrary_pid(self):
        launcher = GameLauncher()
        with patch('game_process.psutil.process_iter', return_value=[self.first, self.second]):
            self.assertIsNone(launcher.refresh(str(self.exe)))
        self.assertTrue(launcher.running)

    def test_selected_installation_disambiguates_and_stale_path_falls_back_to_unique_process(self):
        other = process(789, self.exe.parent / 'other' / PROCESS_NAME)
        launcher = GameLauncher()
        with patch('game_process.psutil.process_iter', return_value=[self.first, other]):
            self.assertEqual(launcher.refresh(self.exe).pid, 123)
        launcher.session = None
        with patch('game_process.psutil.process_iter', return_value=[other]):
            self.assertEqual(launcher.refresh(self.exe).pid, 789)

    def test_pid_reuse_exit_and_changed_executable_cannot_be_stopped(self):
        session = GameSession.capture(self.first)
        for method, value in (('create_time', 101), ('exe', 'different.exe'), ('is_running', False)):
            current = process(123, self.exe)
            getattr(current, method).return_value = value
            with patch('game_process.psutil.Process', return_value=current):
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    session.stop()
            current.terminate.assert_not_called()

    def test_stop_targets_only_the_validated_process(self):
        session = GameSession.capture(self.first)
        with patch('game_process.psutil.Process', return_value=self.first) as lookup:
            session.stop()
        lookup.assert_called_once_with(123)
        self.first.terminate.assert_called_once_with()

    def test_access_denied_blocks_run_but_does_not_enable_stop(self):
        inaccessible = process(123, self.exe)
        inaccessible.exe.side_effect = psutil.AccessDenied(123)
        launcher = GameLauncher()
        with patch('game_process.psutil.process_iter', return_value=[inaccessible]), \
                patch('game_process.subprocess.Popen') as popen:
            self.assertIsNone(launcher.refresh())
            self.assertTrue(launcher.running)
            with self.assertRaises(RuntimeError):
                launcher.launch(self.exe)
        popen.assert_not_called()

    def test_exited_process_clears_selection_and_allows_run_again(self):
        launcher = GameLauncher()
        with patch('game_process.psutil.process_iter', return_value=[self.first]):
            launcher.refresh()
        with patch('game_process.psutil.process_iter', return_value=[]):
            self.assertIsNone(launcher.refresh())
        self.assertFalse(launcher.running)

    def test_invalid_executable_and_failed_spawn_do_not_pin_a_pid(self):
        launcher = GameLauncher()
        with patch('game_process.psutil.process_iter', return_value=[]), \
                patch('game_process.subprocess.Popen') as popen:
            for invalid in (self.exe.parent / 'missing.exe', self.exe.parent / 'wrong.exe'):
                with self.assertRaises(ValueError):
                    launcher.launch(invalid)
            popen.assert_not_called()
            popen.side_effect = OSError('cannot start')
            with self.assertRaises(OSError):
                launcher.launch(self.exe)
        self.assertIsNone(launcher.session)

    def test_startup_exit_does_not_leave_a_pinned_session(self):
        launcher = GameLauncher()
        with patch('game_process.psutil.process_iter', return_value=[]), \
                patch('game_process.subprocess.Popen', return_value=Mock(pid=123, poll=Mock(return_value=1))), \
                patch('game_process.psutil.Process', return_value=self.first):
            with self.assertRaisesRegex(RuntimeError, 'exited during startup'):
                launcher.launch(self.exe)
        self.assertIsNone(launcher.session)

    def test_trainer_rejects_stale_selected_pid_before_layout_or_memory_access(self):
        session = GameSession.capture(self.first)
        self.first.create_time.return_value = 999
        with patch('game_process.psutil.Process', return_value=self.first), \
                patch('trainer.resolve') as resolve, patch('trainer.Memory') as memory:
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                trainer.Trainer(self.exe, session=session)
        resolve.assert_not_called()
        memory.assert_not_called()
