"""Read-only discovery of compatible Reborn code/layout families."""
from collections import defaultdict, OrderedDict
from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import struct
import time
from functools import lru_cache
from threading import RLock
from layout_proof import discover_stat_fields, MetadataUnavailable
from code_search import config_anchors, rip_readers, stat_serializers, name_serializers, resource_serializers

_PROOFS = OrderedDict()
_PROOF_LOCK = RLock()


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
    fields: dict | None = None


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
    # Refresh the complete file hash, but reuse costly instruction proofs when
    # the bytes are identical. Loaded-image guards still run before every write.
    with _PROOF_LOCK:
        cached = _PROOFS.get(digest)
        if cached is not None:
            _PROOFS.move_to_end(digest)
            return cached
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

        config, native_guards = discover_native_config(code, text, data)
        signatures.update(native_guards)
        # The stats serializer independently establishes vector, index, stride,
        # and four contiguous 16-byte stat blocks. Capture relocations as guards.
        matches = stat_serializers(code, text[2])
        if len(matches) != 1:
            raise ValueError(f'expected one verified player serializer, found {len(matches)}')
        m = matches[0]
        index, count, pointer, stride = m.array
        stats = m.value
        signatures[m.rva] = m.raw
        if not (0x200 <= stride <= 0x10000 and stride % 8 == 0
                and 0x160 <= stats <= stride - 64 and stats % 4 == 0
                and 0x100 <= pointer < index < 0x10000):
            raise ValueError('player serializer bounds are inconsistent')
        # Keep verified legacy layouts as a fallback for stripped metadata.
        # Current and future layouts instead derive every writable stat by name.
        families = {0x1798: (0x1038, 0x1670, 0x1744), 0x1858: (0x10E0, 0x1740, 0x1804),
                    0x1870: (0x10F8, 0x1758, 0x181C)}
        name, expected_stats, rerolls = families.get(stride, (None, None, None))
        fields = None
        try:
            fields, field_guards = discover_stat_fields(blob, sections, code, text[2])
        except MetadataUnavailable:
            if stats != expected_stats:
                raise ValueError('player fields lack independent named metadata')
        if fields is not None:
            signatures.update(field_guards)
            # The name-copy routine must independently use the same game vector,
            # selected index and element stride as the combat stat serializer.
            names = name_serializers(code, text[2], (index, count, pointer, stride))
            if len(names) != 1:
                raise ValueError('player name and array serializers do not agree uniquely')
            candidate = names[0]
            name = candidate.value
            if name % 8 or not 0x160 <= name <= stats - 24:
                raise ValueError('player name exceeds the verified element bounds')
            signatures[candidate.rva] = candidate.raw
        # The compiler now holds the same hero in RBX instead of RDI. Match
        # complete, consistent register variants rather than wildcarding ModRM.
        gold_patterns = []
        for base in (3, 5, 6, 7):
            gold_patterns.append(re.escape(b'\x48\x8d' + bytes([0x80 | base]) + struct.pack('<I', 0x15C)
                                           + b'\x48\x8d' + bytes([0x88 | base])) + rb'.{4}'
                                 + re.escape(b'\x48\x8d' + bytes([0x90 | base]) + struct.pack('<I', 0x150)
                                             + b'\x4c\x8d' + bytes([0x80 | base])) + rb'.{4}')
        gold = unique(b'(?:' + b'|'.join(gold_patterns) + b')')
        base = gold.group()[2] & 7
        # The reroll address in the same run serializer must match the family.
        nearby = code[gold.start():gold.start() + 0x150]
        if fields is not None:
            from capstone import Cs, CS_ARCH_X86, CS_MODE_64
            resource_fields = []
            prefix = b'\x48\x81' + bytes([0xC0 | base])
            for instruction in Cs(CS_ARCH_X86, CS_MODE_64).disasm(nearby, text[2] + gold.start()):
                raw = bytes(instruction.bytes)
                if len(raw) == 7 and raw.startswith(prefix):
                    value = struct.unpack_from('<I', raw, 3)[0]
                    if value % 4 == 0 and stats + 64 <= value <= stride - 16:
                        resource_fields.append(value)
            if len(resource_fields) != 1:
                raise ValueError('reroll serializer does not identify a unique resource field')
            rerolls = resource_fields[0]
            auxiliary = struct.unpack('<I', gold.group()[-4:])[0]
            if auxiliary != rerolls + 12:
                raise ValueError('run resource serializer fields disagree')
            # A second resource routine must independently address the same
            # field through the same player array, not just repeat an immediate.
            resources = resource_serializers(code, text[2], (index, count, pointer, stride), rerolls)
            if len(resources) != 1:
                raise ValueError('resource and player array serializers do not agree uniquely')
            signatures[resources[0].rva] = resources[0].raw
        rr = b'\x48\x81' + bytes([0xC0 | base]) + struct.pack('<I', rerolls)
        if nearby.count(rr) != 1:
            raise ValueError('reroll serializer changed')
        signatures[text[2] + gold.start() + nearby.index(rr)] = rr
        if stride == 0x1870 and fields is None:
            # Independently verify the new family's name string and unchanged
            # health/XP header before allowing Player's shared field mapping.
            unique(re.escape(b'\x48\x8d\x3c\x08\x48\x81\xc7' + struct.pack('<I', name))
                   + rb'\xc7\x85.{4}\x00{4}'
                   + re.escape(b'\x48\x83\xbc\x08' + struct.pack('<I', name) + b'\xff'))
        if stride == 0x1870 or fields is not None:
            for modrm, offset in ((0x90 | base, 0xF8), (0x88 | base, 0x100),
                                  (0x80 | base, 0x154), (0x80 | base, 0x158)):
                raw = b'\x48\x8d' + bytes([modrm]) + struct.pack('<I', offset)
                if nearby.count(raw) != 1:
                    raise ValueError('player health/XP serializer changed')
                signatures[text[2] + gold.start() + nearby.index(raw)] = raw
        damage = stats + (fields['damage'] if fields is not None else 8)
        unique(re.escape(b'\x8b\x88' + struct.pack('<I', damage))
               + (re.escape(b'\xf3\x4c\x0f\x2a\xc1') if fields is not None else b''))
        layout = Layout(digest, config, signatures, stride, pointer - 8, index, name, stats, rerolls, fields)
        with _PROOF_LOCK:
            _PROOFS[digest] = layout
            _PROOFS.move_to_end(digest)
            while len(_PROOFS) > 4:
                _PROOFS.popitem(last=False)
        return layout
    except (ValueError, StopIteration, struct.error, IndexError) as exc:
        raise RuntimeError(f'Unsupported Reborn build {digest[:12]}: {exc}. Memory controls are blocked.') from exc


