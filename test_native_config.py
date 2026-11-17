from pathlib import Path
from types import SimpleNamespace
import struct
import tempfile
import unittest
from unittest.mock import Mock,patch

import trainer
from compatibility import resolve_native,NativeLayout
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

    def test_native_trainer_enables_config_without_legacy_player_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/trainer.PROCESS_NAME;path.write_bytes(b'image')
            process=Mock(pid=123)
            process.exe.return_value=str(path)
            process.create_time.return_value=456
            process.is_running.return_value=True
            process.memory_maps.return_value=[SimpleNamespace(addr='0-10000',path=str(path))]
            memory=FakeMemory();memory.close=Mock()
            memory.put(0x5000,bytes(0x234));memory.put(0x51F8,struct.pack('<f',1))
            profile=NativeLayout('abc',0x5000,{0x123:b'ok'})
            memory.put(0x123,b'ok')
            with (patch.object(trainer,'find_games',return_value=[process]),
                  patch.object(trainer,'resolve',side_effect=RuntimeError('Unsupported Reborn build: player metadata changed')),
                  patch.object(trainer,'resolve_native',return_value=profile),
                  patch.object(trainer,'Memory',return_value=memory),patch.object(trainer,'InstanceLock')):
                native=trainer.Trainer(journal=Path(directory)/'journal.json')
                try:
                    self.assertFalse(native.player_supported)
                    native.set('timescale',3);native.set('clock',20);native.set('rift_clock',1)
                    self.assertEqual((native.values()['timescale'],native.values()['clock'],native.values()['rift_clock']),(3,20,1))
                    with self.assertRaisesRegex(RuntimeError,'Full player layout'):Player(native)
                    native.restore()
                    self.assertEqual(native.values()['timescale'],1)
                finally:native.close()
                memory.close.assert_called_once()

    def test_non_compatibility_errors_do_not_trigger_native_fallback(self):
        process=Mock();process.exe.return_value='game.exe'
        with (patch.object(trainer,'find_games',return_value=[process]),
              patch.object(trainer,'resolve',side_effect=RuntimeError('Game file changed')),
              patch.object(trainer,'resolve_native') as fallback):
            with self.assertRaisesRegex(RuntimeError,'file changed'):trainer.Trainer()
            fallback.assert_not_called()
