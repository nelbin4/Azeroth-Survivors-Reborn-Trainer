from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from compatibility import resolve, _resolve


def fixture(stride=0x1858, stats=0x1740, rerolls=0x1804, config=0x5000, base=7,
            pointer=0xB88, index=0xE38):
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
        code[pos:pos + 7] = b'\x0f\xb6\x35' + struct.pack('<i', config + 0x208 - (0x1000 + pos + 7))
    def put(pos, raw):
        code[pos:pos + len(raw)] = raw
    put(0xF0, b'\x8b\x05' + struct.pack('<i', config + 0x230 - (0x10F0 + 6)))
    put(0x130, b'\xf3\x0f\x10\x05' + struct.pack('<i', config + 0x1F8 - (0x1130 + 8)))
    put(0x200, bytes.fromhex('0f958208020000'))
    put(0x220, bytes.fromhex('0fb68208020000413a8008020000'))
    u = lambda value: struct.pack('<I', value)
    put(0x280, b'\x48\x8b\x89' + u(index) + b'\x49\x8b\x95' + u(pointer + 8)
        + b'\x48\x39\xd1\x0f\x83' + u(100) + b'\x49\x8b\x95' + u(pointer)
        + b'\x48\x69\xc9' + u(stride) + b'\x48\x8d\x1c\x0a\x0f\x10\x84\x0a' + u(stats)
        + b'\xf3\x0f\x6f\x8c\x0a' + u(stats + 16)
        + b'\xf3\x0f\x6f\x94\x0a' + u(stats + 32)
        + b'\xf3\x0f\x6f\x9c\x0a' + u(stats + 48))
    put(0x380, b'\x48\x8d' + bytes([0x80 | base]) + u(0x15C)
        + b'\x48\x8d' + bytes([0x88 | base]) + u(0xED8)
        + b'\x48\x8d' + bytes([0x90 | base]) + u(0x150)
        + b'\x4c\x8d' + bytes([0x80 | base]) + u(0x1810))
    put(0x400, b'\x48\x81' + bytes([0xC0 | base]) + u(rerolls))
    for pos, modrm, offset in ((0x3A0, 0x90 | base, 0xF8), (0x3B0, 0x88 | base, 0x100),
                               (0x3C0, 0x80 | base, 0x154), (0x3D0, 0x80 | base, 0x158)):
        put(pos, b'\x48\x8d' + bytes([modrm]) + u(offset))
    put(0x580, b'\x8b\x88' + u(stats + 8))
    if stride == 0x1870:
        put(0x600, b'\x48\x8d\x3c\x08\x48\x81\xc7' + u(0x10F8)
            + b'\xc7\x85' + u(0x180) + u(0)
            + b'\x48\x83\xbc\x08' + u(0x10F8) + b'\xff')
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

    def test_new_family_with_eight_byte_alignment_and_rbx_serializer(self):
        layout = self.resolve_blob(fixture(stride=0x1870, stats=0x1758, rerolls=0x181C,
                                           config=0x5018, base=3, pointer=0xC38, index=0xEE8))
        self.assertEqual((layout.config, layout.stride, layout.name, layout.stats, layout.rerolls),
                         (0x5018, 0x1870, 0x10F8, 0x1758, 0x181C))
        self.assertEqual((layout.vector, layout.index), (0xC30, 0xEE8))
        for rva in (0x10F0, 0x1130, 0x13A0, 0x13B0, 0x13C0, 0x13D0, 0x1600):
            self.assertIn(rva, layout.signatures)

    def test_original_layout_family_remains_supported(self):
        layout = self.resolve_blob(fixture(stride=0x1798, stats=0x1670, rerolls=0x1744,
                                           pointer=0xBB0, index=0xE60))
        self.assertEqual((layout.stride, layout.name, layout.stats, layout.rerolls),
                         (0x1798, 0x1038, 0x1670, 0x1744))

    def test_relocated_config_requires_initialization_and_speed_corroboration(self):
        for offset, length in ((0xF0, 6), (0x130, 8)):
            blob = fixture(config=0x5018)
            blob[0x400 + offset:0x400 + offset + length] = b'\x90' * length
            with self.subTest(offset=offset), self.assertRaisesRegex(RuntimeError, 'unique block'):
                self.resolve_blob(blob)
        for displacement in (0xF0 + 2, 0x130 + 4):
            blob = fixture(config=0x5018)
            blob[0x400 + displacement] ^= 8
            with self.subTest(displacement=displacement), self.assertRaisesRegex(RuntimeError, 'unique block'):
                self.resolve_blob(blob)
        with self.assertRaisesRegex(RuntimeError, 'unique block'):
            self.resolve_blob(fixture(config=0x5014))

    def test_new_family_rejects_changed_name_health_and_xp_fields(self):
        for offset in (0x600 + 7, 0x3A0 + 3, 0x3B0 + 3, 0x3C0 + 3, 0x3D0 + 3):
            blob = fixture(stride=0x1870, stats=0x1758, rerolls=0x181C, config=0x5018, base=3)
            blob[0x400 + offset] ^= 4
            with self.subTest(offset=offset), self.assertRaises(RuntimeError):
                self.resolve_blob(blob)

    def test_inconsistent_serializer_registers_are_rejected(self):
        blob = fixture(base=3)
        blob[0x400 + 0x380 + 9] = 0x8F
        with self.assertRaisesRegex(RuntimeError, 'code anchor'):
            self.resolve_blob(blob)
        blob = fixture(base=3)
        blob[0x400 + 0x400 + 2] = 0xC7
        with self.assertRaisesRegex(RuntimeError, 'reroll serializer'):
            self.resolve_blob(blob)

    def test_independently_corroborated_second_config_still_blocks_discovery(self):
        blob = fixture(config=0x5018)
        for pos in (0x800, 0x810):
            blob[0x400 + pos:0x400 + pos + 7] = b'\x0f\xb6\x35' + struct.pack('<i', 0x5418 + 0x208 - (0x1000 + pos + 7))
        blob[0xBF0:0xBF6] = b'\x8b\x05' + struct.pack('<i', 0x5418 + 0x230 - (0x17F0 + 6))
        blob[0xC30:0xC38] = b'\xf3\x0f\x10\x05' + struct.pack('<i', 0x5418 + 0x1F8 - (0x1830 + 8))
        with self.assertRaisesRegex(RuntimeError, 'unique block'):
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
