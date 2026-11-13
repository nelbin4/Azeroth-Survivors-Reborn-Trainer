import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

import trainer


class FakeMemory:
    def __init__(self):
        self.data = {}
        self.writes = []
        for rva, raw in trainer.SIGNATURES.items():
            self.put(rva, raw)
        self.put(trainer.CONFIG_RVA, bytes(0x234))
        self.put(trainer.CONFIG_RVA + 0x1F8, struct.pack('<f', 1))

    def put(self, address, raw):
        self.data.update({address + i: byte for i, byte in enumerate(raw)})

    def read(self, address, size):
        return bytes(self.data.get(address + i, 0) for i in range(size))

    def write(self, address, raw):
        self.writes.append((address, raw))
        self.put(address, raw)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.t = trainer.Trainer.__new__(trainer.Trainer)
        self.t.process = Mock(pid=123)
        self.t.process.is_running.return_value = True
        self.t.process.create_time.return_value = 456
        self.t.process.exe.return_value = 'test.exe'
        self.t.created = 456
        self.t.path = 'test.exe'
        self.t.base = 0
        self.t.memory = FakeMemory()
        self.t.journal = Path(self.temp.name) / 'recovery.json'
        self.t.changes = {}
        self.t.lock = None

    def test_write_journal_precedes_write_and_restores(self):
        real_write = self.t.memory.write

        def checked_write(address, raw):
            self.assertTrue(self.t.journal.exists())
            real_write(address, raw)

        self.t.memory.write = checked_write
        self.t.set('god', 1)
        self.assertEqual(self.t.values()['god'], 1)
        self.t.restore()
        self.assertEqual(self.t.values()['god'], 0)
        self.assertFalse(self.t.changes)

    def test_restore_keeps_external_change(self):
        self.t.set('timescale', 2)
        self.t.memory.put(trainer.CONFIG_RVA + 0x1F8, struct.pack('<f', 3))
        self.t.restore()
        self.assertEqual(self.t.values()['timescale'], 3)

    def test_repeated_toggle_preserves_original(self):
        self.t.set('god', 1)
        self.t.set('god', 0)
        self.t.set('god', 1)
        self.t.restore()
        self.assertEqual(self.t.values()['god'], 0)

    def test_external_change_blocks_further_write(self):
        self.t.set('timescale', 2)
        self.t.memory.put(trainer.CONFIG_RVA + 0x1F8, struct.pack('<f', 3))
        with self.assertRaises(RuntimeError):
            self.t.set('timescale', 4)
        self.assertEqual(len(self.t.memory.writes), 1)

    def test_process_reuse_blocks_write(self):
        self.t.process.create_time.return_value = 789
        with self.assertRaises(RuntimeError):
            self.t.set('god', 1)
        self.assertEqual(self.t.memory.writes, [])

    def test_instruction_mismatch_blocks_write(self):
        self.t.memory.put(next(iter(trainer.SIGNATURES)), b'\xcc')
        with self.assertRaises(RuntimeError):
            self.t.set('god', 1)
        self.assertEqual(self.t.memory.writes, [])

    def test_uninitialized_config_blocks_write(self):
        self.t.memory.put(trainer.CONFIG_RVA + 0x230, struct.pack('<I', 3))
        with self.assertRaises(RuntimeError):
            self.t.set('god', 1)
        self.assertEqual(self.t.memory.writes, [])

    def test_bad_flag_blocks_write(self):
        self.t.memory.put(trainer.CONFIG_RVA + 0x208, b'\xff')
        with self.assertRaises(RuntimeError):
            self.t.set('autopick', 1)
        self.assertEqual(self.t.memory.writes, [])

    def test_invalid_values_rejected(self):
        for key, value in [('god', 2), ('timescale', math.nan), ('timescale', 11), ('clock', -1), ('clock', 61)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.t.set(key, value)
        self.assertEqual(self.t.memory.writes, [])

    def test_clock_uses_seconds(self):
        self.t.set('clock', 5)
        self.assertEqual(self.t.memory.read(trainer.CONFIG_RVA + 0x1FC, 4), struct.pack('<f', 300))
        self.assertEqual(self.t.values()['clock'], 5)

    def test_unchanged_setting_does_not_write(self):
        self.t.set('god', 0)
        self.assertEqual(self.t.memory.writes, [])
        self.assertFalse(self.t.journal.exists())

    def test_unknown_build_rejected_before_opening_memory(self):
        path = Path(self.temp.name) / trainer.PROCESS_NAME
        path.write_bytes(b'MZ unsupported game')
        process = Mock()
        process.exe.return_value = str(path)
        with patch.object(trainer, 'find_games', return_value=[process]), patch.object(trainer, 'Memory') as memory:
            with self.assertRaisesRegex(RuntimeError, 'Unsupported Reborn build'):
                trainer.Trainer(writable=False)
            memory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
