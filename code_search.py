"""Instruction-validated array searches that tolerate register allocation and NOPs."""
from dataclasses import dataclass
import re
import struct
from threading import local

from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_REG, X86_OP_IMM, X86_OP_MEM


W = rb'[\x48-\x4f]'
NO_SIB = b'[' + bytes(v for v in range(0x80, 0xC0) if v % 8 != 4) + b']'
SIB = b'[' + bytes(range(0x84, 0xC0, 8)) + b'].'
MEM32 = b'(?:' + NO_SIB + b'|' + SIB + rb').{4}'
MOV = W + b'\x8b' + MEM32
REG_MOV = W + rb'[\x89\x8b][\xc0-\xff]'
CMP = W + rb'[\x39\x3b][\xc0-\xff]'
BRANCH = rb'(?:\x0f\x83.{4}|\x73.)'
MULTIPLY = W + rb'\x69[\xc0-\xff].{4}'
LEA = W + b'\x8d(?:[' + re.escape(bytes(range(4, 0x40, 8))) + b'].|[' + re.escape(bytes(range(0x44, 0x80, 8))) + rb'].\x00)'
VECTOR = rb'(?:[\x40-\x4f]?\x0f[\x10\x28]|[\x66\xf3][\x40-\x4f]?\x0f\x6f)' + MEM32
NOP = b'(?:' + b'|'.join(re.escape(bytes.fromhex(s)) for s in (
    '90', '6690', '0f1f00', '0f1f4000', '0f1f440000', '660f1f440000',
    '0f1f8000000000', '0f1f840000000000', '660f1f840000000000')) + b'){0,4}'
STORE = W + rb'\x89[\x85\x8d\x95\x9d\xa5\xad\xb5\xbd].{4}'
ZERO = rb'(?:\xc7\x85.{4}\x00{4}|[\x40-\x4f]?\x31[\xc0-\xff])'


def sequence(*parts):
    return NOP.join(parts)


HEADER = (MOV, MOV, CMP, BRANCH, MOV, MULTIPLY, LEA)
_DECODERS = local()


def _decoder():
    if not hasattr(_DECODERS, 'value'):
        _DECODERS.value = Cs(CS_ARCH_X86, CS_MODE_64)
        _DECODERS.value.detail = True
    return _DECODERS.value


def _matches(pattern, code, opcode, require_w=False, offset=None):
    """Use a literal opcode prefilter before matching expensive alternatives."""
    compiled = re.compile(pattern, re.DOTALL)
    end = 0
    if offset is None:
        starts = (anchor.start() for anchor in re.finditer(re.escape(opcode), code))
    else:
        starts = (anchor.start() - prefix for anchor in re.finditer(re.escape(struct.pack('<I', offset)), code)
                  for prefix in (3, 4))
    for start in starts:
        if offset is None:
            low = 0x48 if require_w else 0x40
            if start and low <= code[start - 1] <= 0x4F:
                start -= 1
            elif require_w:
                continue
        elif start < 0 or not 0x48 <= code[start] <= 0x4F:
            continue
        if start < end:
            continue
        candidate = compiled.match(code, start)
        if candidate:
            end = candidate.end()
            yield candidate


@dataclass(frozen=True)
class CodeMatch:
    index: int
    count: int
    pointer: int
    stride: int
    value: int
    rva: int
    raw: bytes

    @property
    def array(self):
        return self.index, self.count, self.pointer, self.stride


def _decode(raw, rva):
    decoder = _decoder()
    instructions = list(decoder.disasm(raw, rva))
    if sum(i.size for i in instructions) != len(raw):
        return []
    return [i for i in instructions if i.mnemonic != 'nop']


def register_family(ins, register):
    name = ins.reg_name(register)
    aliases = {'al': 'rax', 'ax': 'rax', 'eax': 'rax', 'bl': 'rbx', 'bx': 'rbx', 'ebx': 'rbx',
               'cl': 'rcx', 'cx': 'rcx', 'ecx': 'rcx', 'dl': 'rdx', 'dx': 'rdx', 'edx': 'rdx',
               'sil': 'rsi', 'esi': 'rsi', 'dil': 'rdi', 'edi': 'rdi', 'ebp': 'rbp', 'esp': 'rsp'}
    if re.fullmatch(r'r\d+[dwb]', name):
        return name[:-1]
    return aliases.get(name, name)


