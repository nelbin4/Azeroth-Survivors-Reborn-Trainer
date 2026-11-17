import struct
import unittest
from unittest.mock import Mock
from value_hunt import ValueHunt, encoded


class HuntTests(unittest.TestCase):
    def test_integer_width_range_and_alignment(self):
        session = Mock()
        memory = Mock()
        raw = bytearray(64)
        struct.pack_into('<Q', raw, 8, 178)
        struct.pack_into('<I', raw, 28, 178)
        memory.read.side_effect = lambda address, size: bytes(raw[address-0x10000:address-0x10000+size])
        hunt = ValueHunt(session, '64-bit integer')
        self.assertEqual(hunt.scan(memory, '178', [(0x10000,64)]), [0x10008])
        hunt = ValueHunt(session, '32-bit integer')
        self.assertEqual(hunt.scan(memory, '178', [(0x10000,64)]), [0x10008,0x1001C])
        with self.assertRaises(ValueError):encoded('-1','32-bit integer')
        with self.assertRaises(ValueError):encoded(str(1<<32),'32-bit integer')
        overlapping = b'\x00' + b'\x01' * 7
        memory.read.side_effect = lambda address,size:overlapping[address-0x10000:address-0x10000+size]
        self.assertEqual(hunt.scan(memory,0x01010101,[(0x10000,8)]),[0x10004])

    def test_refine_follows_changed_live_values(self):
        memory = Mock()
        raw = bytearray(struct.pack('<4I',169,169,178,0))
        memory.read.side_effect = lambda address,size:bytes(raw[address-0x10000:address-0x10000+size])
        hunt = ValueHunt(Mock(),'32-bit integer')
        hunt.scan(memory,169,[(0x10000,len(raw))])
        struct.pack_into('<I',raw,4,165)
        self.assertEqual(hunt.refine(memory,165),[0x10004])
        memory.write.assert_not_called()

    def test_budget_and_candidate_limits_are_reported(self):
        memory=Mock();memory.read.side_effect=lambda address,size:bytes(size)
        hunt=ValueHunt(Mock(),'32-bit integer',budget=16,limit=2)
        self.assertEqual(len(hunt.scan(memory,0,[(0x10000,64)])),2)
        self.assertTrue(hunt.limited)
        hunt=ValueHunt(Mock(),'32-bit integer',budget=16)
        hunt.scan(memory,1,[(0x10000,64)])
        self.assertEqual(hunt.scanned,16)
        self.assertTrue(hunt.limited)

    def test_reused_pid_and_unreadable_candidates_are_not_followed(self):
        session=Mock();memory=Mock()
        hunt=ValueHunt(session,'64-bit integer');hunt.addresses=[0x10000]
        session.process.side_effect=RuntimeError('process changed')
        with self.assertRaises(RuntimeError):hunt.refine(memory,178)
        memory.read.assert_not_called()
        session.process.side_effect=None;memory.read.side_effect=OSError('gone')
        self.assertEqual(hunt.refine(memory,178),[])

    def test_continue_resumes_without_rescanning_or_mixing_values(self):
        memory=Mock();raw=struct.pack('<8I',1,2,3,4,5,6,178,8)
        memory.read.side_effect=lambda address,size:raw[address-0x10000:address-0x10000+size]
        hunt=ValueHunt(Mock(),'32-bit integer',budget=16)
        hunt.scan(memory,178,[(0x10000,len(raw))])
        self.assertEqual(hunt.addresses,[])
        with self.assertRaises(ValueError):hunt.scan(memory,169,[(0x10000,len(raw))],more=True)
        hunt.scan(memory,178,[(0x10000,len(raw))],more=True)
        self.assertEqual(hunt.addresses,[0x10018])
        self.assertIsNone(hunt.next_address)
