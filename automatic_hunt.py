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
from hunt_fields import discover_fields, resource_candidates
from hero_identity import discover_identity, read_identity


@dataclass(frozen=True)
class SearchPlan:
    digest: str
    vector: int
    index: int
    stride: int
    stats: int | None
    rva: int
    guard: bytes
    fields: tuple = ()
    name: int | None = None
    identity_guards: tuple = ()
    level_offset: int | None = 0x150
    health_offset: int = 0xF8
    method: str = 'Stat serializer'


class PlayerArrayUnavailable(ValueError):
    """The image did not provide sufficient evidence for live player discovery."""


def file_identity(path):
    state = Path(path).stat()
    return state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino


def corroborated_array(blob, sections, code, text_rva):
    candidates = []
    for resource, fields in resource_candidates(blob, sections, code, text_rva):
        health = next((field for field in fields if field.key == 'health'), None)
        maximum = next((field for field in fields if field.key == 'max_health'), None)
        if not health or not maximum:
            continue
        try:
            name, identity_guards = discover_identity(code, text_rva, resource.array)
        except ValueError:
            continue
        # Identity must come from a separate routine and a separate field.
        if name % 8 or not 0x160 <= name <= resource.stride - 24:
            continue
        if any(start < resource.rva + len(resource.raw) and resource.rva < start + len(raw)
               for start, raw in identity_guards):
            continue
        if any(name < field.offset + struct.calcsize(field.fmt) and field.offset < name + 24
               for field in fields):
            continue
        guards = tuple(dict((*identity_guards, *health.guards)).items())
        candidates.append((resource, name, guards, fields))
    if len(candidates) != 1:
        raise PlayerArrayUnavailable(f'Expected a unique player array, found {len(candidates)} corroborated candidates.')
    return candidates[0]


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
    if before != file_identity(path):
        raise RuntimeError('Game image changed during Hunt discovery.')
    resources = None
    method = 'Stat serializer'
    match = matches[0] if len(matches) == 1 and matches[0].value % 4 == 0 else None
    if match:
        try:
            name, identity_guards = discover_identity(code, text[2], match.array)
        except ValueError:
            from compatibility import resolve, Layout
            try:
                layout = resolve(path)
            except RuntimeError as exc:
                if not str(exc).startswith('Unsupported Reborn build'):
                    raise
                match = None
            else:
                if isinstance(layout, Layout) and (
                    layout.index, layout.vector + 16, layout.vector + 8, layout.stride
                ) == match.array:
                    name, identity_guards = layout.name, tuple(layout.signatures.items())
                else:
                    match = None
    if match:
        stats = match.value
    else:
        match, name, identity_guards, resources = corroborated_array(blob, sections, code, text[2])
        method = 'Identity and resources'
        blocks = [item for item in matches if item.array == match.array and item.value % 4 == 0
                  and item.value + 64 <= match.value
                  and all(field.offset + struct.calcsize(field.fmt) <= item.value
                          or item.value + 64 <= field.offset for field in resources)]
        stats = blocks[0].value if len(blocks) == 1 else None
        if stats is not None:
            identity_guards = tuple(dict((*identity_guards, (blocks[0].rva, blocks[0].raw))).items())
    index, _, pointer, stride = match.array
    fields = discover_fields(blob, sections, code, text[2], match.array, stats, resources=resources)
    if before != file_identity(path):
        raise RuntimeError('Game image changed during field discovery.')
    level = next((field for field in fields if field.key == 'level'), None)
    health = next((field for field in fields if field.key == 'health'), None)
    for field in (level, health):
        if field:
            identity_guards = tuple(dict((*identity_guards, *field.guards)).items())
    return SearchPlan(hashlib.sha256(blob).hexdigest(), pointer - 8, index, stride,
                      stats, match.rva, match.raw, fields, name, identity_guards, level.offset if level else None,
                      health.offset if health else 0xF8, method), before


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
                error_type, message, retry_at = self.plan_error
                if retry_at is None or time.monotonic() < retry_at:
                    raise error_type(message)
                # Transient failures (image lock, update in progress, antivirus)
                # are retried instead of disabling Hunt for the whole session.
                self.plan_error = None
            self.reset()
            self.session = session
            self.stamp = stamp
            try:
                self.plan, self.stamp = plan_source(session.path)
            except PlayerArrayUnavailable as exc:
                self.plan_error = (type(exc), str(exc), None)
                raise
            except Exception as exc:
                self.plan_error = (type(exc), str(exc), time.monotonic() + 5)
                raise
            check()
            try:
                modules = [int(m.addr.split('-')[0], 16) for m in process.memory_maps(grouped=False)
                           if str(Path(m.path).resolve()).casefold() == str(Path(session.path).resolve()).casefold()]
            except Exception:
                self.reset()
                raise
            if not modules:
                self.reset()
                raise RuntimeError('Cannot locate the game module in memory. Reconnect and try again.')
            self.base = min(modules)
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
                slots = struct.unpack_from('<16I', raw, plan.stats) if plan.stats is not None else ()
                if not (guid and 1 <= maximum <= 10**9 and current <= maximum
                        and (level is None or 1 <= level <= 10000) and all(v <= 1000000 for v in slots)):
                    return
                # Discard a snapshot if ownership changes while it is being read.
                if (memory.read(game + plan.vector, 24) != header
                        or memory.read(game + plan.index, 8) != index):
                    return
                found[hero_pointer] = dict(hero=hero_pointer, game=game, raw=raw, count=count, index=selected,
                                           name=name, pointer=pointer, level=level, slots=slots)
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
            heroes.append(dict(hero=pointer, game=item['game'], identity=identity,
                               level=item['level'],
                               count=item['count'], index=item['index'], name=item['name'], pointer=item['pointer'],
                               pairs=pairs, comparisons=comparisons, slots=item['slots']))
        self.previous = {h['hero']: h for h in heroes}
        self.leases = [item['game'] for item in found.values()]
        return dict(build=plan.digest[:12], heroes=heroes, examined=examined, partial=partial,
                    ambiguous=len(heroes) > 1, method=plan.method)


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
