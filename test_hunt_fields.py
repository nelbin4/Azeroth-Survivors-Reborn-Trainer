import struct
import unittest

from hunt_fields import discover_fields, RESOURCE_PREFIX, RESOURCE_LABELS
from test_compatibility import fixture


def resource_fixture():
    blob=fixture()
    u=lambda x:struct.pack('<I',x)
    def put(rva,raw):
        file=0x400+rva-0x1000 if rva<0x2000 else 0x1400+rva-0x5000
        blob[file:file+len(raw)]=raw
    put(0x1020,b'\x8b\x09\xc3')
    put(0x1060,b'\x48\x8b\x09\xc3')
    put(0x5D00,RESOURCE_PREFIX+RESOURCE_LABELS)
    output=bytearray()
    def lea(prefix,target):
        at=0x1380+len(output)
        output.extend(prefix+struct.pack('<i',target-at-7))
    output.extend(b'\x48\x8d\x87'+u(0x15C)+b'\x48\x8d\x8f'+u(0x10B8)
                  +b'\x48\x8d\x97'+u(0x150)+b'\x4c\x8d\x87'+u(0x1810))
    lea(b'\x4c\x8d\x05',0x1020)
    def store(reg,index):
        at=0x20+16*index
        output.extend(b'\x48\x89'+bytes([reg])+u(at))
        output.extend(b'\x4c\x89\x85'+u(at+8))
    store(0x85,5)
    output.extend(b'\x48\x8d\x97'+u(0xF8))
    lea(b'\x4c\x8d\x05',0x1060)
    store(0x95,7)
    output.extend(b'\x48\x8d\x8f'+u(0x100))
    store(0x8D,8)
    output.extend(b'\x48\x81\xc7'+u(0x1804))
    lea(b'\x4c\x8d\x05',0x1020)
    store(0xBD,11)
    lea(b'\x48\x8d\x15',0x5D00)
    output.extend(b'\x4c\x8d\x85'+u(0x20)+b'\xe8'+bytes(4))
    put(0x1380,output)
    put(0x1700,b'\x48\x8b\x89'+u(0xE38)+b'\x49\x8b\x97'+u(0xB90)
        +b'\x48\x39\xd1\x0f\x83'+u(8)+b'\x49\x8b\x87'+u(0xB88)
        +b'\x48\x69\xc9'+u(0x1858)+b'\x4c\x8d\x2c\x08'
        +b'\x49\x8d\x85'+u(0x1810)+b'\x49\x81\xc5'+u(0x1804))
    return blob


def fields(blob):
    p=struct.unpack_from('<I',blob,60)[0]
    optional=struct.unpack_from('<H',blob,p+20)[0]
    sections=[struct.unpack_from('<8sIIII',blob,p+24+optional+i*40)
              for i in range(struct.unpack_from('<H',blob,p+6)[0])]
    return discover_fields(blob,sections,blob[0x400:0x1400],0x1000,(0xE38,0xB90,0xB88,0x1858),0x1740)


class FieldProofTests(unittest.TestCase):
    def test_proves_resources_without_complete_stat_layout(self):
        found={p.key:(p.offset,p.fmt) for p in fields(resource_fixture())}
        self.assertEqual(found,{'run_gold':(0x15C,'I'),'health':(0xF8,'Q'),
                                'max_health':(0x100,'Q'),'rerolls':(0x1804,'I')})

    def test_changed_labels_or_argument_order_never_prove_wrong_fields(self):
        blob=resource_fixture();at=blob.index(b';hpm=');blob[at:at+5]=b';bad='
        self.assertEqual(fields(blob),())
        blob=resource_fixture();old=b'\x48\x8d\x8f'+struct.pack('<I',0x100)
        at=blob.index(old,0x780);blob[at+3:at+7]=struct.pack('<I',0x108)
        self.assertEqual({p.key for p in fields(blob)},{'run_gold','rerolls'})

    def test_unknown_scalar_type_blocks_only_dependent_fields(self):
        blob=resource_fixture();blob[0x460:0x463]=b'\x8b\x09\xc3' # u32 instead of u64
        self.assertEqual({p.key for p in fields(blob)},{'run_gold','rerolls'})
        blob=resource_fixture()
        # Only rerolls loses its integer type proof; other resources remain proven.
        starts=[i for i in range(0x780,0x980) if blob[i:i+3]==b'\x4c\x8d\x05']
        at=starts[2]
        rva=at-0x400+0x1000
        blob[at+3:at+7]=struct.pack('<i',0x1080-rva-7)
        self.assertEqual({p.key for p in fields(blob)},{'run_gold','health','max_health'})

    def test_missing_or_conflicting_independent_array_evidence_blocks_resources(self):
        blob=resource_fixture();blob[0xB00:0xB80]=bytes(128)
        self.assertEqual(fields(blob),())
        blob=resource_fixture();blob[0xB07:0xB0E]=b'\x49\x8b\x97'+struct.pack('<I',0xB98)
        self.assertEqual(fields(blob),())
