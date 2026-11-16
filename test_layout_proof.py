"""Executable-shaped fixtures test independent field discovery and rejection."""
from pathlib import Path
import struct
import tempfile
import unittest

from compatibility import resolve
from layout_proof import STAT_LABELS
from test_compatibility import fixture


def named_fixture(stride=0x1950, stats=0x1838, name=0x11D8, rerolls=0x18FC,
                  pointer=0xC68, index=0xF18, offsets=None, wrapped=True,
                  overwrite=False, truncate=False):
    blob = fixture(stride, stats, rerolls, config=0x5018, pointer=pointer, index=index)
    blob.extend(bytes(0x200))
    struct.pack_into('<H', blob, 0x86, 3)
    struct.pack_into('<Q', blob, 0xB0, 0x140000000)
    struct.pack_into('<8sIIII', blob, 0x1D8, b'.pdata', 12, 0x7000, 12, 0x2400)
    u = lambda value: struct.pack('<I', value)
    def put(pos, value):
        blob[0x400 + pos:0x400 + pos + len(value)] = value
    # An independent name routine shares the array layout, not a family size.
    put(0x600, b'\x48\x8b\x89' + u(index) + b'\x48\x8b\x96' + u(pointer + 8)
        + b'\x48\x39\xd1\x4c\x89\xbd' + u(0x180) + b'\x0f\x83' + u(8)
        + b'\x48\x8b\x86' + u(pointer) + b'\x48\x69\xc9' + u(stride)
        + b'\x4c\x8d\x2c\x08\x48\x8d\x3c\x08\x48\x81\xc7' + u(name)
        + b'\x45\x31\xe4\x48\x83\xbc\x08' + u(name) + b'\xff')
    put(0x700, b'\x48\x8b\x89' + u(index) + b'\x49\x8b\x97' + u(pointer + 8)
        + b'\x48\x39\xd1\x0f\x83' + u(8) + b'\x49\x8b\x87' + u(pointer)
        + b'\x48\x69\xc9' + u(stride) + b'\x4c\x8d\x2c\x08'
        + b'\x49\x8d\x85' + u(rerolls + 12) + b'\x49\x81\xc5' + u(rerolls))
    put(0x398, u(rerolls + 12))
    defaults = dict(zip(STAT_LABELS.values(), (8, 12, 16, 20, 24, 28, 32, 44, 48, 52, 56, 60)))
    defaults.update(offsets or {})
    put(0x580, b'\x8b\x88' + u(stats + defaults['damage']) + b'\xf3\x4c\x0f\x2a\xc1')
    put(0x20, b'\x8b\x09\x41\xb8\x0a\x00\x00\x00\xc3')
    put(0x40, b'\x48\x8b\x01\x8b\x08\x41\xb8\x0a\x00\x00\x00\xc3')
    for rva, size, function in ((0x5C00, 4, 0x1020), (0x5C40, 8, 0x1040)):
        struct.pack_into('<4Q', blob, 0x1400 + rva - 0x5000, 0, size, size, 0x140000000 + function)
    output = bytearray(b'\x48\x81\xec\x00\x03\x00\x00')
    labels = {}
    cursor = 0x5400
    def emit(value):
        output.extend(value)
    def lea(prefix, target):
        address = 0x1900 + len(output)
        emit(prefix + struct.pack('<i', target - address - 7))
    for number, (label, key) in enumerate(STAT_LABELS.items()):
        labels[label] = cursor
        encoded = label.encode('ascii')
        start = 0x1400 + cursor - 0x5000
        blob[start:start + len(encoded)] = encoded
        cursor += len(encoded) + 1
        emit(b'\x48\x8d\x81' + u(defaults[key]))
        if truncate and number == 0:
            emit(b'\x89\xc0')
        indirect = wrapped and key == 'luck'
        if indirect:
            emit(b'\x48\x89\x84\x24' + u(0x280))
            emit(b'\x48\x8d\x84\x24' + u(0x280))
        emit(b'\x48\x89\x84\x24' + u(number * 16))
        lea(b'\x48\x8d\x05', 0x5C40 if indirect else 0x5C00)
        emit(b'\x48\x89\x84\x24' + u(number * 16 + 8))
    if overwrite:
        emit(b'\xc6\x84\x24' + u(1) + b'\x00')
    for number, label in enumerate(STAT_LABELS):
        lea(b'\x48\x8d\x15', labels[label])
        emit(b'\x41\xb8' + u(len(label)))
        emit(b'\x4c\x8d\x8c\x24' + u(number * 16))
        emit(b'\xe8' + struct.pack('<i', 0x1080 - (0x1900 + len(output) + 5)))
    emit(b'\xc3')
    if 0x900 + len(output) > 0x1000:
        raise AssertionError('Fixture exceeded its code section')
    put(0x900, output)
    struct.pack_into('<III', blob, 0x2400, 0x1900, 0x1900 + len(output), 0)
    return blob


