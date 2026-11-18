import struct
import unittest
from unittest.mock import Mock, patch

from player import Player, STATS, PLAYER_SIGNATURES, NAME_OFFSET
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
        with patch('player.discover', return_value=[candidate, dict(candidate, hero='0x40000')]), \
                self.assertRaisesRegex(RuntimeError, '2 distinct'):
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
