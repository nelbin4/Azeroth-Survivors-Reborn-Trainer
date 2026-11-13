"""Read-only PE inspection for the Reborn trainer (requires capstone and numpy)."""
import argparse
import struct
import json
import re
from pathlib import Path

import capstone
import numpy as np


class Image:
    def __init__(self, path):
        self.data = Path(path).read_bytes()
        pe = struct.unpack_from('<I', self.data, 60)[0]
        count = struct.unpack_from('<H', self.data, pe + 6)[0]
        optional = struct.unpack_from('<H', self.data, pe + 20)[0]
        self.sections = [struct.unpack_from('<8sIIII', self.data, pe + 24 + optional + i * 40) for i in range(count)]
        self.base = struct.unpack_from('<Q', self.data, pe + 48)[0]
        self.decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)

    def read(self, rva, size):
        for _, _, address, length, offset in self.sections:
            if address <= rva < address + length:
                return self.data[offset + rva - address:offset + rva - address + size]
        raise ValueError('RVA outside sections')

    def disasm(self, rva, size):
        for ins in self.decoder.disasm(self.read(rva, size), self.base + rva):
            print(f'{ins.address - self.base:08x}  {ins.bytes.hex():24s} {ins.mnemonic:8s} {ins.op_str}')

    def refs(self, target):
        _, _, address, length, offset = self.sections[0]
        code = self.data[offset:offset + length]
        for shift in range(4):
            values = np.frombuffer(code[shift:shift + (len(code) - shift) // 4 * 4], dtype='<i4')
            indexes = np.arange(len(values), dtype=np.int64) * 4 + shift
            for index in indexes[values.astype(np.int64) + indexes + 4 + address == target]:
                rva = address + int(index)
                print(f'REF {rva:x}')
                self.disasm(rva - 19, 100)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['refs', 'disasm', 'search'])
    parser.add_argument('rva')
    parser.add_argument('--size', type=lambda s: int(s, 16), default=256)
    parser.add_argument('--game', default='D:/Azeroth Survivors/AzerothSurvivorsGame.exe')
    args = parser.parse_args()
    image = Image(args.game)
    if args.command == 'search':
        for match in list(re.finditer(re.escape(args.rva.encode()), image.data))[:12]:
            off = match.start()
            address = next((va + off - rp for _, _, va, rs, rp in image.sections if rp <= off < rp + rs), 0)
            print(json.dumps(dict(rva=hex(address), context=image.data[max(0, off - 80):off + 300].decode('utf-8', 'replace'))))
    elif args.command == 'refs':
        image.refs(int(args.rva, 16))
    else:
        image.disasm(int(args.rva, 16), args.size)