@dataclass
class NativeLayout:
    digest: str
    config: int
    signatures: dict


def discover_native_config(code, text, data):
    signatures = {}
    signatures.update(config_anchors(code, text[2]))
    # Independent RIP-relative readers must agree on the configuration block.
    readers = defaultdict(list)
    for rva, raw, target in rip_readers(code, text[2], 'byte'):
        config = target - 0x208
        if config % 8 == 0 and data[2] <= config and config + 0x234 <= data[2] + data[1]:
            readers[config].append((rva, raw))
    candidates = []
    for config, values in readers.items():
        if len(values) < 2:
            continue
        guards = {}
        corroborated = set()
        # Corroborate every candidate through independent initialization
        # and clock-speed readers, regardless of register or alignment.
        for offset, kind in ((0x230, 'integer'), (0x1F8, 'float')):
            for rva, _ in values:
                start = max(0, rva - text[2] - 32)
                window = code[start:rva - text[2] + 0x140]
                for address, raw, target in rip_readers(window, text[2] + start, kind):
                    if target == config + offset:
                        guards[address] = raw
                        corroborated.add(offset)
        if corroborated != {0x230, 0x1F8}:
            continue
        candidates.append((config, values, guards))
    if len(candidates) != 1:
        raise ValueError(f'configuration readers do not identify a unique block (found {len(candidates)} verified blocks)')
    config, values, guards = candidates[0]
    signatures.update(values)
    signatures.update(guards)
    return config, signatures


_NATIVE_PROOFS = OrderedDict()


def resolve_native(path):
    path = Path(path).resolve()
    before = path.stat()
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    after = path.stat()
    fields = lambda s: (s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino)
    if fields(before) != fields(after):
        raise RuntimeError("Game image changed during native discovery.")
    with _PROOF_LOCK:
        if digest in _NATIVE_PROOFS:
            _NATIVE_PROOFS.move_to_end(digest)
            return _NATIVE_PROOFS[digest]
    pe = struct.unpack_from("<I",blob,60)[0]
    if blob[:2] != b"MZ" or blob[pe:pe+4] != b"PE\0\0" or struct.unpack_from("<H",blob,pe+4)[0] != 0x8664:
        raise ValueError("Not a Windows x64 game image.")
    optional=struct.unpack_from("<H",blob,pe+20)[0]
    count=struct.unpack_from("<H",blob,pe+6)[0]
    if not 1 <= count <= 96:
        raise ValueError("Invalid game sections.")
    sections=[struct.unpack_from("<8sIIII",blob,pe+24+optional+i*40) for i in range(count)]
    text=next(s for s in sections if s[0].startswith(b".text"))
    data=next(s for s in sections if s[0].startswith(b".data"))
    code=blob[text[4]:text[4]+text[3]]
    if len(code)!=text[3]:
        raise ValueError("Truncated game image.")
    config,guards=discover_native_config(code,text,data)
    if fields(path.stat()) != fields(before):
        raise RuntimeError("Game image changed during native discovery.")
    result=NativeLayout(digest,config,guards)
    with _PROOF_LOCK:
        _NATIVE_PROOFS[digest]=result
        while len(_NATIVE_PROOFS)>4:
            _NATIVE_PROOFS.popitem(last=False)
    return result
