"""Independent, per-field code proofs for incremental Hunt writes."""
from dataclasses import dataclass
import re
import struct

from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_REG, X86_OP_IMM, X86_OP_MEM
from code_search import register_family, resource_serializers
from layout_proof import discover_stat_fields
from player import STATS


@dataclass(frozen=True)
class FieldProof:
    key: str
    offset: int
    fmt: str
    maximum: int
    guards: tuple
    indirect: tuple = ()


# Rust's compact format description fixes the argument order independently of
# register allocation and the player-array serializer. Unknown descriptions fail closed.
RESOURCE_LABELS = (b'\x04;ph=\xc0\x04;sl=\xc0\x05;lvl=\xc0\x07;kills=\xc0'
                   b'\x06;gold=\xc0\x07;cache=\xc0\x04;hp=\xc0\x05;hpm=\xc0'
                   b'\x04;xp=\xc0\x05;xpn=\xc0\x04;rr=\xc0\x09;endless=\xc0')
RESOURCE_PREFIX = b'\x02t=\xc5 \x00\x00p\x01\x00'


def resource_candidates(blob, sections, code, text_rva, array=None, stats=None):
    """Pair typed resource formatting with a selected-player resource routine."""
    def read(rva, size):
        for _, _, address, length, offset in sections:
            if address <= rva and rva + size <= address + length:
                return blob[offset + rva - address:offset + rva - address + size]
        return b''

    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    decoder.detail = True

    def scalar_formatter(tag, width):
        if not tag or tag[0] != 'code':
            return None
        start = tag[1]
        raw = read(start, 96)
        for ins in decoder.disasm(raw, start):
            if ins.mnemonic == 'call' or ins.mnemonic.startswith('j') or ins.mnemonic == 'ret':
                return None
            ops = ins.operands
            if (ins.mnemonic == 'mov' and len(ops) == 2 and ops[0].type == X86_OP_REG
                    and ops[1].type == X86_OP_MEM and ins.reg_name(ops[1].mem.base) == 'rcx'
                    and not ops[1].mem.index and ops[1].mem.disp == 0):
                if ops[0].size == ops[1].size == width:
                    return start, raw[:ins.address + ins.size - start]
                return None
            if any(register_family(ins, reg) == 'rcx' for reg in ins.regs_access()[1]):
                return None
        return None

    candidates = []
    anchors = {}
    for base in (3, 5, 6, 7):
        # Match the argument setup, then derive the offsets from the named
        # formatter slots. The compiler can move level and gold together.
        pattern = (
            re.escape(b'\x48\x8d' + bytes([0x80 | base])) + rb'.{4}'
            + re.escape(b'\x48\x8d' + bytes([0x88 | base])) + rb'.{4}'
            + re.escape(b'\x48\x8d' + bytes([0x90 | base])) + rb'.{4}'
            + re.escape(b'\x4c\x8d' + bytes([0x80 | base])) + rb'.{4}'
        )
        for match in re.finditer(pattern, code, re.DOTALL):
            if len(candidates) > 16:
                return ()
            start = text_rva + match.start()
            raw = code[match.start():match.start() + 0x150]
            hero_register = {3:'rbx', 5:'rbp', 6:'rsi', 7:'rdi'}[base]
            # RBP cannot simultaneously be the frame and hero in this ABI.
            if hero_register == 'rbp':
                continue
            registers = {hero_register:('field', 0), 'rbp':('stack', 'rbp', 0), 'rsp':('stack', 'rsp', 0)}
            stack = {}
            def address(ins, op, registers=registers):
                mem = op.mem
                if mem.index:
                    return None
                if ins.reg_name(mem.base) == 'rip':
                    return ('code', ins.address + ins.size + mem.disp)
                tag = registers.get(register_family(ins, mem.base))
                if tag and tag[0] == 'field':
                    return ('field', tag[1] + mem.disp)
                if tag and tag[0] == 'stack':
                    return ('stack', tag[1], tag[2] + mem.disp)
                return None
            for ins in decoder.disasm(raw, start):
                ops = ins.operands
                if ins.mnemonic == 'call':
                    description = registers.get('rdx')
                    args = registers.get('r8')
                    if not description or description[0] != 'code' or not args or args[0] != 'stack':
                        break
                    metadata = read(description[1], 192)
                    position = len(RESOURCE_PREFIX)
                    if not metadata.startswith(RESOURCE_PREFIX + RESOURCE_LABELS):
                        break
                    fields = {}
                    addresses = {}
                    guards = {start:raw[:ins.address + ins.size - start],
                              description[1]:metadata[:position + len(RESOURCE_LABELS)]}
                    expected = {'level':(3,'I'), 'run_gold':(5,'I'), 'health':(7,'Q'),
                                'max_health':(8,'Q'), 'rerolls':(11,'I')}
                    for key,(index,fmt) in expected.items():
                        slot = (args[1], args[2] + index * 16)
                        field, callback = stack.get(slot), stack.get((slot[0],slot[1]+8))
                        if not field or field[0] != 'field':
                            continue
                        actual = field[1]
                        if not 0 <= actual <= 0x10000 - struct.calcsize(fmt) or actual % struct.calcsize(fmt):
                            continue
                        addresses[key] = actual
                        proof = scalar_formatter(callback, struct.calcsize(fmt))
                        if proof:
                            fields[key] = (actual,fmt)
                            guards[proof[0]] = proof[1]
                    # Treat health as a pair. A jump in only one argument is
                    # insufficient evidence that the pair's layout moved.
                    hp, maximum = addresses.get('health'), addresses.get('max_health')
                    if hp is None or maximum != hp + 8 or hp <= 0xF0:
                        fields.pop('health', None)
                        fields.pop('max_health', None)
                    # This independent routine must use exactly the same array
                    # and establish the resource address from that hero.
                    if 'rerolls' not in addresses:
                        break
                    rr = addresses['rerolls']
                    if stats is not None and rr < stats + 64:
                        break
                    if rr not in anchors:
                        if len(anchors) == 16:
                            return ()
                        anchors[rr] = resource_serializers(code, text_rva, array, rr)
                    for anchor in anchors[rr]:
                        bounded = {key: (offset, fmt) for key, (offset, fmt) in fields.items()
                                   if offset + struct.calcsize(fmt) <= anchor.stride}
                        evidence = {**guards, anchor.rva: anchor.raw}
                        proofs = tuple(FieldProof(key, offset, fmt, maximum_for(key), tuple(evidence.items()))
                                       for key, (offset, fmt) in bounded.items())
                        candidates.append((anchor, proofs))
                        if len(candidates) > 16:
                            return ()
                    break
                if ins.mnemonic.startswith('j') or ins.mnemonic == 'ret':
                    break
                value = None
                if ins.mnemonic == 'lea' and len(ops) == 2 and ops[1].type == X86_OP_MEM:
                    value = address(ins,ops[1])
                elif ins.mnemonic == 'mov' and len(ops) == 2 and ops[1].type == X86_OP_REG:
                    value = registers.get(register_family(ins,ops[1].reg))
                elif (ins.mnemonic == 'add' and len(ops) == 2 and ops[0].type == X86_OP_REG
                      and ops[1].type == X86_OP_IMM):
                    old = registers.get(register_family(ins,ops[0].reg))
                    if old and old[0] == 'field':
                        value = ('field',old[1]+ops[1].imm)
                destination = address(ins,ops[0]) if ops and ops[0].type == X86_OP_MEM else None
                for reg in ins.regs_access()[1]:
                    registers.pop(register_family(ins,reg),None)
                if destination and destination[0] == 'stack':
                    slot = destination[1:]
                    stack.pop(slot,None)
                    if ins.mnemonic == 'mov' and ops[0].size == 8 and value is not None:
                        stack[slot] = value
                if ops and ops[0].type == X86_OP_REG and value is not None:
                    registers[register_family(ins,ops[0].reg)] = value

    return candidates


