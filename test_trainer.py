from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

import trainer


class SettingsTests(unittest.TestCase):
    def test_legacy_settings_are_migrated_and_new_settings_take_precedence(self):
        with TemporaryDirectory() as folder:
            legacy = Path(folder) / 'reborn-settings.json'
            current = Path(folder) / 'state' / legacy.name
            current.parent.mkdir()
            trainer.atomic_json(legacy, {'stats': {'enabled': ['crit']}})
            migrated = trainer.load_settings(current, legacy)
            self.assertEqual(migrated, {'stats': {'enabled': ['crit']}})
            trainer.atomic_json(legacy, {})
            self.assertEqual(trainer.load_settings(current, legacy), migrated)


class FakeMemory:
    def __init__(self):
        self.data = {}
        self.writes = []

    def put(self, address, raw):
        self.data.update({address + i: byte for i, byte in enumerate(raw)})

    def read(self, address, size):
        return bytes(self.data.get(address + i, 0) for i in range(size))

    def write(self, address, raw):
        self.writes.append((address, raw))
        self.put(address, raw)


class ConnectionTests(unittest.TestCase):
    def test_guard_rejects_an_exited_or_reused_process(self):
        connection = trainer.Trainer.__new__(trainer.Trainer)
        connection.memory = FakeMemory()
        connection.process = Mock()
        connection.process.is_running.return_value = True
        connection.process.create_time.return_value = 2
        connection.created = 1
        with self.assertRaisesRegex(RuntimeError, 'connection lost'):
            connection.guard()

    def test_guard_rejects_changed_legacy_instruction(self):
        connection = trainer.Trainer.__new__(trainer.Trainer)
        connection.memory = FakeMemory()
        connection.process = Mock()
        connection.process.is_running.return_value = True
        connection.process.create_time.return_value = connection.created = 1
        connection.process.exe.return_value = connection.path = 'game.exe'
        connection.base = 0x10000
        connection.signatures = {0x100: b'verified'}
        with self.assertRaisesRegex(RuntimeError, 'instruction mismatch'):
            connection.guard()
