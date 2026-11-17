import unittest
from hero_identity import discover_identity
from test_code_search import ARRAY, header, u


def routines(offset):
    sentinel=header()+b'\x49\x83\xbc\x24'+u(offset)+b'\xff'
    drop=header()+b'\x49\x8b\x84\x24'+u(offset)
    drop+=bytes.fromhex('4883f8ff74124885c0740d498b8c24')+u(offset+8)+b'\xe8\x00\x00\x00\x00'
    return sentinel+b'\xcc'*16+drop


class IdentityTests(unittest.TestCase):
    def test_compiler_changed_string_drop_is_corroborated(self):
        for offset in (0x11d8,0x12c0,0x1300):
            name,guards=discover_identity(routines(offset),0x1000,ARRAY)
            self.assertEqual(name,offset)
            self.assertEqual(len(guards),2)

    def test_single_sentinel_and_conflicting_strings_fail_closed(self):
        for raw in (header()+b'\x49\x83\xbc\x24'+u(0x12c0)+b'\xff',
                    routines(0x12c0)+b'\xcc'*16+routines(0x1300)):
            with self.assertRaises(ValueError):discover_identity(raw,0x1000,ARRAY)

    def test_different_array_cannot_corroborate_identity(self):
        with self.assertRaises(ValueError):discover_identity(routines(0x12c0),0x1000,(*ARRAY[:3],ARRAY[3]+8))
