from dataclasses import replace
import struct
import unittest
from unittest.mock import Mock

from automatic_hunt import HuntSchedule, file_identity
from hunt_fields import FieldProof
from hunt_freeze import HuntFreezer
import test_automatic_hunt as automatic_tests


class HuntFreezeTests(unittest.TestCase):
    setUp=automatic_tests.AutomaticHuntTests.setUp
    step=automatic_tests.AutomaticHuntTests.step

    def prepare(self,keys=('max_health','health','run_gold','rerolls')):
        self.module[0x30:0x35]=b'proof'
        specs={'max_health':(0x100,'Q',1000000),'health':(0xF8,'Q',1000000),
               'run_gold':(0x15C,'I',99999999),'rerolls':(0x2E0,'I',9999)}
        proofs=tuple(FieldProof(k,*specs[k],((0x30,b'proof'),)) for k in keys)
        self.plan=replace(self.plan,fields=proofs)
        self.source.return_value=(self.plan,file_identity(self.path))
        self.freezer=HuntFreezer()
        self.assertEqual(self.freezer.observe(self.hunt,self.step()),())
        self.result=self.step()
        self.fields=self.freezer.observe(self.hunt,self.result)
        self.writes=[]
        def write(address,raw):
            self.writes.append((address,raw))
            for base,data in self.memory.regions.items():
                if base<=address and address+len(raw)<=base+len(data):
                    data[address-base:address-base+len(raw)]=raw
                    return
            raise OSError('unmapped')
        self.memory.write=write

    def apply(self,**kwargs):
        return self.freezer.apply(self.hunt,self.result,self.memory,self.fields,**kwargs)

    def add_echo(self):
        table=bytearray(512)
        table[0x100:0x104]=bytes([255,2,255,255])
        struct.pack_into('<B3xf',table,0xF0,13,.09)
        self.memory.regions[0x400000]=table
        struct.pack_into('<4Q',self.hero,0x200,0x400100,3,2,1)
        proof=FieldProof('echo',0x200,'f',1,((0x30,b'proof'),),(0x208,0x218,13))
        self.hunt.plan=replace(self.hunt.plan,fields=(*self.hunt.plan.fields,proof))
        self.fields=self.freezer.observe(self.hunt,self.result)
        return table

    def test_echo_is_maxed_in_its_current_table_and_held(self):
        self.prepare(('run_gold',))
        table=self.add_echo()
        self.assertEqual(self.apply()['held']['echo'],1)
        self.assertEqual(struct.unpack_from('<f',table,0xF4)[0],1)
        writes=len(self.writes)
        self.apply()
        self.assertEqual(len(self.writes),writes)

    def test_absent_echo_does_not_block_other_stats(self):
        self.prepare(('run_gold',));table=self.add_echo()
        table[0x101]=255;struct.pack_into('<Q',self.hero,0x218,0)
        result=self.apply()
        self.assertEqual(result['held'],{'run_gold':99999999})
        self.assertIn('echo',result['errors'])

    def test_table_rehash_before_write_does_not_follow_stale_effect_pointer(self):
        self.prepare(('run_gold',));table=self.add_echo()
        self.memory.regions[0x500000]=bytearray(table)
        read=self.memory.read;hits=0
        def moving(address,size):
            nonlocal hits
            if address==self.pointer+0x200 and size==32:
                hits+=1
                if hits==2:struct.pack_into('<Q',self.hero,0x200,0x500100)
            return read(address,size)
        self.memory.read=moving
        result=self.apply()
        self.assertIn('echo',result['errors'])
        self.assertEqual(result['held'],{'run_gold':99999999})
        self.assertTrue(all(address==self.pointer+0x15C for address,_ in self.writes))

    def test_one_verified_field_is_maxed_and_held_without_waiting_for_others(self):
        self.prepare(('run_gold',))
        before=bytes(self.hero)
        self.assertEqual(self.apply()['held'],{'run_gold':99999999})
        self.assertEqual(self.memory.read(self.pointer+0xF8,16),before[0xF8:0x108])
        self.assertEqual(len(self.writes),1)
        self.apply()
        self.assertEqual(len(self.writes),1) # skips unchanged values
        struct.pack_into('<I',self.hero,0x15C,100)
        self.apply()
        self.assertEqual(len(self.writes),2)
        self.assertEqual(HuntSchedule().delay(dict(heroes=[{}],partial=True,freeze=dict(held={'run_gold':99999999}))),.5)

    def test_maximum_is_applied_before_healing_and_other_fields_continue(self):
        self.prepare()
        result=self.apply()
        self.assertEqual(len(result['held']),4)
        self.assertEqual(struct.unpack_from('<2Q',self.hero,0xF8),(1000000,1000000))
        self.assertEqual(self.writes[0][0],self.pointer+0x100)
        self.assertEqual(self.writes[-1][0],self.pointer+0xF8)

    def test_lost_solo_ownership_or_reused_process_prevents_every_write(self):
        self.prepare()
        struct.pack_into('<Q',self.stack,0x150,2)
        with self.assertRaisesRegex(RuntimeError,'ownership changed'):self.apply()
        self.assertEqual(self.writes,[])
        struct.pack_into('<Q',self.stack,0x150,1)
        self.session.process=Mock(side_effect=RuntimeError('process changed'))
        with self.assertRaisesRegex(RuntimeError,'process changed'):self.apply()
        self.assertEqual(self.writes,[])

    def test_cancel_or_array_code_change_prevents_every_write(self):
        self.prepare()
        with self.assertRaises(InterruptedError):self.apply(cancelled=lambda:True)
        self.assertEqual(self.writes,[])
        self.module[16]=0
        with self.assertRaisesRegex(RuntimeError,'array proof changed'):self.apply()
        self.assertEqual(self.writes,[])

    def test_changed_field_proof_does_not_block_an_independent_field(self):
        self.prepare(('run_gold','rerolls'))
        bad=replace(self.fields[1],guards=((0x30,b'wrong'),))
        self.hunt.plan=replace(self.hunt.plan,fields=(self.fields[0],bad))
        self.fields=(self.fields[0],bad)
        result=self.apply()
        self.assertEqual(len(result['held']),1)
        self.assertEqual(len(result['errors']),1)
        self.assertEqual(len(self.writes),1)

    def test_value_race_skips_only_that_field(self):
        self.prepare(('run_gold','rerolls'))
        read=self.memory.read
        hits=0
        def racing(address,size):
            nonlocal hits
            if address==self.pointer+0x15C and size==4:
                hits+=1
                if hits==2:struct.pack_into('<I',self.hero,0x15C,7)
            return read(address,size)
        self.memory.read=racing
        result=self.apply()
        self.assertEqual(result['held'],{'rerolls':9999})
        self.assertIn('run_gold',result['errors'])
        self.assertEqual(struct.unpack_from('<I',self.hero,0x15C)[0],7)

    def test_ambiguous_or_incomplete_initial_discovery_cannot_write(self):
        self.prepare(('run_gold',))
        self.freezer.reset()
        partial=dict(self.result,partial=True)
        self.assertEqual(self.freezer.observe(self.hunt,partial),())
        self.assertEqual(self.freezer.observe(self.hunt,partial),())
        self.assertTrue(self.freezer.observe(self.hunt,self.result))
        self.assertEqual(self.freezer.observe(self.hunt,dict(self.result,ambiguous=True)),())
        with self.assertRaises(RuntimeError):self.apply()
        self.assertEqual(self.writes,[])

    def test_unmapped_or_malformed_fields_are_not_promoted(self):
        self.prepare(('run_gold',))
        good=self.fields[0]
        bad=(replace(good,key='unknown'),replace(good,offset=self.plan.stride),
             replace(good,maximum=0xFFFFFFFF),replace(good,fmt='Q'))
        self.hunt.plan=replace(self.hunt.plan,fields=bad)
        self.assertEqual(self.freezer.observe(self.hunt,self.result),())
        with self.assertRaisesRegex(RuntimeError,'active search plan'):self.apply()
        self.assertEqual(self.writes,[])
