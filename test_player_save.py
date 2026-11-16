import struct
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from player import Player, STATS, PLAYER_SIGNATURES, NAME_OFFSET
from save_editor import GameData, backup_save, apply_upgrade
from test_trainer import FakeMemory


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.t = Mock(base=0)
        self.t.memory = FakeMemory()
        m = self.t.memory
        for rva, raw in PLAYER_SIGNATURES.items():
            m.put(rva, raw)
        m.put(0x10000 + 0xBA8, struct.pack('<3Q', 1, 0x20000, 1))
        m.put(0x10000 + 0xE60, struct.pack('<Q', 0))
        m.put(0x20000 + 0xF0, struct.pack('<3Q', 123, 50, 100))
        m.put(0x20000 + 0x150, struct.pack('<4I', 6, 5, 34, 22))
        m.put(0x20000 + NAME_OFFSET, struct.pack('<3Q', 4, 0x30000, 4))
        m.put(0x30000, b'hero')
        self.p = Player(self.t)
        self.p.lease = dict(game=hex(0x10000), hero=hex(0x20000), guid=123, name='hero', level=6)

    def test_maximum_hold_survives_recalculation_and_restores(self):
        self.p.maximize()
        self.assertEqual(self.p.snapshot()['hp'], 1000000)
        self.assertEqual(self.p.snapshot()['gold'], 99999999)
        self.assertEqual(self.p.snapshot()['xp'], 33)
        self.assertEqual(self.p.frozen, {k: s.maximum for k, s in STATS.items()})
        self.t.memory.put(0x20000 + STATS['damage'].offset, struct.pack('<I', 45))
        self.p.tick()
        self.assertEqual(self.p.snapshot()['values']['damage'], 9999)
        self.p.stop()
        self.assertEqual(self.p.snapshot()['values']['damage'], 45)
        self.assertEqual(self.p.snapshot()['maximum'], 100)
        self.assertEqual(self.p.snapshot()['hp'], 100)
        self.assertEqual(self.p.snapshot()['gold'], 99999999)
        self.assertFalse(self.p.xp_assist)

    def test_lowering_health_keeps_current_health_valid(self):
        self.p.maximize()
        self.p.set_stat('max_health', 200)
        self.assertEqual(self.p.snapshot()['hp'], 200)
        self.assertEqual(self.p.snapshot()['maximum'], 200)
        with self.assertRaises(ValueError):
            self.p.set_stat('max_health', 0)

    def test_native_level_and_gold_gains_do_not_disable_holds(self):
        self.p.maximize()
        self.t.memory.put(0x20000 + 0x150, struct.pack('<I', 7))
        self.t.memory.put(0x20000 + 0x100, struct.pack('<Q', 1000009))
        self.t.memory.put(0x20000 + 0x15C, struct.pack('<I', 100001312))
        result = self.p.tick()
        self.assertEqual(result['maximum'], 1000000)
        self.assertEqual(result['gold'], 99999999)
        self.assertEqual(result['xp'], result['next_xp'] - 1)

    def test_multiplayer_identity_and_new_run_block_writes(self):
        for offset, raw in [(0x10000 + 0xBA8, struct.pack('<3Q', 2, 0x20000, 2)),
                            (0x20000 + 0xF0, struct.pack('<Q', 456)),
                            (0x20000 + 0x150, struct.pack('<I', 1))]:
            old = self.t.memory.read(offset, len(raw))
            self.t.memory.put(offset, raw)
            with self.assertRaises(RuntimeError):
                self.p.maximize()
            self.assertEqual(self.t.memory.writes, [])
            self.t.memory.put(offset, old)

    def test_duplicate_references_are_one_hero_but_distinct_allocations_block(self):
        candidate = dict(self.p.lease, count=1, index=0)
        with patch('player.discover', return_value=[candidate, dict(candidate)]):
            self.assertEqual(self.p.connect()['guid'], 123)
        with patch('player.discover', return_value=[candidate, dict(candidate, hero='0x40000')]):
            with self.assertRaisesRegex(RuntimeError, '2 distinct'):
                self.p.connect()

    def test_unchanged_hold_tick_does_not_write(self):
        self.p.maximize()
        self.t.memory.writes.clear()
        self.p.tick()
        self.assertEqual(self.t.memory.writes, [])

    def test_named_field_mapping_writes_and_holds_reordered_damage(self):
        from compatibility import Layout
        fields = {k: s.offset - 0x1670 for k, s in STATS.items() if 0x1670 <= s.offset <= 0x16AC}
        fields['damage'], fields['crit'] = fields['crit'], fields['damage']
        self.t.profile = Layout('proof', 0, {}, 0x1900, 0xBA8, 0xE60, NAME_OFFSET,
                                0x1700, 0x1800, fields)
        player = Player(self.t)
        player.lease = dict(self.p.lease)
        player.hold('damage', 123)
        address = 0x20000 + 0x1700 + fields['damage']
        self.assertEqual(struct.unpack('<I', self.t.memory.read(address, 4))[0], 123)
        self.assertEqual(self.t.memory.read(0x20000 + STATS['damage'].offset, 4), bytes(4))
        self.t.memory.put(address, struct.pack('<I', 45))
        player.tick()
        self.assertEqual(player.snapshot()['values']['damage'], 123)
        self.assertEqual(player.snapshot()['values']['crit'], 0)
        player.stop()
        self.assertEqual(struct.unpack('<I', self.t.memory.read(address, 4))[0], 45)

    def test_larger_solo_vector_capacity_does_not_break_discovery(self):
        from stack_probe import discover
        with patch('stack_probe.stacks', return_value=[(1, 0x10000, 0x13000)]):
            for capacity in (8, 64):
                self.t.memory.put(0x10BA8, struct.pack('<3Q', capacity, 0x20000, 1))
                found = discover(self.t.process, self.t.memory)
                self.assertEqual(len(found), 1)
                self.assertEqual((found[0]['name'], found[0]['count']), ('hero', 1))
            self.t.memory.put(0x10BA8, struct.pack('<3Q', 65, 0x20000, 1))
            self.assertEqual(discover(self.t.process, self.t.memory), [])

    def test_larger_capacity_keeps_identity_and_multiplayer_write_guards(self):
        self.t.memory.put(0x10BA8, struct.pack('<3Q', 32, 0x20000, 1))
        self.p.set_stat('damage', 321)
        self.assertEqual(self.p.snapshot()['values']['damage'], 321)
        self.t.memory.writes.clear()
        self.t.memory.put(0x10BA8, struct.pack('<3Q', 32, 0x20000, 2))
        with self.assertRaises(RuntimeError):
            self.p.set_stat('damage', 9999)
        self.assertEqual(self.t.memory.writes, [])


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.g = GameData.__new__(GameData)
        self.g.stream_key = bytes(range(32))
        self.g.tag_key = bytes(range(32, 64))

    def test_envelope_authentication_and_payload_version(self):
        text = 'VER\t1\nC\taccount\t123\n'
        for flag in (0, 1):
            raw = self.g.seal(text, flag)
            self.assertEqual(self.g.unseal(raw), text)
            corrupt = bytearray(raw)
            corrupt[20] ^= 1
            with self.assertRaises(ValueError):
                self.g.unseal(corrupt)
        with self.assertRaises(ValueError):
            self.g.seal('VER\t2\n')

    def test_max_gear_uses_native_star_and_worn_syntax(self):
        assets = {f'base/classes/c{i}/class.toml': {'id': i} for i in range(1, 13)}
        assets.update({f'{p}/gear_tiers.toml': {'gear_tier': [{}]} for p in ('tbc', 'wotlk', 'cata')})
        assets['base/account/levels.toml'] = dict(max_level=30, xp_first=1000, xp_step=250)
        assets['achievements.toml'] = {'achievement': [{'key': f'a{i}'} for i in range(83)]}
        self.g.assets = lambda: iter(assets.items())
        identity = ['C', 'account', '123', 'hero'] + ['0'] * 8
        aggregate = ['A', 'account'] + ['0'] * 19
        aggregate[13] = ''
        text = '\n'.join('\t'.join(r) for r in [identity, aggregate, ['UNKNOWN', 'preserve'], ['W', 'account', '5', '', '']]) + '\n'
        result, summary = self.g.maximize(text)
        self.assertIn('\t'.join(identity) + '\n', result)
        self.assertIn('UNKNOWN\tpreserve\n', result)
        rows = [r.split('\t') for r in result.splitlines()]
        gear = [r for r in rows if r[0] == 'G']
        self.assertEqual(summary['gear'], 1080)
        self.assertEqual(len(gear), 12)
        for row in gear:
            entries = row[3].split(',')
            self.assertEqual(len(entries), 90)
            self.assertEqual(sum(e.endswith('*5!') for e in entries), 10)
            self.assertTrue(all('*5' in e for e in entries))
        again, _ = self.g.maximize(result)
        self.assertEqual(again, result)

    def test_backup_keeps_save_and_talent_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'UserData').mkdir()
            (root / 'WTF').mkdir()
            raw = self.g.seal('VER\t1\n')
            (root / 'UserData/survivors-save.tsv').write_bytes(raw)
            (root / 'WTF/Config.wtf').write_text('SET azsTalentsSHAMAN "5-0-0"')
            with patch('save_editor.GameData', return_value=self.g):
                backup = backup_save(root / 'game.exe', root / 'backup')
                self.assertEqual(Path(backup).read_bytes(), raw)
                self.assertEqual((root / 'backup/WTF/Config.wtf').read_text(), 'SET azsTalentsSHAMAN "5-0-0"')
                with self.assertRaises(FileExistsError):
                    backup_save(root / 'game.exe', root / 'backup')

    def test_apply_blocks_running_game(self):
        with patch('trainer.find_games', return_value=[Mock()]):
            with self.assertRaisesRegex(RuntimeError, 'Exit Reborn'):
                apply_upgrade('nonexistent.exe', 'upgrade', 'backup')


if __name__ == '__main__':
    unittest.main()