def config_anchors(code, text_rva):
    writes = []
    for match in _matches(rb'[\x40-\x4f]?\x0f\x95' + MEM32, code, b'\x0f\x95'):
        ins = _decode(match.group(), text_rva + match.start())
        if (len(ins) == 1 and ins[0].mnemonic == 'setne' and ins[0].operands[0].type == X86_OP_MEM
                and not ins[0].operands[0].mem.index and ins[0].operands[0].mem.disp == 0x208):
            base = ins[0].operands[0].mem.base
            decoder = _decoder()
            fields = set()
            end = 0
            rva = text_rva + match.start()
            for following in decoder.disasm(code[match.start():match.start() + 128], rva):
                if following.mnemonic in ('ret', 'call', 'jmp'):
                    break
                operands = following.operands
                if not operands or operands[0].type != X86_OP_MEM:
                    continue
                memory = operands[0].mem
                if memory.base != base or memory.index:
                    continue
                if following.mnemonic == 'setne' and memory.disp in (0x208, 0x209, 0x20A) or following.mnemonic == 'movss' and memory.disp == 0x1F8:
                    fields.add(memory.disp)
                if fields == {0x208, 0x209, 0x20A, 0x1F8}:
                    end = following.address + following.size - rva
                    break
            if end:
                writes.append((rva, code[match.start():match.start() + end]))
    comparisons = []
    pattern = sequence(rb'[\x40-\x4f]?\x0f\xb6' + MEM32, rb'[\x40-\x4f]?\x3a' + MEM32)
    for match in _matches(pattern, code, b'\x0f\xb6'):
        ins = _decode(match.group(), text_rva + match.start())
        if len(ins) != 2 or ins[0].mnemonic != 'movzx' or ins[1].mnemonic != 'cmp':
            continue
        load, compare = ins
        first, second = load.operands, compare.operands
        if (first[0].type == second[0].type == X86_OP_REG and first[1].type == second[1].type == X86_OP_MEM
                and first[1].size == second[1].size == 1 and not first[1].mem.index and not second[1].mem.index
                and first[1].mem.disp == second[1].mem.disp == 0x208
                and register_family(load, first[0].reg) == register_family(compare, second[0].reg)):
            comparisons.append((text_rva + match.start(), match.group()))
    if len(writes) != 1 or len(comparisons) != 1:
        raise ValueError(f'configuration field anchors are ambiguous (found {len(writes)} writers, {len(comparisons)} comparisons)')
    return dict(writes + comparisons)


def rip_readers(code, text_rva, kind):
    patterns = {
        'byte': rb'[\x40-\x4f]?\x0f\xb6[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}',
        'integer': rb'[\x40-\x4f]?\x8b[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}',
        'float': rb'\xf3[\x40-\x4f]?\x0f\x10[\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}',
    }
    matches = (_matches(patterns[kind], code, b'\x0f\xb6') if kind == 'byte'
               else re.finditer(patterns[kind], code, re.DOTALL))
    for match in matches:
        address = text_rva + match.start()
        ins = _decode(match.group(), address)
        if len(ins) != 1 or len(ins[0].operands) != 2:
            continue
        source = ins[0].operands[1]
        expected = 1 if kind == 'byte' else 4
        if source.type == X86_OP_MEM and source.size == expected and ins[0].reg_name(source.mem.base) == 'rip':
            yield address, match.group(), address + len(match.group()) + source.mem.disp


def _load(ins):
    ops = ins.operands
    if (ins.mnemonic != 'mov' or len(ops) != 2 or ops[0].type != X86_OP_REG
            or ops[1].type != X86_OP_MEM or ops[1].size != 8 or ops[1].mem.index):
        return None
    return ops[0].reg, ops[1].mem.base, ops[1].mem.disp


