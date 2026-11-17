"""Corroborate a selected player's owned string across independent routines."""
from code_search import (name_serializers, _matches, sequence, HEADER, _decode,
                         _header, _hero, _decoder)
from capstone.x86 import X86_OP_MEM, X86_OP_REG, X86_OP_IMM
import struct


def discover_identity(code, text_rva, array):
    legacy = name_serializers(code, text_rva, array)
    if len(legacy) == 1:
        item = legacy[0]
        return item.value, ((item.rva, item.raw),)
    checks, drops = {}, {}
    for match in _matches(sequence(*HEADER), code, b'\x8b', require_w=True, offset=array[0]):
        rva = text_rva + match.start()
        header = _header(_decode(match.group(), rva))
        if not header or header[0] != array:
            continue
        registers = header[1]
        suffix = list(_decoder().disasm(code[match.end():match.end()+80], text_rva+match.end()))
        if not suffix:
            continue
        first = suffix[0]; ops = first.operands
        # Direct sentinel check of the same selected player's Option<String>.
        if (first.mnemonic == 'cmp' and len(ops)==2 and ops[0].type==X86_OP_MEM
                and ops[0].size==8 and ops[1].type==X86_OP_IMM and ops[1].imm==-1):
            offset = _hero(ops[0].mem, registers)
            if offset is not None:
                checks.setdefault(offset, []).append((rva, code[match.start():first.address+first.size-text_rva]))
        # New compiler drops the old string by loading capacity first, checking
        # both None (-1) and empty (0), then freeing the matching data pointer.
        if len(suffix)<7 or first.mnemonic!='mov' or len(ops)!=2 or ops[0].type!=X86_OP_REG or ops[1].type!=X86_OP_MEM or ops[1].size!=8:
            continue
        offset = _hero(ops[1].mem, registers); reg=ops[0].reg
        cmp,branch,test,empty,pointer,call = suffix[1:7]
        co,to,po=cmp.operands,test.operands,pointer.operands
        if (offset is not None and cmp.mnemonic=='cmp' and len(co)==2
                and co[0].type==X86_OP_REG and co[0].reg==reg and co[1].type==X86_OP_IMM and co[1].imm==-1
                and branch.mnemonic=='je' and test.mnemonic=='test' and len(to)==2
                and all(o.type==X86_OP_REG and o.reg==reg for o in to)
                and empty.mnemonic=='je' and branch.operands[0].imm==empty.operands[0].imm
                and pointer.mnemonic=='mov' and len(po)==2 and po[0].type==X86_OP_REG
                and pointer.reg_name(po[0].reg)=='rcx' and po[1].type==X86_OP_MEM and po[1].size==8
                and _hero(po[1].mem, registers)==offset+8 and call.mnemonic=='call'
                and branch.operands[0].imm==call.address+call.size):
            drops.setdefault(offset, []).append((rva, code[match.start():call.address+call.size-text_rva]))
    candidates=[off for off in checks.keys() & drops.keys() if off%8==0 and 0x160<=off<=array[3]-24]
    if len(candidates)!=1:
        raise ValueError('Hunt cannot corroborate a unique player identity string.')
    offset=candidates[0]
    return offset, tuple(checks[offset]+drops[offset])


def read_identity(memory, hero, offset):
    raw=memory.read(hero+offset,24)
    capacity,pointer,length=struct.unpack('<3Q',raw)
    if not (1<=length<=capacity<=128 and 0x10000<=pointer<0x7FFFFFFFF000):
        raise ValueError('Player identity string is unavailable.')
    name=memory.read(pointer,length).decode('utf-8')
    if not name.isprintable() or not name.strip():
        raise ValueError('Player identity string is invalid.')
    if memory.read(hero+offset,24)!=raw:
        raise ValueError('Player identity changed during verification.')
    return name
