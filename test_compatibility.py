from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from compatibility import resolve, _resolve


def fixture(stride=0x1858, stats=0x1740, rerolls=0x1804):
    blob = bytearray(0x2400)
    blob[:2] = b'MZ'
    struct.pack_into('<I', blob, 60, 0x80)
    blob[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HH', blob, 0x84, 0x8664, 2)
    struct.pack_into('<H', blob, 0x94, 0xF0)
    for i, (name, rva, offset) in enumerate([(b'.text', 0x1000, 0x400), (b'.data', 0x5000, 0x1400)]):
        struct.pack_into('<8sIIII', blob, 0x188 + i * 40, name, 0x1000, rva, 0x1000, offset)
    code = bytearray(b'\x90' * 0x1000)
    for pos in (0x100, 0x110):
        code[pos:pos + 7] = b'\x0f\xb6\x35' + struct.pack('<i', 0x5208 - (0x1000 + pos + 7))
    def put(pos, raw):
        code[pos:pos + len(raw)] = raw
    put(0x200, bytes.fromhex('0f958208020000'))
    put(0x220, bytes.fromhex('0fb68208020000413a8008020000'))
    u = lambda value: struct.pack('<I', value)
    put(0x280, b'\x48\x8b\x89' + u(0xE38) + b'\x49\x8b\x95' + u(0xB90)
        + b'\x48\x39\xd1\x0f\x83' + u(100) + b'\x49\x8b\x95' + u(0xB88)
        + b'\x48\x69\xc9' + u(stride) + b'\x48\x8d\x1c\x0a\x0f\x10\x84\x0a' + u(stats)
        + b'\xf3\x0f\x6f\x8c\x0a' + u(stats + 16)
        + b'\xf3\x0f\x6f\x94\x0a' + u(stats + 32)
        + b'\xf3\x0f\x6f\x9c\x0a' + u(stats + 48))
    put(0x380, bytes.fromhex('488d875c010000488d8f') + u(0xED8)
        + bytes.fromhex('488d97500100004c8d87') + u(0x1810))
    put(0x400, b'\x48\x81\xc7' + u(rerolls))
    put(0x580, b'\x8b\x88' + u(stats + 8))
    blob[0x400:0x1400] = code
    return blob


class CompatibilityTests(unittest.TestCase):
    def resolve_blob(self, blob):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'game.exe'
            path.write_bytes(blob)
            return resolve(path)

    def test_discovers_new_layout_and_ignores_unrelated_build_changes(self):
        blob = fixture()
        first = self.resolve_blob(blob)
        blob[0x2300] = 17
        second = self.resolve_blob(blob)
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(second.config, 0x5000)
        self.assertEqual((second.stride, second.name, second.stats, second.rerolls),
                         (0x1858, 0x10E0, 0x1740, 0x1804))
        self.assertEqual((second.vector, second.index), (0xB80, 0xE38))

    def test_ambiguous_anchor_blocks_discovery(self):
        blob = fixture()
        blob[0xA00:0xA07] = bytes.fromhex('0f958208020000')
        with self.assertRaisesRegex(RuntimeError, 'found 2'):
            self.resolve_blob(blob)

    def test_disagreeing_config_readers_block_discovery(self):
        blob = fixture()
        struct.pack_into('<i', blob, 0x400 + 0x110 + 3, 0x5408 - (0x1000 + 0x110 + 7))
        with self.assertRaisesRegex(RuntimeError, 'unique block'):
            self.resolve_blob(blob)

    def test_unknown_structural_family_and_wrong_stats_are_blocked(self):
        for blob in (fixture(stride=0x1900), fixture(stats=0x1750), fixture(rerolls=0x1808)):
            with self.subTest(), self.assertRaises(RuntimeError):
                self.resolve_blob(blob)

    def test_truncated_or_wrong_architecture_is_blocked(self):
        for blob in (fixture()[:100], b'not a game'):
            with self.subTest(), self.assertRaises(RuntimeError):
                self.resolve_blob(blob)

    def test_file_version_cache_invalidates_after_update(self):
        with tempfile.TemporaryDirectory() as temp, patch('compatibility.time.monotonic', return_value=1) as clock:
            path = Path(temp) / 'game.exe'
            path.write_bytes(fixture())
            first = resolve(path)
            before = _resolve.cache_info().hits
            self.assertIs(resolve(path), first)
            self.assertEqual(_resolve.cache_info().hits, before + 1)
            blob = fixture()
            blob[0x2300] = 19
            path.write_bytes(blob)
            clock.return_value = 6
            self.assertNotEqual(resolve(path).digest, first.digest)
