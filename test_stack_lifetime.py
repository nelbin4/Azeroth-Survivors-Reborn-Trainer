import unittest
from unittest.mock import Mock,patch
import stack_probe


class StackLifetimeTests(unittest.TestCase):
    def test_exited_thread_without_teb_is_skipped_and_handle_closed(self):
        kernel=Mock();kernel.OpenThread.return_value=123
        ntdll=Mock();ntdll.NtQueryInformationThread.return_value=0
        process=Mock();process.threads.return_value=[Mock(id=7)]
        memory=Mock()
        with patch.object(stack_probe.ctypes,'WinDLL',side_effect=[kernel,ntdll]):
            self.assertEqual(list(stack_probe.stacks(process,memory)),[])
        memory.read.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)
