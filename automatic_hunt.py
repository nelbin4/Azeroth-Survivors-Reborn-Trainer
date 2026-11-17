"""Automatic, read-only discovery of live values when the full layout is unknown.

The array serializer establishes a search region, not named writable fields.
Array/numeric candidates cannot authorize writes. Separate named field proofs
may authorize the guarded incremental writer in hunt_freeze.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import struct
import time

from code_search import stat_serializers
from stack_probe import stacks
from hunt_fields import discover_fields
from hero_identity import discover_identity, read_identity


@dataclass(frozen=True)
class SearchPlan:
    digest: str
    vector: int
    index: int
    stride: int
    stats: int
    rva: int
    guard: bytes
    fields: tuple = ()
    name: int | None = None
    identity_guards: tuple = ()
    level_offset: int | None = 0x150
    health_offset: int = 0xF8


def file_identity(path):
    state = Path(path).stat()
    return state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino


def make_plan(path):
    before = file_identity(path)
    blob = Path(path).read_bytes()
    pe = struct.unpack_from('<I', blob, 60)[0]
    if blob[:2] != b'MZ' or blob[pe:pe + 4] != b'PE\0\0' or struct.unpack_from('<H', blob, pe + 4)[0] != 0x8664:
        raise ValueError('Hunt requires a Windows x64 game image.')
    optional = struct.unpack_from('<H', blob, pe + 20)[0]
    count = struct.unpack_from('<H', blob, pe + 6)[0]
    if not 1 <= count <= 96:
        raise ValueError('Invalid section table.')
    sections = [struct.unpack_from('<8sIIII', blob, pe + 24 + optional + i * 40) for i in range(count)]
    text = next(s for s in sections if s[0].rstrip(b'\0') == b'.text')
    code = blob[text[4]:text[4] + text[3]]
    if len(code) != text[3]:
        raise ValueError('Truncated game image.')
    matches = stat_serializers(code, text[2])
    if len(matches) != 1:
        raise ValueError(f'Hunt cannot establish a unique player-array search region ({len(matches)} matches).')
    match = matches[0]
    index, count, pointer, stride = match.array
    if not (pointer >= 8 and count == pointer + 8 and pointer < index < 0x10000
            and 0x200 <= stride <= 0x10000 and stride % 8 == 0
            and 0 <= match.value <= stride - 64 and match.value % 4 == 0):
        raise ValueError('Hunt array bounds are inconsistent.')
    if before != file_identity(path):
        raise RuntimeError('Game image changed during Hunt discovery.')
    try:
        name, identity_guards = discover_identity(code,text[2],match.array)
    except ValueError:
        # Older verified families predate the string-copy metadata. Reuse only
        # their full compatibility contract, never an inferred string offset.
        from compatibility import resolve, Layout
        layout=resolve(path)
        if not isinstance(layout,Layout):raise ValueError('Player identity proof unavailable.')
        name,identity_guards=layout.name,tuple(layout.signatures.items())
    fields = discover_fields(blob,sections,code,text[2],match.array,match.value)
    if before != file_identity(path):
        raise RuntimeError('Game image changed during field discovery.')
    level = next((field for field in fields if field.key == 'level'), None)
    health = next((field for field in fields if field.key == 'health'), None)
    for field in (level, health):
        if field:
            identity_guards = tuple(dict((*identity_guards, *field.guards)).items())
    return SearchPlan(hashlib.sha256(blob).hexdigest(), pointer - 8, index, stride,
                      match.value, match.rva, match.raw, fields, name, identity_guards, level.offset if level else None,
                      health.offset if health else 0xF8), before


def health_candidates(raw):
    """Plausible integer pairs only; these are deliberately not trusted fields."""
    found = {}
    for offset in range(8, min(len(raw) - 15, 0x200), 8):
        identity, current, maximum = struct.unpack_from('<3Q', raw, offset - 8)
        if identity and 10 <= maximum <= 10**9 and 0 <= current <= maximum:
            found[offset] = (current, maximum)
    return found


class AutomaticHunt:
    def __init__(self, budget=8 * 1024 * 1024, seconds=0.3):
        self.budget, self.seconds = budget, seconds
        self.reset()

    def reset(self):
        self.session = None
        self.plan = None
        self.plan_error = None
        self.stamp = None
        self.cursor = None
        self.previous = {}
        self.base = None
        self.leases = []
        self.next_discovery = 0

    def step(self, session, memory, cancelled=lambda: False, stack_source=stacks, plan_source=make_plan, discover=True):
        def check():
            if cancelled():
                raise InterruptedError('Automatic Hunt stopped.')
            return session.process()

        process = check()
        if session != self.session:
            self.reset()
            self.session = session
        stamp = file_identity(session.path)
        if self.plan is None or self.stamp != stamp or self.base is None:
            if self.stamp == stamp and self.plan_error:
                raise RuntimeError(self.plan_error)
            self.reset()
            self.session = session
            self.stamp = stamp
            try:
                self.plan, self.stamp = plan_source(session.path)
            except Exception as exc:
                self.plan_error = str(exc)
                raise
            check()
            try:
                self.base = min(int(m.addr.split('-')[0], 16) for m in process.memory_maps(grouped=False)
                                if str(Path(m.path).resolve()).casefold() == str(Path(session.path).resolve()).casefold())
            except Exception:
                self.reset()
                raise
        plan = self.plan
        if memory.read(self.base + plan.rva, len(plan.guard)) != plan.guard:
            self.reset()
            raise RuntimeError('Loaded array instructions changed; Hunt will rediscover.')
        for rva, guard in plan.identity_guards:
            if memory.read(self.base+rva,len(guard))!=guard:
                self.reset()
                raise RuntimeError('Player identity proof changed; rediscovering.')
        started = time.monotonic()
        examined = 0
        found = {}
        probes = 0

        def probe(game, header):
            nonlocal probes
            if cancelled():
                raise InterruptedError('Automatic Hunt stopped.')
            probes += 1
            cap, pointer, count = struct.unpack('<3Q', header)
            if not (1 <= count <= min(cap,4) and 1 <= cap <= 64 and pointer % 8 == 0
                    and 0x10000 <= pointer < 0x7FFFFFFFF000):
                return
            try:
                index = memory.read(game + plan.index, 8)
                selected = struct.unpack('<Q',index)[0]
                if selected >= count or (count>1 and plan.name is None):
                    return
                hero_pointer = pointer+selected*plan.stride
                name = None
                # Every participant must have a valid identity; generic vectors
                # and stale resource buffers are not player ownership evidence.
                if plan.name is not None:
                    names=[read_identity(memory,pointer+i*plan.stride,plan.name) for i in range(count)]
                    name = names[selected]
                raw = memory.read(hero_pointer, plan.stride)
                # These header/value bounds narrow the search; they are not a
                # semantic layout proof and cannot enable writes.
                guid = struct.unpack_from('<Q', raw, 0xF0)[0]
                current, maximum = struct.unpack_from('<2Q', raw, plan.health_offset)
                level = struct.unpack_from('<I', raw, plan.level_offset)[0] if plan.level_offset is not None else None
                slots = struct.unpack_from('<16I', raw, plan.stats)
                if not (guid and 1 <= maximum <= 10**9 and current <= maximum
                        and (level is None or 1 <= level <= 10000) and all(v <= 1000000 for v in slots)):
                    return
                # Discard a snapshot if ownership changes while it is being read.
                if (memory.read(game + plan.vector, 24) != header
                        or memory.read(game + plan.index, 8) != index):
                    return
                found[hero_pointer] = dict(hero=hero_pointer, game=game, raw=raw, count=count, index=selected, name=name, pointer=pointer, level=level)
            except (OSError,ValueError,UnicodeError):
                return

        # Revalidate previously located roots first, instead of rescanning all stacks.
        for game in self.leases[:8]:
            try:
                probe(game, memory.read(game + plan.vector, 24))
            except OSError:
                pass
        partial = False
        if discover and (not found or self.cursor is not None or started >= self.next_discovery):
            regions = list(stack_source(process, memory))
            resume = self.cursor
            if resume and not any(tid == resume[0] and bottom <= resume[1] < top for tid, bottom, top in regions):
                resume = None
            reached = resume is None
            for tid, bottom, top in regions:
                check()
                if not reached:
                    if tid != resume[0]:
                        continue
                    reached = True
                position = resume[1] if resume and tid == resume[0] else bottom
                while position < top:
                    check()
                    if examined >= self.budget or time.monotonic() - started >= self.seconds or probes >= 128:
                        self.cursor = (tid, position)
                        partial = True
                        break
                    amount = min(256 * 1024, top - position, self.budget - examined)
                    try:
                        # 16-byte overlap includes headers straddling chunk boundaries.
                        raw = memory.read(position, min(amount + 16, top - position))
                    except OSError:
                        raw = b''
                    examined += amount
                    for match in re.finditer(rb'(?=([\x01-\x40]\x00{7}.{8}[\x01-\x04]\x00{7}))', raw, re.DOTALL):
                        address = position + match.start()
                        game = address - plan.vector
                        if address % 8 or game < bottom or game + plan.index + 8 > top:
                            continue
                        probe(game, match.group(1))
                        if probes >= 128 or len(found) >= 8:
                            # Resume after this candidate, not after the entire
                            # chunk: dense stacks can contain hundreds of Rust
                            # vectors before the actual player array.
                            self.cursor = (tid, address + 8)
                            partial = True
                            break
                    if partial:
                        break
                    position += amount
                if partial:
                    break
            if not partial:
                self.cursor = None
                self.next_discovery = time.monotonic() + 15
        check()
        if file_identity(session.path) != self.stamp:
            self.reset()
            raise RuntimeError('Game image changed; discarding Hunt results.')
        heroes = []
        for pointer, item in found.items():
            raw = item['raw']
            pairs = health_candidates(raw)
            old = self.previous.get(pointer)
            identity = struct.unpack_from('<Q', raw, 0xF0)[0]
            if old and old['identity'] != identity:
                old = None
            # Compare snapshots, but never promote temporal agreement into write permission.
            comparisons = {}
            for offset, pair in pairs.items():
                if old and offset in old['pairs'] and old['pairs'][offset][1] == pair[1]:
                    comparisons[offset] = 'changed' if pair[0] != old['pairs'][offset][0] else 'stable'
            slots = struct.unpack_from('<16I', raw, plan.stats)
            heroes.append(dict(hero=pointer, game=item['game'], identity=identity,
                               level=item['level'],
                               count=item['count'], index=item['index'], name=item['name'], pointer=item['pointer'],
                               pairs=pairs, comparisons=comparisons, slots=slots))
        self.previous = {h['hero']: h for h in heroes}
        self.leases = [item['game'] for item in found.values()]
        return dict(build=plan.digest[:12], heroes=heroes, examined=examined, partial=partial,
                    ambiguous=len(heroes) > 1)


class HuntSchedule:
    """Bounded backoff for missing heroes/errors; active snapshots run every 2s."""
    def __init__(self):
        self.failures = 0

    def delay(self, result):
        if not isinstance(result, Exception) and (result['heroes'] or result['partial']):
            self.failures = 0
            if result.get('freeze', {}).get('held'):
                return .5
            return .75 if result['partial'] else 2
        self.failures += 1
        return min(15, 2 ** min(self.failures, 4))
