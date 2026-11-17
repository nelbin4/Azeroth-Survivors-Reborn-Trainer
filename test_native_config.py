from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

import trainer
from compatibility import resolve_native
from player import Player
from test_compatibility import fixture
from test_trainer import FakeMemory


class NativeConfigTests(unittest.TestCase):
    def test_native_discovery_does_not_require_player_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'game.exe'
            blob=fixture();blob[0x680:0x700]=bytes(128)
            path.write_bytes(blob)
            profile=resolve_native(path)
            self.assertEqual(profile.config,0x5000)
            self.assertTrue(profile.signatures)
            # A broken config anchor must still block its own controls.
            blob[0x600:0x620]=bytes(32);path.write_bytes(blob)
            with self.assertRaises(ValueError):resolve_native(path)

    def test_connection_allows_hunt_when_legacy_layout_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / trainer.PROCESS_NAME
            path.write_bytes(b'image')
            process = Mock(pid=123)
            process.exe.return_value = str(path)
            process.create_time.return_value = 456
            process.is_running.return_value = True
            process.memory_maps.return_value = [SimpleNamespace(addr='0-10000', path=str(path))]
            memory = FakeMemory()
            memory.close = Mock()
            with (patch.object(trainer, 'find_games', return_value=[process]),
                  patch.object(trainer, 'resolve', side_effect=RuntimeError('Unsupported Reborn build: player metadata changed')),
                  patch.object(trainer, 'Memory', return_value=memory)):
                connection = trainer.Trainer(writable=False)
                try:
                    self.assertIsNone(connection.profile)
                    self.assertFalse(connection.player_supported)
                    self.assertEqual(memory.writes, [])
                    with self.assertRaisesRegex(RuntimeError, 'Full player layout'):
                        Player(connection)
                finally:
                    connection.close()
                memory.close.assert_called_once()

    def test_connection_propagates_errors_other_than_unsupported_layout(self):
        process = Mock()
        process.exe.return_value = 'game.exe'
        with (patch.object(trainer, 'find_games', return_value=[process]),
              patch.object(trainer, 'resolve', side_effect=RuntimeError('Game file changed')),
              patch.object(trainer, 'Memory') as memory):
            with self.assertRaisesRegex(RuntimeError, 'file changed'):
                trainer.Trainer()
            memory.assert_not_called()