def _hero(memory, registers):
    if memory.scale != 1:
        return None
    first, second = registers.get(memory.base), registers.get(memory.index)
    if first == 'hero' and not memory.index:
        return memory.disp
    if {first, second} == {'pointer', 'scaled'}:
        return memory.disp
    return None


def _header(instructions):
    if len(instructions) < 7:
        return None
    a, b, compare, branch, p, multiply, lea = instructions[:7]
    index, count, pointer = _load(a), _load(b), _load(p)
    if not all((index, count, pointer)):
        return None
    ir, _, index_offset = index
    cr, game, count_offset = count
    pr, other_game, pointer_offset = pointer
    if ir in (cr, pr) or game != other_game or game in (ir, cr):
        return None
    if count_offset != pointer_offset + 8 or not 0x100 <= pointer_offset < index_offset < 0x10000:
        return None
    if (compare.mnemonic != 'cmp' or len(compare.operands) != 2
            or any(o.type != X86_OP_REG for o in compare.operands)
            or tuple(o.reg for o in compare.operands) != (ir, cr) or branch.mnemonic != 'jae'):
        return None
    ops = multiply.operands
    if (multiply.mnemonic != 'imul' or len(ops) != 3 or ops[0].type != X86_OP_REG
            or ops[1].type != X86_OP_REG or ops[1].reg != ir or ops[2].type != X86_OP_IMM):
        return None
    stride = ops[2].imm
    scaled = ops[0].reg
    if scaled == pr or not 0x200 <= stride <= 0x10000 or stride % 8:
        return None
    registers = {pr: 'pointer', scaled: 'scaled'}
    if (lea.mnemonic != 'lea' or lea.operands[0].type != X86_OP_REG
            or lea.operands[1].type != X86_OP_MEM or _hero(lea.operands[1].mem, registers) != 0):
        return None
    registers[lea.operands[0].reg] = 'hero'
    return (index_offset, count_offset, pointer_offset, stride), registers


def stat_serializers(code, text_rva):
    results = []
    for match in _matches(sequence(*HEADER, *([VECTOR] * 4)), code, b'\x8b', require_w=True):
        rva = text_rva + match.start()
        ins = _decode(match.group(), rva)
        header = _header(ins)
        if not header or len(ins) != 11:
            continue
        array, registers = header
        offsets = []
        for load in ins[7:]:
            ops = load.operands
            if (load.mnemonic not in ('movups', 'movaps', 'movdqu', 'movdqa') or len(ops) != 2
                    or ops[0].type != X86_OP_REG or ops[1].type != X86_OP_MEM or ops[1].size != 16):
                break
            offset = _hero(ops[1].mem, registers)
            if offset is None:
                break
            offsets.append(offset)
        if len(offsets) != 4:
            continue
        first = min(offsets)
        if sorted(offsets) != [first + i * 16 for i in range(4)] or not 0x160 <= first <= array[3] - 64:
            continue
        if ins[3].operands[0].imm < rva + len(match.group()):
            continue
        results.append(CodeMatch(*array, first, rva, match.group()))
    return results


