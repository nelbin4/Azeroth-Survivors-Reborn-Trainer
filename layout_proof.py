"""Bounded decoding of the game's named Stats formatter, without executing it."""
from bisect import bisect_right
import re
import struct

from capstone import Cs, CS_ARCH_X86, CS_MODE_64, CS_AC_WRITE
from capstone.x86 import X86_OP_REG, X86_OP_IMM, X86_OP_MEM


STAT_LABELS = {
    'damage_pct': 'damage', 'cooldown_pct': 'cooldown', 'crit_pct': 'crit',
    'crit_damage_pct': 'crit_damage', 'speed_pct': 'speed', 'armor_pct': 'armor',
    'regen': 'regen', 'pickup_pct': 'pickup', 'gold_pct': 'gold_bonus',
    'area_pct': 'area', 'extra_targets': 'targets', 'luck_pct': 'luck',
}


class MetadataUnavailable(ValueError):
    """An older executable lacks the metadata needed for independent proof."""


def discover_stat_fields(blob, sections, code, text_rva):
    """Return named u32 fields and loaded-image guards from one verified formatter."""
    def read(rva, size):
        for _, _, address, length, offset in sections:
            if address <= rva and rva + size <= address + length:
                return blob[offset + rva - address:offset + rva - address + size]
        return b''

    pdata = next((s for s in sections if s[0].rstrip(b'\0') == b'.pdata'), None)
    if not pdata:
        raise MetadataUnavailable('named stat formatter has no function boundaries')
    functions = list(struct.iter_unpack('<III', blob[pdata[4]:pdata[4] + pdata[3] // 12 * 12]))
    functions = sorted((start, end) for start, end, _ in functions if text_rva <= start < end <= text_rva + len(code))
    starts = [f[0] for f in functions]
    labels = set()
    for section in sections:
        if section[0].rstrip(b'\0') == b'.text':
            continue
        raw = blob[section[4]:section[4] + section[3]]
        labels.update(section[2] + m.start() for m in re.finditer(b'damage_pct', raw))
    candidates = set()
    for match in re.finditer(rb'\x48\x8d\x15....', code, re.DOTALL):
        rva = text_rva + match.start()
        target = rva + 7 + struct.unpack_from('<i', match.group(), 3)[0]
        if target in labels:
            index = bisect_right(starts, rva) - 1
            if index >= 0 and functions[index][0] <= rva < functions[index][1]:
                candidates.add(functions[index])
    if len(candidates) > 16:
        raise ValueError('too many named stat formatter candidates')
    if not candidates:
        raise MetadataUnavailable('named stat formatter references were not found')
    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    decoder.detail = True
    pe = struct.unpack_from('<I', blob, 60)[0]
    image_base = struct.unpack_from('<Q', blob, pe + 48)[0]
    verified = []
    for start, end in sorted(candidates):
        if end - start > 0x2000:
            continue
        raw = code[start - text_rva:end - text_rva]
        result = _decode_fields(decoder, raw, start, read)
        if result:
            fields, used, names, formats = result
            guards = {start: raw[:used]}
            guards.update(names)
            if not _verify_formats(formats, image_base, text_rva, len(code), read, guards):
                continue
            verified.append((fields, guards))
    if len(verified) != 1:
        raise ValueError(f'named stat formatter is not unique (found {len(verified)})')
    return verified[0]


def _verify_formats(formats, image_base, text_rva, code_size, read, guards):
    # Rust can format either u32 directly or a stack reference to u32. Confirm
    # the concrete vtable size/alignment and the decoder's actual 32-bit load.
    for callback, depth in set(formats.values()):
        if not isinstance(callback, tuple) or callback[0] != 'address' or depth not in (0, 1):
            return False
        metadata = read(callback[1] + 8, 24)
        if len(metadata) != 24:
            return False
        size, alignment, function = struct.unpack('<3Q', metadata)
        if (size, alignment) != ((4, 4) if depth == 0 else (8, 8)):
            return False
        rva = function - image_base
        if not text_rva <= rva < text_rva + code_size - 128:
            return False
        raw = read(rva, 128)
        pattern = b'\x8b' + (b'\x09' if depth == 0 else b'\x08') + b'\x41\xb8\x0a\x00\x00\x00'
        position = raw.find(pattern, 0, 0x60)
        if position < 0 or (depth == 1 and b'\x48\x8b\x01' not in raw[:position]):
            return False
        # Do not guard the absolute function pointer: Windows relocates it.
        guards[callback[1] + 8] = metadata[:16]
        guards[rva] = raw[:position + len(pattern)]
    return True


def _decode_fields(decoder, raw, start, read):
    # Track only field addresses, stack addresses and constants. Never evaluate
    # game values or follow an arbitrary pointer. Unsupported writes erase facts.
    registers = {'rcx': ('field', 0), 'rsp': ('stack', 0)}
    stack = {}
    fields = {}
    callbacks = {}
    names = {}

    def reg(ins, number):
        name = ins.reg_name(number)
        if not name:
            return None
        for full, aliases in (('rax', ('al', 'ah', 'ax', 'eax')), ('rbx', ('bl', 'bh', 'bx', 'ebx')),
                              ('rcx', ('cl', 'ch', 'cx', 'ecx')), ('rdx', ('dl', 'dh', 'dx', 'edx')),
                              ('rsi', ('sil', 'si', 'esi')), ('rdi', ('dil', 'di', 'edi')),
                              ('rbp', ('bpl', 'bp', 'ebp')), ('rsp', ('spl', 'sp', 'esp'))):
            if name in aliases:
                return full
        if name.startswith('e') and len(name) == 3:
            return 'r' + name[1:]
        if re.fullmatch(r'r\d+[dwb]', name):
            return name[:-1]
        return name

    def address(ins, operand):
        m = operand.mem
        if m.index:
            return None
        if ins.reg_name(m.base) == 'rip':
            return ('address', ins.address + ins.size + m.disp)
        base = registers.get(reg(ins, m.base))
        return (base[0], base[1] + m.disp) if isinstance(base, tuple) else None

    def invalidate(offset, size):
        for saved in list(stack):
            if saved < offset + size and offset < saved + 8:
                del stack[saved]

    for ins in decoder.disasm(raw, start):
        operands = ins.operands
        if ins.mnemonic == 'call':
            label, length, pair = (registers.get(k) for k in ('rdx', 'r8', 'r9'))
            if isinstance(label, tuple) and label[0] == 'address' and isinstance(length, int) and 1 <= length <= 64:
                data = read(label[1], length)
                name = data.decode('ascii', errors='replace')
                if name in STAT_LABELS:
                    if not isinstance(pair, tuple) or pair[0] != 'stack':
                        return None
                    value, callback = stack.get(pair[1]), stack.get(pair[1] + 8)
                    depth = 0
                    while isinstance(value, tuple) and value[0] == 'stack' and depth < 2:
                        value = stack.get(value[1])
                        depth += 1
                    if not isinstance(value, tuple) or value[0] != 'field' or callback is None:
                        return None
                    if name in fields or value[1] % 4 or not 0 <= value[1] <= 60:
                        return None
                    fields[name] = value[1]
                    callbacks[name] = (callback, depth)
                    names[label[1]] = data
                    if len(fields) == len(STAT_LABELS):
                        if len(set(fields.values())) != len(fields):
                            return None
                        return ({STAT_LABELS[k]: v for k, v in fields.items()},
                                ins.address + ins.size - start, names, callbacks)
            for name in ('rax', 'rcx', 'rdx', 'r8', 'r9', 'r10', 'r11'):
                registers.pop(name, None)
            continue
        if ins.mnemonic.startswith('j') or ins.mnemonic in ('ret', 'loop'):
            return None
        if ins.mnemonic == 'push':
            pointer = registers.get('rsp')
            if not isinstance(pointer, tuple) or pointer[0] != 'stack':
                return None
            registers['rsp'] = ('stack', pointer[1] - 8)
            continue
        if len(operands) < 2:
            continue
        dest, source = operands[:2]
        value = None
        if ins.mnemonic == 'lea' and source.type == X86_OP_MEM:
            value = address(ins, source)
        elif ins.mnemonic == 'mov':
            if source.type == X86_OP_REG:
                value = registers.get(reg(ins, source.reg))
            elif source.type == X86_OP_IMM:
                value = source.imm
            elif source.type == X86_OP_MEM:
                location = address(ins, source)
                if location and location[0] == 'stack':
                    value = stack.get(location[1])
        elif ins.mnemonic in ('add', 'sub') and source.type == X86_OP_IMM and dest.type == X86_OP_REG:
            old = registers.get(reg(ins, dest.reg))
            if isinstance(old, tuple):
                value = (old[0], old[1] + source.imm * (-1 if ins.mnemonic == 'sub' else 1))
        elif ins.mnemonic == 'xor' and dest.type == source.type == X86_OP_REG and dest.reg == source.reg:
            value = 0
        elif ins.mnemonic not in ('lea', 'mov', 'add', 'sub', 'xor'):
            # Fail closed if an unmodelled instruction writes a tracked register.
            for number in ins.regs_access()[1]:
                registers.pop(reg(ins, number), None)
            for operand in operands:
                if operand.type == X86_OP_MEM and operand.access & CS_AC_WRITE:
                    location = address(ins, operand)
                    if location and location[0] == 'stack':
                        invalidate(location[1], operand.size)
            continue
        if dest.type == X86_OP_REG:
            if dest.size != 8 and isinstance(value, tuple):
                value = None
            registers[reg(ins, dest.reg)] = value
        elif dest.type == X86_OP_MEM:
            location = address(ins, dest)
            if location and location[0] == 'stack':
                invalidate(location[1], dest.size)
                if dest.size == 8:
                    stack[location[1]] = value
    return None
