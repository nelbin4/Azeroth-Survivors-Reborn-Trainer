import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from session_role import SessionRole
from test_automatic_hunt import FakeMemory


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.raw=bytearray(0x10000)
        self.memory=FakeMemory({0x10000:self.raw})
        self.env,self.state,self.global_state,self.lobby=0x11000,0x12000,0x13000,0x15000
        self.key_g=self.text(0x18000,b'_G')
        self.key_lobby=self.text(0x18100,b'AZS_RUN_LOBBY')
        self.key_role=self.text(0x18200,b'role')
        self.host=self.text(0x18300,b'host');self.guest=self.text(0x18400,b'guest');self.none=self.text(0x18500,b'none')
        self.make_table(self.env,0x14000,3)
        self.node(0x140c8,self.env,5,self.key_g)
        self.node(0x140a0,self.lobby,5,self.key_lobby)
        self.make_table(self.lobby,0x16000,1)
        self.node(0x16028,self.host,4,self.key_role)
        self.raw[self.state-0x10000+8]=8
        self.pack(self.state+32,'Q',self.global_state)
        self.pack(self.state+120,'QI',self.env,5)
        self.pack(self.global_state+176,'Q',self.state)
        self.session=SimpleNamespace(process=Mock())
        self.reader=SessionRole(seconds=10)

    def pack(self,address,fmt,*values):struct.pack_into('<'+fmt,self.raw,address-0x10000,*values)

    def text(self,address,value):
        self.raw[address-0x10000+8]=4
        self.pack(address+16,'Q',len(value))
        self.raw[address-0x10000+24:address-0x10000+25+len(value)]=value+b'\0'
        return address

    def make_table(self,address,nodes,log):
        self.raw[address-0x10000+8]=5;self.raw[address-0x10000+12]=log
        self.pack(address+32,'QQ',nodes,nodes+(1<<log)*40)

    def node(self,address,value,tag,key):
        self.pack(address,'QI',value,tag);self.pack(address+16,'QI',key,4)

    def discover(self):
        return self.reader.step(self.session, self.memory, regions=[(0x10000, len(self.raw))])

    def test_finds_only_a_main_thread_rooted_live_role(self):
        self.assertEqual(self.discover(),'host')
        self.assertEqual(self.reader.state,self.state)

    def test_host_guest_and_none_changes_are_read_without_rescanning(self):
        self.discover()
        for value,expected in ((self.guest,'guest'),(self.none,'none'),(self.host,'host')):
            self.node(0x16028,value,4,self.key_role)
            self.assertEqual(self.reader.read(self.memory),expected)

    def test_new_lobby_table_and_global_rehash_are_followed(self):
        self.discover();self.make_table(self.env,0x17000,3)
        self.node(0x170c8,self.env,5,self.key_g)
        self.node(0x170a0,self.lobby,5,self.key_lobby)
        self.node(0x16028,self.guest,4,self.key_role)
        self.assertEqual(self.reader.read(self.memory),'guest')

    def test_mid_read_role_change_never_returns_the_previous_role(self):
        self.assertEqual(self.discover(), 'host')
        read = self.memory.read

        def changing(address, size):
            if address == self.host:
                self.node(0x16028, self.guest, 4, self.key_role)
            return read(address, size)

        self.memory.read = changing
        with self.assertRaisesRegex(ValueError, 'Lobby changed'):
            self.reader.read(self.memory)
        self.assertEqual(self.reader.read(self.memory), 'guest')

    def test_stale_runtime_and_invalid_role_never_authorize_host(self):
        self.discover();self.pack(self.global_state+176,'Q',self.state+8)
        with self.assertRaises(ValueError):self.reader.read(self.memory)
        self.pack(self.global_state+176,'Q',self.state)
        self.node(0x16028,self.key_g,4,self.key_role)
        with self.assertRaises(ValueError):self.reader.read(self.memory)

    def test_only_matching_text_without_runtime_root_does_not_authorize(self):
        self.pack(self.global_state+176,'Q',0)
        self.assertEqual(self.discover(),'unknown')

    def test_scan_is_cancellable_and_uses_a_byte_budget(self):
        with self.assertRaises(InterruptedError):self.reader.step(self.session,self.memory,cancelled=lambda:True,regions=[(0x10000,len(self.raw))])
        self.reader.budget=4096
        self.assertEqual(self.reader.step(self.session,self.memory,regions=[(0x10000,len(self.raw))]),'unknown')
        self.assertEqual(self.reader.cursor,0x11000)

    def test_ambiguous_rooted_runtimes_remain_unknown(self):
        state, owner = 0x12100, 0x13200
        self.raw[state-0x10000+8] = 8
        self.pack(state+32, 'Q', owner)
        self.pack(state+120, 'QI', self.env, 5)
        self.pack(owner+176, 'Q', state)
        self.assertEqual(self.discover(), 'unknown')
        self.assertIsNone(self.reader.state)

    def test_transient_role_read_does_not_discard_root(self):
        self.assertEqual(self.discover(), 'host')
        self.node(0x16028, self.key_g, 4, self.key_role)
        self.assertEqual(self.discover(), 'unknown')
        self.assertEqual(self.reader.state, self.state)
        self.node(0x16028, self.host, 4, self.key_role)
        self.assertEqual(self.discover(), 'host')