def name_serializers(code, text_rva, expected):
    prefix = sequence(MOV, MOV, CMP)
    rest = sequence(BRANCH, MOV, MULTIPLY, LEA,
                    b'(?:' + LEA + b'|' + REG_MOV + b')', W + rb'\x81[\xc0-\xc7].{4}',
                    ZERO, W + b'\x83' + MEM32 + b'\xff')
    results = []
    for match in _matches(prefix + NOP + b'(?:' + STORE + NOP + b')?' + rest, code, b'\x8b', require_w=True, offset=expected[0]):
        rva = text_rva + match.start()
        ins = _decode(match.group(), rva)
        # Stack spills between CMP and its bounds branch are harmless; remove
        # only an actual memory store, never an arbitrary register instruction.
        if len(ins) == 12 and ins[3].mnemonic == 'mov' and ins[3].operands[0].type == X86_OP_MEM:
            store = ins[3]
            if (store.reg_name(store.operands[0].mem.base) not in ('rsp', 'rbp')
                    or store.operands[0].mem.index or store.operands[1].type != X86_OP_REG):
                continue
            ins = ins[:3] + ins[4:]
        header = _header(ins)
        if not header or len(ins) != 11 or header[0] != expected:
            continue
        array, registers = header
        copy, add, zero, compare = ins[7:]
        if copy.mnemonic == 'lea':
            same = copy.operands[1].type == X86_OP_MEM and _hero(copy.operands[1].mem, registers) == 0
        else:
            same = copy.mnemonic == 'mov' and copy.operands[1].type == X86_OP_REG and registers.get(copy.operands[1].reg) == 'hero'
        if not same or copy.operands[0].type != X86_OP_REG:
            continue
        target = copy.operands[0].reg
        registers[target] = 'hero'
        if (add.mnemonic != 'add' or add.operands[0].type != X86_OP_REG or add.operands[0].reg != target
                or add.operands[1].type != X86_OP_IMM):
            continue
        name = add.operands[1].imm
        # Clearing bookkeeping must not destroy the selected hero address.
        if zero.mnemonic == 'xor':
            if zero.operands[0].reg != zero.operands[1].reg:
                continue
            canonical = register_family(zero, zero.operands[0].reg)
            if any(zero.reg_name(r) == canonical for r in registers):
                continue
        elif zero.mnemonic != 'mov' or zero.operands[0].type != X86_OP_MEM:
            continue
        # The comparison is relative to the original array expression. ADD may
        # change the alias target, so discard that alias before resolving it.
        registers.pop(target, None)
        ops = compare.operands
        if (compare.mnemonic != 'cmp' or len(ops) != 2 or ops[0].type != X86_OP_MEM
                or ops[0].size != 8 or ops[1].type != X86_OP_IMM or ops[1].imm != -1
                or _hero(ops[0].mem, registers) != name):
            continue
        results.append(CodeMatch(*array, name, rva, match.group()))
    return results


def resource_serializers(code, text_rva, expected, rerolls):
    results = []
    offset = expected[0] if expected is not None else None
    for match in _matches(sequence(*HEADER), code, b'\x8b', require_w=True, offset=offset):
        rva = text_rva + match.start()
        prefix = _decode(match.group(), rva)
        header = _header(prefix)
        if not header or (expected is not None and header[0] != expected):
            continue
        array, registers = header
        heroes = {register for register, value in registers.items() if value == 'hero'}
        decoder = _decoder()
        start = match.end()
        suffix = list(decoder.disasm(code[start:start + 192], text_rva + start))
        suffix = [i for i in suffix if i.mnemonic != 'nop']
        branch_targets = []
        for number, ins in enumerate(suffix[:-1]):
            if ins.mnemonic in ('call', 'ret', 'jmp') or ins.mnemonic.startswith('loop'):
                break
            ops = ins.operands
            if ins.mnemonic.startswith('j'):
                if len(ops) != 1 or ops[0].type != X86_OP_IMM or ops[0].imm <= ins.address:
                    break
                branch_targets.append(ops[0].imm)
            if (ins.mnemonic == 'lea' and len(ops) == 2 and ops[0].type == X86_OP_REG
                    and ops[0].reg not in heroes and ops[1].type == X86_OP_MEM
                    and ops[1].mem.base in heroes and not ops[1].mem.index and ops[1].mem.disp == rerolls + 12):
                following = suffix[number + 1]
                operands = following.operands
                if (following.mnemonic == 'add' and len(operands) == 2 and operands[0].type == X86_OP_REG
                        and operands[0].reg == ops[1].mem.base and operands[1].type == X86_OP_IMM
                        and operands[1].imm == rerolls):
                    end = following.address + following.size - text_rva
                    if rerolls <= array[3] - 16 and all(target >= text_rva + end for target in branch_targets):
                        results.append(CodeMatch(*array, rerolls, rva, code[match.start():end]))
                    break
            for register in ins.regs_access()[1]:
                name = register_family(ins, register)
                heroes = {r for r in heroes if ins.reg_name(r) != name}
            if not heroes:
                break
    return results
