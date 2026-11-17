"""Bounded data-flow proof from copied Stats fields to named compact arguments."""
from collections import deque
from dataclasses import dataclass
import re
import struct

from capstone import Cs, CS_ARCH_X86, CS_MODE_64, CS_AC_WRITE
from capstone.x86 import X86_OP_REG, X86_OP_MEM, X86_OP_IMM
from code_search import register_family, stat_serializers


LABELS = ('dmg','crit','critd','cdr','area','tgt','echo','hpm','red','cap','regen',
          'ls','kh','thorns','spd','run','pick','gold','xp','luck','buff','dot','lives')
KEYS = {'dmg':'damage','crit':'crit','critd':'crit_damage','cdr':'cooldown','area':'area',
        'tgt':'targets','red':'armor','regen':'regen','spd':'speed','pick':'pickup',
        'gold':'gold_bonus','luck':'luck','echo':'echo'}
DESCRIPTION = b''.join(bytes([len(('' if i == 0 else ';')+label+'=')])
                         + (('' if i == 0 else ';')+label+'=').encode()
                         + (b'\xc5 \x00\x00p\x01\x00' if label=='run' else b'\xc0')
                         for i,label in enumerate(LABELS)) + b'\x00'


@dataclass(frozen=True)
class Value:
    origins: frozenset = frozenset()
    pointer: tuple | None = None
    lanes: tuple | None = None
    scaled: frozenset = frozenset()


EMPTY = Value()


def merge(a,b):
    lanes = tuple(x|y for x,y in zip(a.lanes,b.lanes)) if a.lanes and b.lanes else None
    return Value(a.origins|b.origins,a.pointer if a.pointer==b.pointer else None,lanes,a.scaled|b.scaled)


