import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from automatic_hunt import AutomaticHunt, HuntSchedule, SearchPlan, file_identity, make_plan
from test_compatibility import fixture


class FakeMemory:
    def __init__(self, regions):
        self.regions = regions
        self.reads = []

    def read(self, address, size):
        self.reads.append((address, size))
        for base, raw in self.regions.items():
            if base <= address and address + size <= base + len(raw):
                return bytes(raw[address-base:address-base+size])
        raise OSError('unmapped')

    def write(self, *_):
        raise AssertionError('Automatic Hunt must never write')


class AutomaticHuntTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.exe'
        self.path.write_bytes(b'image')
        self.plan = SearchPlan('abcdef123456', 0x100, 0x180, 0x300, 0x240, 0x10, b'guard')
        self.module = bytearray(128)
        self.module[16:21] = b'guard'
        self.stack = bytearray(0x2000)
        self.hero = bytearray(0x300)
        self.game = 0x100040
        self.pointer = 0x200000
        struct.pack_into('<3Q', self.stack, 0x140, 4, self.pointer, 1)
        struct.pack_into('<3Q', self.hero, 0xF0, 91, 169, 178)
        struct.pack_into('<I', self.hero, 0x150, 3)
        struct.pack_into('<I', self.hero, 0x248, 17)
        self.memory = FakeMemory({0x140000000:self.module, 0x100000:self.stack, self.pointer:self.hero})
        self.process = Mock()
        self.process.memory_maps.return_value = [SimpleNamespace(addr='140000000-140001000', path=str(self.path))]
        self.session = SimpleNamespace(path=str(self.path), process=lambda:self.process)
        self.source = Mock(return_value=(self.plan, file_identity(self.path)))
        self.stacks = lambda p,m:[(7,0x100000,0x102000)]
        self.hunt = AutomaticHunt(seconds=10)

    def step(self, **kwargs):
        return self.hunt.step(self.session,self.memory,stack_source=self.stacks,plan_source=self.source,**kwargs)

    def test_automatically_finds_values_and_compares_without_input(self):
        first = self.step()
        self.assertEqual(first['heroes'][0]['pairs'][0xF8],(169,178))
        self.assertEqual(first['heroes'][0]['slots'][2],17)
        struct.pack_into('<Q',self.hero,0xF8,165)
        second = self.step()
        self.assertEqual(second['heroes'][0]['comparisons'][0xF8],'changed')
        self.assertEqual(second['examined'],0)  # reuses revalidated roots
        self.source.assert_called_once()

    def test_multiplayer_and_invalid_header_values_are_rejected(self):
        struct.pack_into('<Q',self.stack,0x150,2)
        self.assertEqual(self.step()['heroes'],[])
        struct.pack_into('<Q',self.stack,0x150,1)
        struct.pack_into('<Q',self.hero,0xF8,179)
        self.assertEqual(self.step()['heroes'],[])

    def test_changed_vector_during_read_discards_snapshot(self):
        original=self.memory.read
        def read(address,size):
            value=original(address,size)
            if address==self.pointer:
                struct.pack_into('<Q',self.stack,0x148,0x300000)
            return value
        self.memory.read=read
        self.assertEqual(self.step()['heroes'],[])

    def test_cancellation_and_reused_pid_stop_before_game_reads(self):
        with self.assertRaises(InterruptedError):self.step(cancelled=lambda:True)
        self.assertEqual(self.memory.reads,[])
        self.session.process=Mock(side_effect=RuntimeError('process changed'))
        with self.assertRaisesRegex(RuntimeError,'process changed'):self.step()
        self.assertEqual(self.memory.reads,[])

    def test_changed_code_resets_cached_roots(self):
        self.step()
        self.module[16]=0
        with self.assertRaisesRegex(RuntimeError,'instructions changed'):self.step()
        self.assertEqual(self.hunt.leases,[])
        self.assertIsNone(self.hunt.plan)

    def test_scan_budget_continues_and_includes_split_headers(self):
        self.stack[0x140:0x158]=bytes(24)
        self.game=0x101000-8-self.plan.vector
        struct.pack_into('<3Q',self.stack,0xFF8,4,self.pointer,1)
        self.hunt=AutomaticHunt(budget=0x1000,seconds=10)
        first=self.step()
        self.assertEqual(first['examined'],0x1000)
        self.assertEqual(len(first['heroes']),1)
        self.assertTrue(first['partial'])
        self.assertEqual(self.hunt.cursor,(7,0x101000))
        self.assertFalse(self.step()['partial'])

    def test_unchanged_failed_plan_is_not_repeated_but_file_update_retries(self):
        self.source.side_effect=ValueError('unknown array')
        for _ in range(2):
            with self.assertRaisesRegex((ValueError,RuntimeError),'unknown array'):self.step()
        self.source.assert_called_once()
        self.path.write_bytes(b'changed image')
        with self.assertRaises(ValueError):self.step()
        self.assertEqual(self.source.call_count,2)

    def test_independent_search_plan_survives_missing_named_metadata(self):
        self.path.write_bytes(fixture())
        plan,stamp=make_plan(self.path)
        self.assertEqual((plan.vector,plan.index,plan.stride,plan.stats),(0xB80,0xE38,0x1858,0x1740))
        self.assertEqual(stamp,file_identity(self.path))
        blob=fixture()
        blob[0xA00:0xA80]=blob[0x680:0x700]
        self.path.write_bytes(blob)
        with self.assertRaisesRegex(ValueError,'unique'):make_plan(self.path)

    def test_retry_backoff_is_bounded_and_resets_after_progress(self):
        schedule=HuntSchedule()
        empty=dict(heroes=[],partial=False)
        self.assertEqual([schedule.delay(empty) for _ in range(6)],[2,4,8,15,15,15])
        self.assertEqual(schedule.delay(dict(heroes=[],partial=True)),.75)
        self.assertEqual(schedule.delay(empty),2)
        self.assertEqual(schedule.delay(dict(heroes=[{}],partial=False)),2)

    def test_interrupted_plan_capture_retries_module_resolution(self):
        self.session.process=Mock(side_effect=[self.process,RuntimeError('temporarily unavailable'),self.process])
        with self.assertRaisesRegex(RuntimeError,'temporarily unavailable'):self.step()
        self.assertIsNone(self.hunt.base)
        self.session.process=Mock(return_value=self.process)
        self.assertEqual(len(self.step()['heroes']),1)

    def test_new_process_or_hero_identity_discards_temporal_history(self):
        self.step()
        struct.pack_into('<Q',self.hero,0xF0,92)
        self.assertEqual(self.step()['heroes'][0]['comparisons'],{})
        self.session=SimpleNamespace(path=str(self.path),process=lambda:self.process)
        self.assertEqual(self.step()['heroes'][0]['comparisons'],{})
        self.assertEqual(self.source.call_count,2)

    def test_distinct_allocations_are_ambiguous_but_duplicate_roots_are_grouped(self):
        struct.pack_into('<3Q',self.stack,0x1140,4,self.pointer+0x1000,1)
        self.memory.regions[self.pointer+0x1000]=bytearray(self.hero)
        self.assertTrue(self.step()['ambiguous'])
        struct.pack_into('<Q',self.stack,0x1148,self.pointer)
        result=self.step()
        self.assertFalse(result['ambiguous'])
        self.assertEqual(len(result['heroes']),1)