def maximum_for(key):
    if key == 'health':
        return 1000000
    if key == 'level':
        return 10000
    return STATS[key].maximum


def discover_fields(blob, sections, code, text_rva, array, stats, *, resources=None):
    if resources is None:
        candidates = resource_candidates(blob, sections, code, text_rva, array, stats)
        resources = candidates[0][1] if len(candidates) == 1 else ()
    result = list(resources)
    if stats is None:
        return tuple(result)
    stride = array[3]
    try:
        fields,guards = discover_stat_fields(blob,sections,code,text_rva)
        for key,relative in fields.items():
            stat = STATS[key]
            offset = stats + relative
            if 0 <= offset <= stride - struct.calcsize(stat.fmt):
                result.append(FieldProof(key,offset,stat.fmt,stat.maximum,tuple(guards.items())))
    except (ValueError,StopIteration,struct.error,IndexError):
        pass
    if not any(p.key in ('damage','crit','crit_damage','cooldown','area','targets',
                         'armor','regen','speed','pickup','gold_bonus','luck') for p in result):
        from compact_stats import discover_compact_stats
        fields,guards = discover_compact_stats(blob,sections,code,text_rva,array,stats)
        for key,relative in fields.items():
            if key=='echo':
                _,control,mask,count,kind=relative
                result.append(FieldProof('echo',control,'f',1,tuple(guards.items()),(mask,count,kind)))
                continue
            stat=STATS[key]
            result.append(FieldProof(key,stats+relative,stat.fmt,stat.maximum,tuple(guards.items())))
    # Healing requires a verified maximum; maximum health can stand alone.
    keys = {p.key for p in result}
    return tuple(p for p in result if p.key != 'health' or 'max_health' in keys)
