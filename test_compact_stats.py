import struct
import unittest

from compact_stats import DESCRIPTION,LABELS,KEYS,discover_compact_stats,unary_summary
from code_search import stat_serializers
from test_compatibility import fixture


DEFAULTS={'damage':8,'crit':16,'crit_damage':20,'cooldown':12,'area':52,'targets':56,
          'armor':28,'regen':32,'speed':24,'pickup':44,'gold_bonus':48,'luck':60}


def compact_fixture(offsets=None,ambiguous=False,unreachable=False,echo=False,partial=False):
    offsets=dict(DEFAULTS,**(offsets or {}))
    blob=fixture()
    def put(rva,data):
        at=0x400+rva-0x1000 if rva<0x2000 else 0x1400+rva-0x5000
        blob[at:at+len(data)]=data
    put(0x1020,b'\x8b\x09\xc3')
    put(0x1060,b'\x48\x8b\x09\xc3')
    put(0x5D00,DESCRIPTION)
    anchor=stat_serializers(bytes(blob[0x400:0x1400]),0x1000)[0]
    entry=anchor.rva+len(anchor.raw)
    output=bytearray(bytes.fromhex('660f7f5d00660f7f55f0660f7f4de00f2945d0'))
    u=lambda n:struct.pack('<i',n)
    if partial:
        output.extend(bytes.fromhex('8b45e88845d9'))
    if echo:
        put(0x5C90,struct.pack('<f',100))
        output.extend(bytes.fromhex('488b8b00180000488b930818000031c04921d24e8d1cd5000000004889ce4c29de807ef80d7402eb1649f7daf3420f1044d1fc'))
        at=entry+len(output)
        output.extend(b'\xf3\x0f\x59\x05'+u(0x5C90-at-8)+bytes.fromhex('f30f2cc08985d8030000'))
    def lea(prefix,target):
        output.extend(prefix+u(target-entry-len(output)-7))
    lea(b'\x48\x8d\x0d',0x1020)
    for index,label in enumerate(LABELS):
        if label not in KEYS:continue
        key=KEYS[label]
        if key not in offsets and not (echo and key=='echo'):continue
        disp=0x3D8 if key=='echo' else -0x30+offsets[key]
        if key=='damage' and (ambiguous or unreachable):
            output.extend(b'\x8b\x85'+u(disp))
            output.extend(b'\x85\xff\x74\x03' if ambiguous else b'\xeb\x03')
            output.extend(b'\x8b\x45\xe8')
            output.extend(b'\x89\x85'+u(0x340))
            disp=0x340
        if key=='regen':
            output.extend(b'\x8b\x85'+u(disp)+b'\x48\x89\x85'+u(0x380))
            disp=0x380
            lea(b'\x48\x8d\x15',0x1060)
        output.extend(b'\x48\x8d\x85'+u(disp)+b'\x48\x89\x85'+u(0x18+16*index))
        output.extend((b'\x48\x89\x95' if key=='regen' else b'\x48\x89\x8d')+u(0x20+16*index))
    lea(b'\x48\x8d\x15',0x5D00)
    output.extend(b'\x4c\x8d\x85'+u(0x18)+b'\xe8'+bytes(4))
    put(entry,output)
    return blob,anchor


def discover(blob,anchor):
    sections=[(b'.text',0x1000,0x1000,0x1000,0x400),(b'.data',0x1000,0x5000,0x1000,0x1400)]
    return discover_compact_stats(blob,sections,bytes(blob[0x400:0x1400]),0x1000,anchor.array,anchor.value)


class CompactStatsTests(unittest.TestCase):
    def test_named_echo_traces_to_a_verified_float_table_entry(self):
        blob,anchor=compact_fixture(echo=True)
        fields,guards=discover(blob,anchor)
        self.assertEqual(fields['echo'],('map',0x1800,0x1808,0x1818,13))
        self.assertIn(0x5C90,guards)
        blob[0x2090:0x2094]=struct.pack('<f',90)
        self.assertNotIn('echo',discover(blob,anchor)[0])
    def test_derives_all_fields_from_named_arguments_and_vector_copy(self):
        blob,anchor=compact_fixture()
        fields,guards=discover(blob,anchor)
        self.assertEqual(fields,DEFAULTS)
        self.assertIn(0x5D00,guards)
        self.assertIn(anchor.rva,guards)

    def test_reordered_stats_are_derived_not_assumed(self):
        offsets=dict(DEFAULTS,damage=44,pickup=8)
        blob,anchor=compact_fixture(offsets)
        self.assertEqual(discover(blob,anchor)[0],offsets)

    def test_duplicate_role_offsets_block_only_conflicting_fields(self):
        blob,anchor=compact_fixture(dict(speed=8))
        fields,_=discover(blob,anchor)
        self.assertNotIn('damage',fields)
        self.assertNotIn('speed',fields)
        self.assertEqual(len(fields),10)

    def test_partial_stack_overwrite_cannot_erase_a_conflicting_origin(self):
        blob,anchor=compact_fixture(partial=True)
        self.assertNotIn('damage',discover(blob,anchor)[0])

    def test_conflicting_branch_origins_block_only_the_ambiguous_field(self):
        blob,anchor=compact_fixture(ambiguous=True)
        fields,_=discover(blob,anchor)
        self.assertNotIn('damage',fields)
        self.assertEqual(len(fields),11)

    def test_unreachable_overwrite_does_not_pollute_a_field(self):
        blob,anchor=compact_fixture(unreachable=True)
        self.assertEqual(discover(blob,anchor)[0],DEFAULTS)

    def test_wrong_labels_types_and_array_contracts_are_rejected(self):
        blob,anchor=compact_fixture()
        blob[0x2101]=ord('x')
        self.assertEqual(discover(blob,anchor)[0],{})
        blob,anchor=compact_fixture()
        blob[0x420:0x423]=b'\x48\x8b\x09' # wrong integer width
        self.assertEqual(set(discover(blob,anchor)[0]),{'regen'})
        blob,anchor=compact_fixture()
        blob[0x420:0x426]=bytes.fromhex('4889d18b09c3') # replaced argument pointer
        self.assertEqual(set(discover(blob,anchor)[0]),{'regen'})
        blob,anchor=compact_fixture()
        blob[0x680]=0x90
        self.assertEqual(discover(blob,anchor)[0],{})

    def test_unary_helper_summary_requires_a_closed_single_input_function(self):
        valid=bytes.fromhex('660f7ec289d083e001740289d0660f6ec2c3')+bytes(128)
        self.assertIsNotNone(unary_summary(lambda r,n:valid[:n],0x1000))
        extra=valid.replace(bytes.fromhex('660f7ec2'),bytes.fromhex('660f7eca'),1)
        self.assertIsNone(unary_summary(lambda r,n:extra[:n],0x1000))
        external=bytes.fromhex('e800000000c3')+bytes(128)
        self.assertIsNone(unary_summary(lambda r,n:external[:n],0x1000))
