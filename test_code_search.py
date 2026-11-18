import struct
import unittest

from code_search import stat_serializers, name_serializers, resource_serializers
import test_compatibility
from test_compatibility import fixture


ARRAY = (0xF18, 0xC70, 0xC68, 0x1950)
u = lambda n: struct.pack('<I', n)


def mov(dst, base, displacement):
    return bytes((0x48 | ((dst >> 3) << 2) | (base >> 3), 0x8B,
                  0x80 | ((dst & 7) << 3) | (base & 7))) + u(displacement)


def indexed(dst, base, index, displacement=None, vector=False):
    rex = 0x40 | (0 if vector else 8) | ((dst >> 3) << 2) | ((index >> 3) << 1) | (base >> 3)
    mod = 0x80 if displacement is not None else 0
    return bytes((rex,)) + (b'\x0f\x10' if vector else b'\x8d') + bytes((mod | ((dst & 7) << 3) | 4,
                 ((index & 7) << 3) | (base & 7))) + (u(displacement) if displacement is not None else b'')


def header(nop=b'', wrong_count=False):
    parts = [mov(8, 14, ARRAY[0]), mov(9, 15, ARRAY[1]), b'\x4d\x39\xc8',
             b'\x0f\x83' + u(0x300), mov(10, 14 if wrong_count else 15, ARRAY[2]),
             b'\x4d\x69\xd8' + u(ARRAY[3]), indexed(12, 10, 11)]
    return nop.join(parts)


class CodeSearchTests(unittest.TestCase):
    def test_register_allocation_padding_and_vector_schedule_changes(self):
        for padding in (b'', b'\x90', bytes.fromhex('0f1f440000')):
            raw = header(padding) + padding + padding.join(indexed(4 + i, 10, 11, 0x1838 + off, vector=True)
                                                          for i, off in enumerate((32, 0, 48, 16)))
            matches = stat_serializers(raw, 0x1000)
            self.assertEqual(len(matches), 1)
            self.assertEqual((matches[0].array, matches[0].value), (ARRAY, 0x1838))

    def test_mismatched_array_base_and_multiplier_are_rejected(self):
        suffix = b''.join(indexed(4 + i, 10, 11, 0x1838 + i * 16, vector=True) for i in range(4))
        self.assertEqual(stat_serializers(header(wrong_count=True) + suffix, 0x1000), [])
        raw = bytearray(header() + suffix)
        position = raw.find(b'\x4d\x69\xd8')
        raw[position + 2] = 0xD9  # Multiply the count instead of the selected index.
        self.assertEqual(stat_serializers(raw, 0x1000), [])

    def test_duplicate_complete_candidates_remain_ambiguous(self):
        raw = header() + b''.join(indexed(4 + i, 10, 11, 0x1838 + i * 16, vector=True) for i in range(4))
        self.assertEqual(len(stat_serializers(raw + bytes(16) + raw, 0x1000)), 2)

    def test_renamed_name_alias_and_resource_registers(self):
        name = header(b'\x90') + b'\x4d\x89\xe5\x49\x81\xc5' + u(0x11D8)
        name += b'\x45\x31\xf6\x4b\x83\xbc\x1a' + u(0x11D8) + b'\xff'
        result = name_serializers(name, 0x1000, ARRAY)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].value, 0x11D8)
        resource = header() + b'\x85\xc0\x49\x8d\x94\x24' + u(0x1908)
        resource += b'\x66\x90\x49\x81\xc4' + u(0x18FC)
        self.assertEqual(len(resource_serializers(resource, 0x1000, ARRAY, 0x18FC)), 1)
        # A clobbered hero or different array is not accepted as the same field.
        clobbered = header() + b'\x45\x31\xe4' + resource[len(header()):]
        self.assertEqual(resource_serializers(clobbered, 0x1000, ARRAY, 0x18FC), [])

    def test_resource_search_can_find_an_array_without_the_stat_serializer(self):
        fields = b'\x49\x8d\x94\x24' + u(0x1908) + b'\x49\x81\xc4' + u(0x18FC)
        raw = header() + b'\x85\xc0\x74\x20' + fields
        result = resource_serializers(raw, 0x1000, None, 0x18FC)
        self.assertEqual([(match.array, match.value) for match in result], [(ARRAY, 0x18FC)])
        # A branch into the middle of the field-address sequence is ambiguous.
        joining = header() + b'\x85\xc0\x74\x08' + fields
        self.assertEqual(resource_serializers(joining, 0x1000, None, 0x18FC), [])

    def test_config_register_changes_are_independently_corroborated(self):
        blob = fixture(config=0x5018)
        code = memoryview(blob)[0x400:0x1400]
        code[0x200:0x260] = b'\x90' * 0x60
        block = b''.join(b'\x41\x0f\x95\x83' + u(off) for off in (0x208, 0x209, 0x20A))
        block += b'\xf3\x41\x0f\x11\xbb' + u(0x1F8)
        code[0x200:0x200 + len(block)] = block
        compare = bytes.fromhex('0fb6ba080200006690413ab808020000')
        code[0x240:0x240 + len(compare)] = compare
        for position, opcode in ((0x100, bytes.fromhex('440fb615')), (0x110, bytes.fromhex('440fb63d'))):
            raw = opcode + struct.pack('<i', 0x5018 + 0x208 - (0x1000 + position + len(opcode) + 4))
            code[position:position + len(raw)] = raw
        code[0xF0:0xF7] = b'\x44\x8b\x05' + struct.pack('<i', 0x5018 + 0x230 - 0x10F7)
        code[0x130:0x139] = b'\xf3\x44\x0f\x10\x2d' + struct.pack('<i', 0x5018 + 0x1F8 - 0x1139)
        layout = test_compatibility.CompatibilityTests().resolve_blob(blob)
        self.assertEqual(layout.config, 0x5018)

    def test_config_comparison_cannot_use_a_different_flag_register(self):
        blob = fixture()
        blob[0x400 + 0x220 + 2] = 0x8A  # ECX load, but comparison still consumes AL.
        with self.assertRaisesRegex(RuntimeError, 'configuration field anchors'):
            test_compatibility.CompatibilityTests().resolve_blob(blob)

    def test_full_resolver_cross_checks_renamed_and_padded_helpers(self):
        import test_layout_proof
        blob = test_layout_proof.named_fixture()
        raw = header(b'\x66\x90') + b'\x66\x90'
        raw += b'\x66\x90'.join(indexed(4 + i, 10, 11, 0x1838 + off, vector=True)
                                  for i, off in enumerate((32, 0, 48, 16)))
        blob[0x680:0x780] = b'\x90' * 0x100
        blob[0x680:0x680 + len(raw)] = raw
        name = header(b'\x90') + b'\x4d\x89\xe5\x49\x81\xc5' + u(0x11D8)
        name += b'\x45\x31\xf6\x4b\x83\xbc\x1a' + u(0x11D8) + b'\xff'
        blob[0xA00:0xA80] = b'\x90' * 0x80
        blob[0xA00:0xA00 + len(name)] = name
        resource = header() + b'\x49\x8d\x94\x24' + u(0x1908) + b'\x66\x90\x49\x81\xc4' + u(0x18FC)
        blob[0xB00:0xB80] = b'\x90' * 0x80
        blob[0xB00:0xB00 + len(resource)] = resource
        layout = test_layout_proof.NamedLayoutTests().resolve(blob)
        self.assertEqual((layout.index, layout.vector, layout.stride, layout.name, layout.rerolls),
                         (0xF18, 0xC60, 0x1950, 0x11D8, 0x18FC))