class NamedLayoutTests(unittest.TestCase):
    def resolve(self, blob):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'game.exe'
            path.write_bytes(blob)
            return resolve(path)

    def test_current_build_is_derived_without_a_family_entry(self):
        layout = self.resolve(named_fixture())
        self.assertEqual((layout.stride, layout.name, layout.stats, layout.rerolls),
                         (0x1950, 0x11D8, 0x1838, 0x18FC))
        self.assertEqual((layout.vector, layout.index), (0xC60, 0xF18))
        self.assertEqual(layout.fields['luck'], 60)
        self.assertEqual(len(layout.fields), 12)

    def test_unlisted_future_relocation_and_reordered_stats_are_supported(self):
        layout = self.resolve(named_fixture(stride=0x1D10, stats=0x1A00, name=0x1280,
                                            rerolls=0x1BC4, pointer=0xD08, index=0x1030,
                                            offsets={'damage': 16, 'crit': 8}))
        self.assertEqual((layout.stride, layout.name, layout.stats, layout.rerolls),
                         (0x1D10, 0x1280, 0x1A00, 0x1BC4))
        self.assertEqual((layout.fields['damage'], layout.fields['crit']), (16, 8))

    def test_direct_and_referenced_u32_formatters_are_verified(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(self.resolve(named_fixture(wrapped=wrapped)).fields['luck'], 60)

    def test_wrong_types_and_unrelated_reference_formatters_are_rejected(self):
        for position in (0x2008, 0x2048, 0x400 + 0x40 + 3):
            blob = named_fixture()
            blob[position] ^= 4
            with self.subTest(position=position), self.assertRaisesRegex(RuntimeError, 'formatter'):
                self.resolve(blob)

    def test_duplicate_and_out_of_bounds_fields_are_rejected(self):
        for change in ({'damage': 16}, {'damage': 64}, {'damage': 9}):
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, 'formatter'):
                self.resolve(named_fixture(offsets=change))

    def test_partial_stack_overwrite_and_pointer_truncation_are_rejected(self):
        for change in ({'overwrite': True}, {'truncate': True}):
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, 'formatter'):
                self.resolve(named_fixture(**change))

    def test_name_and_resource_routines_must_use_the_same_array(self):
        for position in (0x400 + 0x600 + 3, 0x400 + 0x700 + 3):
            blob = named_fixture()
            blob[position] ^= 8
            with self.subTest(position=position), self.assertRaises(RuntimeError):
                self.resolve(blob)

    def test_missing_stat_labels_or_disagreeing_combat_reader_are_rejected(self):
        blob = named_fixture()
        position = blob.find(b'luck_pct')
        blob[position] = ord('x')
        with self.assertRaisesRegex(RuntimeError, 'formatter'):
            self.resolve(blob)
        blob = named_fixture()
        blob[0x400 + 0x580 + 2] ^= 4
        with self.assertRaisesRegex(RuntimeError, 'code anchor'):
            self.resolve(blob)
