"""Apply independently verified Hunt fields without waiting for the full layout."""
import struct
import math

from player import STATS


class FieldProofChanged(RuntimeError):
    pass


class HuntFreezer:
    def __init__(self):
        self.reset()

    def reset(self):
        self.owner = None
        self.observations = 0
        self.complete = False
        self.last_level = None

    def observe(self, hunt, result):
        if result['ambiguous'] or len(result['heroes']) != 1:
            self.reset()
            return ()
        hero = result['heroes'][0]
        owner = (hunt.session,hunt.plan.digest,hero['hero'],hero['game'],hero['identity'],hero.get('count',1),hero.get('index',0),hero.get('name'))
        if owner != self.owner or (self.last_level is not None and hero['level'] is not None and hero['level']<self.last_level):
            self.owner, self.observations, self.complete = owner, 0, False
        self.last_level=hero['level']
        self.observations += 1
        self.complete = self.complete or not result['partial']
        if self.observations < 2 or not self.complete:
            return ()
        fields = []
        for proof in hunt.plan.fields:
            if proof.key!='echo' and proof.indirect:
                continue
            stat = STATS.get(proof.key)
            if proof.key == 'health':
                fmt,maximum = 'Q',1000000
            elif proof.key=='echo' and (len(proof.indirect)==3 and proof.indirect[0]==proof.offset+8
                                       and proof.indirect[1]==proof.offset+24 and 0<=proof.indirect[2]<=255
                                       and proof.offset<=hunt.plan.stride-32):
                fmt,maximum = 'f',1
            elif stat:
                fmt,maximum = stat.fmt,stat.maximum
            else:
                continue
            if (proof.fmt == fmt and proof.maximum == maximum and proof.guards
                    and 0 <= proof.offset <= hunt.plan.stride - struct.calcsize(fmt)
                    and proof.offset % struct.calcsize(fmt) == 0):
                fields.append(proof)
        # Conflicting field identities cannot be selected by list order.
        fields = [p for p in fields if sum(q.offset == p.offset or q.key == p.key for q in fields) == 1]
        keys = {p.key for p in fields}
        fields = [p for p in fields if p.key != 'health' or 'max_health' in keys]
        return tuple(sorted(fields,key=lambda p:(p.key != 'max_health',p.key == 'health',p.key)))

    def apply(self, hunt, result, memory, fields, cancelled=lambda:False, *, targets=None, keys=None, read_only=False, allow_multiplayer=False, authorize=lambda:True):
        from automatic_hunt import file_identity
        if not self.owner or len(result['heroes']) != 1 or result['ambiguous']:
            raise RuntimeError('Hunt ownership is not uniquely verified.')
        hero = result['heroes'][0]
        expected = (hunt.session,hunt.plan.digest,hero['hero'],hero['game'],hero['identity'],hero.get('count',1),hero.get('index',0),hero.get('name'))
        if expected != self.owner or not self.complete or self.observations < 2:
            raise RuntimeError('Hunt owner changed before applying fields.')
        plan = hunt.plan
        if hero.get('count',1)>1 and plan.name is None:
            raise RuntimeError('Multiplayer identity proof is unavailable.')
        if hero.get("count",1)>1 and not allow_multiplayer and not read_only:
            raise RuntimeError("Multiplayer writes require explicit Host session mode.")
        if any(proof not in plan.fields for proof in fields):
            raise RuntimeError('Field proof does not belong to the active search plan.')
        held,errors,values,confirmed = {},{},{},{}
        targets=targets or {}
        # Validate the entire request before the first write. Field limits and
        # offsets still come exclusively from the unchanged proof objects.
        for proof in fields:
            if proof.key not in targets:continue
            value=targets[proof.key]
            if (not isinstance(value,(int,float)) or not math.isfinite(value)
                    or not (1 if proof.key=='max_health' else 0)<=value<=proof.maximum
                    or (proof.fmt!='f' and int(value)!=value)):
                raise ValueError(f'Invalid target for {proof.key}.')

        def address_of(proof):
            if not proof.indirect:
                return hero['hero']+proof.offset,None
            raw=memory.read(hero['hero']+proof.offset,32)
            control,mask,_,count=struct.unpack('<4Q',raw)
            if (not 0x10000<=control<0x7FFFFFFFF000 or not 0<=mask<1024
                    or (mask+1)&mask or not 1<=count<=min(mask+1,64)):
                raise ValueError('Active-effects table is not available yet.')
            controls=memory.read(control,mask+1)
            if sum(c<0x80 for c in controls)!=count:
                raise ValueError('Active-effects table changed; retrying.')
            found=[]
            for index,occupied in enumerate(controls):
                if occupied>=0x80:continue
                payload=control-8*(index+1)
                item=memory.read(payload,8)
                if item[0]==proof.indirect[2]:
                    value=struct.unpack_from('<f',item,4)[0]
                    if not math.isfinite(value) or not 0<=value<=1:
                        raise ValueError('Unexpected effect value; waiting for verification.')
                    found.append(payload+4)
            if len(found)!=1:
                raise ValueError('Echo effect is not active yet; hunting its entry.')
            return found[0],raw

        def guard(proof):
            if cancelled():
                raise InterruptedError('Hunt freeze stopped.')
            if not read_only and not authorize():
                raise RuntimeError('Session authority changed; stat writes stopped.')
            hunt.session.process()
            if file_identity(hunt.session.path) != hunt.stamp:
                raise RuntimeError('Game image changed; Hunt writes stopped.')
            if memory.read(hunt.base+plan.rva,len(plan.guard)) != plan.guard:
                raise RuntimeError('Player-array proof changed; Hunt writes stopped.')
            for rva,raw in (*proof.guards,*plan.identity_guards):
                if memory.read(hunt.base+rva,len(raw)) != raw:
                    raise FieldProofChanged('Field proof changed; waiting for rediscovery.')
            cap,pointer,count = struct.unpack('<3Q',memory.read(hero['game']+plan.vector,24))
            index = struct.unpack('<Q',memory.read(hero['game']+plan.index,8))[0]
            identity = struct.unpack('<Q',memory.read(hero['hero']+0xF0,8))[0]
            level_valid = True
            if plan.level_offset is not None:
                level = struct.unpack('<I', memory.read(hero['hero'] + plan.level_offset, 4))[0]
                level_valid = hero['level'] <= level <= 10000
            current,maximum = struct.unpack('<2Q',memory.read(hero['hero']+plan.health_offset,16))
            if (pointer != hero.get('pointer',hero['hero']) or count != hero.get('count',1)
                    or index != hero.get('index',0) or not 1 <= count <= min(cap,4) or not 1 <= cap <= 64
                    or pointer+index*plan.stride != hero['hero']
                    or (count>1 and not allow_multiplayer and not read_only)
                    or identity != hero['identity'] or not level_valid
                    or not 1 <= maximum <= 10**9 or current > maximum):
                raise RuntimeError('Player ownership changed; Hunt writes stopped.')
            if plan.name is not None:
                from hero_identity import read_identity
                if read_identity(memory,hero['hero'],plan.name)!=hero.get('name'):
                    raise RuntimeError('Selected player identity changed; Hunt writes stopped.')

        for proof in fields:
            # Identity/code failures abort the batch; a value race only skips
            # that field so other independently proven stats can proceed.
            try:
                guard(proof)
            except FieldProofChanged as exc:
                errors[proof.key] = str(exc)
                continue
            try:
                address,table_before = address_of(proof)
                before = memory.read(address, struct.calcsize(proof.fmt))
            except (ValueError,OSError) as exc:
                errors[proof.key]=str(exc)
                continue
            values[proof.key]=struct.unpack('<'+proof.fmt,before)[0]
            if read_only or (keys is not None and proof.key not in keys):
                continue
            target = targets.get(proof.key,proof.maximum)
            maximum_before = None
            if proof.key == 'max_health':
                # Maximize never lowers a larger live maximum or strands HP
                # above its maximum when healing is not yet verified.
                target = max(target,struct.unpack('<Q',before)[0])
            if proof.key == 'health':
                maximum = next(p for p in fields if p.key == 'max_health')
                maximum_before = memory.read(hero['hero']+maximum.offset,8)
                target = struct.unpack('<Q',maximum_before)[0]
                if not 1 <= target <= 10**9:
                    raise RuntimeError('Unexpected maximum health; healing stopped.')
            after = struct.pack('<'+proof.fmt,target)
            if before != after:
                try:
                    guard(proof)
                except FieldProofChanged as exc:
                    errors[proof.key] = str(exc)
                    continue
                if table_before is not None:
                    try:
                        latest,table_after=address_of(proof)
                    except (ValueError,OSError) as exc:
                        errors[proof.key]=str(exc)
                        continue
                    if latest!=address or table_after!=table_before:
                        errors[proof.key]='Active-effects table moved; retrying.'
                        continue
                if maximum_before is not None and memory.read(hero['hero']+maximum.offset,8) != maximum_before:
                    errors[proof.key] = 'Maximum health changed; retrying.'
                    continue
                if memory.read(address,len(before)) != before:
                    errors[proof.key] = 'Value changed; retrying.'
                    continue
                memory.write(address,after)
                if memory.read(address,len(after)) != after:
                    errors[proof.key] = 'Game recalculated value; retrying.'
                    continue
            if before==after:confirmed[proof.key]=target
            held[proof.key] = target
            values[proof.key] = target
        return dict(held=held,errors=errors,values=values,confirmed=confirmed)
