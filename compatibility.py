"""Read-only discovery of compatible Reborn code/layout families."""
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import struct
import time
from functools import lru_cache


@dataclass
class Layout:
    digest: str
    config: int
    signatures: dict
    stride: int
    vector: int
    index: int
    name: int
    stats: int
    rerolls: int


def resolve(path):
    path = Path(path).resolve()
    state = path.stat()
    # File identity catches atomic updates; bounded freshness also catches an
    # in-place rewrite whose filesystem timestamps were retained/coarsened.
    return _resolve(str(path), state.st_size, state.st_mtime_ns, state.st_ctime_ns,
                    state.st_ino, int(time.monotonic() // 5))


@lru_cache(maxsize=4)
def _resolve(path, size, modified, created, file_id, generation):
    blob = Path(path).read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    state = Path(path).stat()
    if (state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino) != (size, modified, created, file_id):
        raise RuntimeError('Game executable changed during layout discovery. Retry.')
    if digest == 'ab7b6332a6c47e4c29188e44932939e560b274cf06e0f212cb3ada119a3a3d35':
        guards = {0x7139363: '0f958208020000', 0x71417F8: '0fb68208020000',
                  0x6EB6123: '0fb635ee968303', 0x6D8750F: '488b89600e0000',
                  0x6D8752F: '4869c998170000', 0x6D8753A: '0f10840a70160000',
                  0x6D7D728: '488d875c010000', 0x6DBEF36: '8b8878160000',
                  0x6D7D7F1: '4881c744170000', 0x6D7D79A: '488d8f00010000'}
        return Layout(digest, 0xA6EF610, {r: bytes.fromhex(s) for r, s in guards.items()},
                      0x1798, 0xBA8, 0xE60, 0x1038, 0x1670, 0x1744)
    try:
        if blob[:2] != b'MZ':
            raise ValueError('not a PE executable')
        pe = struct.unpack_from('<I', blob, 60)[0]
        if blob[pe:pe + 4] != b'PE\0\0' or struct.unpack_from('<H', blob, pe + 4)[0] != 0x8664:
            raise ValueError('not a Windows x64 executable')
        optional = struct.unpack_from('<H', blob, pe + 20)[0]
        sections = [struct.unpack_from('<8sIIII', blob, pe + 24 + optional + i * 40)
                    for i in range(struct.unpack_from('<H', blob, pe + 6)[0])]
        text = next(s for s in sections if s[0].startswith(b'.text'))
        data = next(s for s in sections if s[0].startswith(b'.data'))
        code = blob[text[4]:text[4] + text[3]]
        if len(code) != text[3]:
            raise ValueError('truncated code section')
        signatures = {}

        def unique(pattern):
            found = list(re.finditer(pattern, code, re.DOTALL))
            if len(found) != 1:
                raise ValueError(f'expected one code anchor, found {len(found)}')
            match = found[0]
            signatures[text[2] + match.start()] = match.group()
            return match

        unique(re.escape(bytes.fromhex('0f958208020000')))
        unique(re.escape(bytes.fromhex('0fb68208020000413a8008020000')))
        # Independent RIP-relative readers must agree on the configuration block.
        readers = defaultdict(list)
        for match in re.finditer(b'\x0f\xb6\x35....', code, re.DOTALL):
            rva = text[2] + match.start()
            target = rva + 7 + struct.unpack_from('<i', match.group(), 3)[0]
            config = target - 0x208
            if config % 8 == 0 and data[2] <= config and config + 0x234 <= data[2] + data[1]:
                readers[config].append((rva, match.group()))
        candidates = []
        for config, values in readers.items():
            if len(values) < 2:
                continue
            guards = {}
            corroborated = set()
            # 32-byte placement was incidental to older linkers. For other
            # 8-byte-aligned blocks, corroborate the flag with the initialization
            # state and clock-speed readers in the same helper, not live values.
            for offset, pattern, displacement, length in (
                    (0x230, rb'\x8b\x05....', 2, 6),
                    (0x1F8, rb'\xf3\x0f\x10\x05....', 4, 8)):
                for rva, _ in values:
                    start = max(0, rva - text[2] - 32)
                    window = code[start:rva - text[2] + 0x140]
                    for match in re.finditer(pattern, window, re.DOTALL):
                        address = text[2] + start + match.start()
                        target = address + length + struct.unpack_from('<i', match.group(), displacement)[0]
                        if target == config + offset:
                            guards[address] = match.group()
                            corroborated.add(offset)
            if config % 32 and corroborated != {0x230, 0x1F8}:
                continue
            candidates.append((config, values, guards))
        if len(candidates) != 1:
            raise ValueError(f'configuration readers do not identify a unique block (found {len(candidates)} verified blocks)')
        config, values, guards = candidates[0]
        signatures.update(values)
        signatures.update(guards)
        # The stats serializer independently establishes vector, index, stride,
        # and four contiguous 16-byte stat blocks. Capture relocations as guards.
        m = unique(rb'\x48\x8b\x89(.{4})\x49\x8b\x95(.{4})\x48\x39\xd1'
                   rb'\x0f\x83.{4}\x49\x8b\x95(.{4})\x48\x69\xc9(.{4})'
                   rb'\x48\x8d\x1c\x0a\x0f\x10\x84\x0a(.{4})'
                   rb'\xf3\x0f\x6f\x8c\x0a(.{4})\xf3\x0f\x6f\x94\x0a(.{4})'
                   rb'\xf3\x0f\x6f\x9c\x0a(.{4})')
        index, count, pointer, stride, stats, second, third, fourth = [struct.unpack('<I', v)[0] for v in m.groups()]
        if count != pointer + 8 or (second, third, fourth) != (stats + 16, stats + 32, stats + 48):
            raise ValueError('inconsistent player serializer')
        # These name/header families have been verified independently. Unknown
        # structural changes stay blocked, even if a few opcodes still match.
        families = {0x1798: (0x1038, 0x1670, 0x1744), 0x1858: (0x10E0, 0x1740, 0x1804),
                    0x1870: (0x10F8, 0x1758, 0x181C)}
        name, expected_stats, rerolls = families.get(stride, (None, None, None))
        if stats != expected_stats or not 0x800 <= pointer < index < 0x1200:
            raise ValueError('player layout family has not been verified')
        # The compiler now holds the same hero in RBX instead of RDI. Match
        # complete, consistent register variants rather than wildcarding ModRM.
        gold_patterns = []
        for base in (3, 7):
            gold_patterns.append(re.escape(b'\x48\x8d' + bytes([0x80 | base]) + struct.pack('<I', 0x15C)
                                           + b'\x48\x8d' + bytes([0x88 | base])) + rb'.{4}'
                                 + re.escape(b'\x48\x8d' + bytes([0x90 | base]) + struct.pack('<I', 0x150)
                                             + b'\x4c\x8d' + bytes([0x80 | base])) + rb'.{4}')
        gold = unique(b'(?:' + b'|'.join(gold_patterns) + b')')
        base = gold.group()[2] & 7
        # The reroll address in the same run serializer must match the family.
        nearby = code[gold.start():gold.start() + 0x150]
        rr = b'\x48\x81' + bytes([0xC0 | base]) + struct.pack('<I', rerolls)
        if nearby.count(rr) != 1:
            raise ValueError('reroll serializer changed')
        signatures[text[2] + gold.start() + nearby.index(rr)] = rr
        if stride == 0x1870:
            # Independently verify the new family's name string and unchanged
            # health/XP header before allowing Player's shared field mapping.
            unique(re.escape(b'\x48\x8d\x3c\x08\x48\x81\xc7' + struct.pack('<I', name))
                   + rb'\xc7\x85.{4}\x00{4}'
                   + re.escape(b'\x48\x83\xbc\x08' + struct.pack('<I', name) + b'\xff'))
            for modrm, offset in ((0x90 | base, 0xF8), (0x88 | base, 0x100),
                                  (0x80 | base, 0x154), (0x80 | base, 0x158)):
                raw = b'\x48\x8d' + bytes([modrm]) + struct.pack('<I', offset)
                if nearby.count(raw) != 1:
                    raise ValueError('player health/XP serializer changed')
                signatures[text[2] + gold.start() + nearby.index(raw)] = raw
        unique(re.escape(b'\x8b\x88' + struct.pack('<I', stats + 8)))
        return Layout(digest, config, signatures, stride, pointer - 8, index, name, stats, rerolls)
    except (ValueError, StopIteration, struct.error, IndexError) as exc:
        raise RuntimeError(f'Unsupported Reborn build {digest[:12]}: {exc}. Memory controls are blocked.') from exc