def trace(raw, start, entry, seeds, hero_register, stats, target, summaries=None, map_sources=None):
    decoder=Cs(CS_ARCH_X86,CS_MODE_64);decoder.detail=True
    instructions={i.address:i for i in decoder.disasm(raw,start)}
    if entry not in instructions or target not in instructions:
        return None
    states={entry:({'rbp':Value(pointer=('rbp',0)), 'rsp':Value(pointer=('rsp',0)),
                   hero_register:Value(pointer=('hero',0)), **seeds},{})}
    pending=deque([entry]);iterations=0

    def address(ins,op,registers):
        mem=op.mem
        if mem.index:
            return None
        if ins.reg_name(mem.base)=='rip':
            return ('image',ins.address+ins.size+mem.disp)
        pointer=registers.get(register_family(ins,mem.base),EMPTY).pointer
        return (pointer[0],pointer[1]+mem.disp) if pointer else None

    def load(pointer,size,memory):
        if not pointer:return EMPTY
        base,offset=pointer
        if base=='hero' and size in (4,16) and stats<=offset and offset+size<=stats+64 and offset%4==0:
            lanes=tuple(frozenset([offset-stats+i]) for i in range(0,size,4))
            return Value(frozenset().union(*lanes),lanes=lanes if size==16 else None)
        if base=='hero' and size==8:
            return Value(pointer=('loaded',offset))
        exact=memory.get((base,offset,size))
        if exact is not None:return exact
        origins=frozenset();scaled=frozenset()
        for (b,p,n),value in memory.items():
            if b==base and p<offset+size and offset<p+n:
                if value.lanes and n==16 and size==4 and (offset-p)%4==0:
                    origins |= value.lanes[(offset-p)//4]
                else:origins |= value.origins
                scaled |= value.scaled
        return Value(origins,scaled=scaled)

    def store(pointer,size,value,memory):
        if not pointer or pointer[0] not in ('rbp','rsp'):return
        base,offset=pointer
        # Split vector stores into exact 32-bit cells; later scalar reloads
        # must not inherit the other three fields in the vector.
        for key in list(memory):
            b,p,n=key
            if b==base and p<offset+size and offset<p+n:
                if offset<=p and p+n<=offset+size:
                    del memory[key]
                else:
                    old=memory[key]
                    memory[key]=Value(old.origins|value.origins,scaled=old.scaled|value.scaled)
        if size==16 and value.lanes:
            for i,lane in enumerate(value.lanes):memory[(base,offset+4*i,4)]=Value(lane,scaled=value.scaled&lane)
        else:memory[(base,offset,size)]=value

    while pending:
        iterations+=1
        if iterations>50000:return None
        pc=pending.popleft()
        if pc==target:continue
        ins=instructions[pc];registers,memory=states[pc]
        registers,memory=dict(registers),dict(memory)
        ops=ins.operands;mn=ins.mnemonic
        def read(op):
            if op.type==X86_OP_REG:return registers.get(register_family(ins,op.reg),EMPTY)
            if op.type==X86_OP_MEM:
                source=(map_sources or {}).get(pc)
                if source and op.size==4:
                    control,mask,key=source
                    first=registers.get(control,EMPTY).pointer
                    second=registers.get(mask,EMPTY).pointer
                    if first and second and first[0]==second[0]=='loaded' and second[1]==first[1]+8:
                        return Value(frozenset([('map',first[1],second[1],first[1]+24,key)]))
                return load(address(ins,op,registers),op.size,memory)
            return EMPTY
        value=EMPTY
        destination=address(ins,ops[0],registers) if ops and ops[0].type==X86_OP_MEM else None
        old=read(ops[0]) if ops else EMPTY
        if mn=='lea' and len(ops)==2:
            pointer=address(ins,ops[1],registers)
            mem=ops[1].mem
            data=merge(registers.get(register_family(ins,mem.base),EMPTY) if mem.base else EMPTY,registers.get(register_family(ins,mem.index),EMPTY) if mem.index else EMPTY)
            value=Value(data.origins,pointer,scaled=data.scaled)
        elif mn in ('mov','movzx','movsx','movsxd','movss','movsd','movaps','movups','movdqu','movdqa','movd','movq') and len(ops)==2:
            value=read(ops[1])
        elif mn in ('xor','xorps','pxor','sub','psubd') and len(ops)==2 and ops[0].type==ops[1].type==X86_OP_REG and ops[0].reg==ops[1].reg:
            value=EMPTY
        else:
            value=old
            for operand in ops[1:]:value=merge(value,read(operand))
            value=Value(value.origins,scaled=value.scaled | (value.origins if mn in ('imul','mul') else frozenset()))
            if mn in ('add','sub') and len(ops)==2 and old.pointer and ops[1].type==X86_OP_IMM:
                value=Value(value.origins,(old.pointer[0],old.pointer[1]+ops[1].imm*(1 if mn=='add' else -1)),scaled=value.scaled)
        writes=ins.regs_access()[1]
        # Calls conservatively transfer argument dependencies into return
        # values, then kill volatile aliases. They never create a field pointer.
        if mn=='call':
            inputs=('xmm0',) if summaries and ops[0].type==X86_OP_IMM and ops[0].imm in summaries else ('rcx','rdx','r8','r9','xmm0','xmm1','xmm2','xmm3')
            dependencies=frozenset().union(*(registers.get(r,EMPTY).origins for r in inputs))
            scaled=frozenset().union(*(registers.get(r,EMPTY).scaled for r in inputs))
            for reg in ('rax','rcx','rdx','r8','r9','r10','r11',*(f'xmm{i}' for i in range(6))):registers.pop(reg,None)
            registers['rax']=registers['xmm0']=Value(dependencies,scaled=scaled)
        elif mn=='mul':
            combined=merge(old,registers.get('rax',EMPTY))
            registers['rax']=registers['rdx']=Value(combined.origins,scaled=combined.origins|combined.scaled)
        else:
            for reg in writes:
                name=register_family(ins,reg)
                if name not in ('rflags','eflags'):registers.pop(name,None)
            if ops and ops[0].type==X86_OP_REG and ops[0].access & CS_AC_WRITE:
                registers[register_family(ins,ops[0].reg)]=value
            elif ops and ops[0].type==X86_OP_MEM and ops[0].access & CS_AC_WRITE:
                store(destination,ops[0].size,value,memory)
        successors=[]
        after=pc+ins.size
        if mn not in ('ret','ud2','int3'):
            if mn.startswith('j') and ops and ops[0].type==X86_OP_IMM:
                successors.append(ops[0].imm)
                if mn!='jmp':successors.append(after)
            else:successors.append(after)
        for successor in successors:
            if not entry<=successor<=target or successor not in instructions:continue
            if successor not in states:
                states[successor]=(dict(registers),dict(memory));pending.append(successor)
            else:
                old_regs,old_mem=states[successor]
                joined_regs={key:merge(old_regs.get(key,EMPTY),registers.get(key,EMPTY)) for key in old_regs.keys()|registers.keys()}
                joined_mem={key:merge(old_mem.get(key,EMPTY),memory.get(key,EMPTY)) for key in old_mem.keys()|memory.keys()}
                if (joined_regs,joined_mem)!=states[successor]:
                    states[successor]=(joined_regs,joined_mem);pending.append(successor)
    if target not in states:return None
    registers,memory=states[target]
    args=registers.get('r8',EMPTY).pointer
    if not args or args[0] not in ('rbp','rsp'):return None
    result={}
    callbacks={}
    for i,label in enumerate(LABELS):
        pointer=load((args[0],args[1]+16*i),8,memory).pointer
        callback=load((args[0],args[1]+16*i+8),8,memory).pointer
        if pointer and callback and callback[0]=='image':
            data=load(pointer,8 if label=='regen' else 4,memory)
            origins=data.origins-data.scaled if label=='regen' else data.origins
            if len(origins)==1:
                result[label]=next(iter(origins));callbacks[label]=callback[1]
    return result,callbacks


def unary_summary(read,start):
    """Prove a small closed helper has XMM0 as its only external data input."""
    raw=read(start,128)
    decoder=Cs(CS_ARCH_X86,CS_MODE_64);decoder.detail=True
    instructions={i.address:i for i in decoder.disasm(raw,start)}
    pending=deque([(start,frozenset(('xmm0','rsp','rip','rflags','eflags')))])
    seen={};visited=set();returns=False
    while pending:
        pc,defined=pending.popleft()
        if pc not in instructions:return None
        if pc in seen and defined>=seen[pc]:continue
        seen[pc]=defined if pc not in seen else defined&seen[pc]
        defined=seen[pc];ins=instructions[pc];visited.add(pc)
        if len(visited)>64 or ins.mnemonic in ('call','int3','ud2'):return None
        if any(op.type==X86_OP_MEM for op in ins.operands):return None
        reads,writes=ins.regs_access()
        if any(register_family(ins,r) not in defined for r in reads):return None
        defined=defined|{register_family(ins,r) for r in writes}
        if ins.mnemonic=='ret':returns=True;continue
        after=pc+ins.size
        if ins.mnemonic.startswith('j'):
            if not ins.operands or ins.operands[0].type!=X86_OP_IMM:return None
            pending.append((ins.operands[0].imm,defined))
            if ins.mnemonic!='jmp':pending.append((after,defined))
        else:pending.append((after,defined))
    if not returns:return None
    return raw[:max(pc+instructions[pc].size for pc in visited)-start]


def map_readers(instructions,read):
    """Verify Rust table bucket/key addressing before accepting a float source."""
    result={};guards={}
    for n,ins in enumerate(instructions[:-1]):
        ops=ins.operands
        if ins.mnemonic!='movss' or len(ops)!=2 or ops[1].type!=X86_OP_MEM:continue
        mem=ops[1].mem
        if not mem.base or not mem.index or mem.scale!=8 or mem.disp!=-4 or n<1:continue
        previous=instructions[n-1]
        if (previous.mnemonic!='neg' or previous.operands[0].type!=X86_OP_REG
                or previous.operands[0].reg!=mem.index):continue
        multiply=instructions[n+1]
        m=multiply.operands
        if (multiply.mnemonic!='mulss' or len(m)!=2 or m[0].reg!=ops[0].reg
                or m[1].type!=X86_OP_MEM or multiply.reg_name(m[1].mem.base)!='rip'):continue
        constant=multiply.address+multiply.size+m[1].mem.disp
        if read(constant,4)!=struct.pack('<f',100):continue
        for j in range(max(1,n-40),n-4):
            lea,copy,subtract,compare,branch=instructions[j:j+5]
            a,b,c,d,e=(i.operands for i in (lea,copy,subtract,compare,branch))
            if not (lea.mnemonic=='lea' and len(a)==2 and a[1].type==X86_OP_MEM
                    and not a[1].mem.base and a[1].mem.index==mem.index and a[1].mem.scale==8
                    and a[1].mem.disp==0 and copy.mnemonic=='mov' and len(b)==2
                    and b[0].type==b[1].type==X86_OP_REG and b[1].reg==mem.base
                    and subtract.mnemonic=='sub' and len(c)==2 and c[0].reg==b[0].reg and c[1].reg==a[0].reg
                    and compare.mnemonic=='cmp' and len(d)==2 and d[0].type==X86_OP_MEM
                    and d[0].size==1 and d[0].mem.base==b[0].reg and not d[0].mem.index
                    and d[0].mem.disp==-8 and d[1].type==X86_OP_IMM and 0<=d[1].imm<=255
                    and branch.mnemonic=='je' and e[0].type==X86_OP_IMM and e[0].imm==previous.address):continue
            bound=instructions[j-1];bo=bound.operands
            if (bound.mnemonic=='and' and len(bo)==2 and bo[0].type==bo[1].type==X86_OP_REG
                    and bo[0].reg==mem.index):
                result[ins.address]=(ins.reg_name(mem.base),bound.reg_name(bo[1].reg),d[1].imm)
                guards[constant]=struct.pack('<f',100)
    return result,guards


def discover_compact_stats(blob,sections,code,text_rva,array,stats):
    def read(rva,size):
        for _,_,v,length,offset in sections:
            if v<=rva and rva+size<=v+length:return blob[offset+rva-v:offset+rva-v+size]
        return b''
    descriptions=set()
    for section in sections:
        if section[0].startswith(b'.text'):continue
        raw=blob[section[4]:section[4]+section[3]]
        descriptions.update(section[2]+m.start() for m in re.finditer(re.escape(DESCRIPTION),raw))
    if not descriptions:return {},{}
    decoder=Cs(CS_ARCH_X86,CS_MODE_64);decoder.detail=True
    verified=[]
    anchors=[a for a in stat_serializers(code,text_rva) if a.array==array and a.value==stats]
    if len(anchors)!=1:return {},{}
    anchor=anchors[0]
    for match in re.finditer(rb'\x48\x8d\x15....',code,re.DOTALL):
        rva=text_rva+match.start();description=rva+7+struct.unpack_from('<i',match.group(),3)[0]
        if description not in descriptions or not anchor.rva<rva<anchor.rva+0x4000:continue
        suffix=list(decoder.disasm(code[match.end():match.end()+40],rva+7))
        call=next((i for i in suffix if i.mnemonic=='call'),None)
        if not call:continue
        prefix=list(decoder.disasm(anchor.raw,anchor.rva))
        seeds={}
        for ins in prefix[7:]:
            offset=ins.operands[1].mem.disp-stats
            lanes=tuple(frozenset([offset+j]) for j in range(0,16,4))
            seeds[register_family(ins,ins.operands[0].reg)]=Value(frozenset().union(*lanes),lanes=lanes)
        hero_register=register_family(prefix[6],prefix[6].operands[0].reg)
        entry=anchor.rva+len(anchor.raw)
        start=anchor.rva
        raw=code[start-text_rva:call.address+call.size-text_rva]
        summaries={}
        decoded=list(decoder.disasm(raw,start))
        sources,map_guards=map_readers(decoded,read)
        for ins in decoded:
            if ins.mnemonic=='call' and ins.operands[0].type==X86_OP_IMM:
                address=ins.operands[0].imm
                if address not in summaries:
                    summary=unary_summary(read,address)
                    if summary:summaries[address]=summary
        result=trace(raw,start,entry,seeds,hero_register,stats,call.address,summaries,sources)
        if not result:continue
        fields,callbacks=result
        guards={start:raw,description:DESCRIPTION}
        guards.update(summaries)
        guards.update(map_guards)
        proven={}
        for label,relative in fields.items():
            if label not in KEYS:continue
            if label=='echo':
                if (not isinstance(relative,tuple) or len(relative)!=5 or relative[0]!='map'
                        or relative[1]%8 or not 0<=relative[1]<=array[3]-32):continue
            elif not isinstance(relative,int) or relative%4 or not 0<=relative<=60:continue
            callback=callbacks[label]
            function=read(callback,96)
            width=8 if label=='regen' else 4
            for ins in decoder.disasm(function,callback):
                if ins.mnemonic=='call' or ins.mnemonic.startswith('j') or ins.mnemonic=='ret':break
                ops=ins.operands
                if (ins.mnemonic=='mov' and len(ops)==2 and ops[1].type==X86_OP_MEM
                        and ins.reg_name(ops[1].mem.base)=='rcx' and not ops[1].mem.index
                        and ops[1].mem.disp==0):
                    if ops[0].size==ops[1].size==width:
                        proven[KEYS[label]]=relative
                        guards[callback]=function[:ins.address+ins.size-callback]
                    break
                if any(register_family(ins,r)=='rcx' for r in ins.regs_access()[1]):break
        proven={key:value for key,value in proven.items() if list(proven.values()).count(value)==1}
        if proven:verified.append((proven,guards))
    return verified[0] if len(verified)==1 else ({},{})
