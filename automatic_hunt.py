"""Automatic, read-only discovery of live values when the full layout is unknown.

The array serializer establishes a search region, not named writable fields.
None of the evidence or numeric candidates in this module authorizes writes.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import struct
import time

from code_search import stat_serializers
from stack_probe import stacks


@dataclass(frozen=True)
class SearchPlan:
    digest: str
    vector: int
    index: int
    stride: int
    stats: int
    rva: int
    guard: bytes


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
    return SearchPlan(hashlib.sha256(blob).hexdigest(), pointer - 8, index, stride,
                      match.value, match.rva, match.raw), before


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

    def step(self, session, memory, cancelled=lambda: False, stack_source=stacks, plan_source=make_plan):
        def check():
            if cancelled():
                raise InterruptedError('Automatic Hunt stopped.')
            return session.process()

        process = check()
        if session != self.session:
            self.reset()
            self.session = session
        stamp = file_identity(session.path)
        if self.plan is None or self.stamp != stamp:
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
        started = time.monotonic()
        examined = 0
        found = {}
        probes = 0

        def probe(game, header):
            nonlocal probes
            check()
            probes += 1
            cap, pointer, count = struct.unpack('<3Q', header)
            if not (count == 1 and 1 <= cap <= 64 and pointer % 8 == 0
                    and 0x10000 <= pointer < 0x7FFFFFFFF000):
                return
            try:
                index = memory.read(game + plan.index, 8)
                if index != bytes(8):
                    return
                raw = memory.read(pointer, plan.stride)
                # These header/value bounds narrow the search; they are not a
                # semantic layout proof and cannot enable writes.
                guid, current, maximum = struct.unpack_from('<3Q', raw, 0xF0)
                level = struct.unpack_from('<I', raw, 0x150)[0]
                slots = struct.unpack_from('<16I', raw, plan.stats)
                if not (guid and 1 <= maximum <= 10**9 and current <= maximum
                        and 1 <= level <= 10000 and all(v <= 1000000 for v in slots)):
                    return
                # Discard a snapshot if ownership changes while it is being read.
                if (memory.read(game + plan.vector, 24) != header
                        or memory.read(game + plan.index, 8) != index):
                    return
                found[pointer] = dict(hero=pointer, game=game, raw=raw)
            except OSError:
                return

        # Revalidate previously located roots first, instead of rescanning all stacks.
        for game in self.leases[:8]:
            try:
                probe(game, memory.read(game + plan.vector, 24))
            except OSError:
                pass
        partial = False
        if not found or self.cursor is not None or started >= self.next_discovery:
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
                    for match in re.finditer(rb'(?=([\x01-\x40]\x00{7}.{8}\x01\x00{7}))', raw, re.DOTALL):
                        address = position + match.start()
                        game = address - plan.vector
                        if address % 8 or game < bottom or game + plan.index + 8 > top:
                            continue
                        probe(game, match.group(1))
                        if probes >= 128 or len(found) >= 8:
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
            heroes.append(dict(hero=pointer, identity=identity, pairs=pairs, comparisons=comparisons, slots=slots))
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
            return 2 if result['heroes'] else 0.75
        self.failures += 1
        return min(15, 2 ** min(self.failures, 4))


def add_automatic_panel(root, parent, session_provider, needed, closing):
    import queue
    import threading
    import tkinter as tk
    from tkinter import ttk

    frame = ttk.Frame(parent)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Automatic background Hunt', font=('Segoe UI', 19, 'bold')).pack(anchor='w')
    enabled = tk.BooleanVar(value=True)
    ttk.Checkbutton(frame, text='Hunt automatically when normal detection is unavailable', variable=enabled).pack(anchor='w', pady=10)
    ttk.Label(frame, text='No values to enter. Hunt follows code-identified player arrays, searches live integer pairs and '
              'compares snapshots while you play. Candidate values remain read-only until their stat meanings are verified.',
              wraplength=1020).pack(anchor='w')
    status = tk.StringVar(value='Waiting for the selected game process…')
    ttk.Label(frame, textvariable=status, wraplength=1020, foreground='#76dfc1').pack(anchor='w', pady=12)
    output = tk.Text(frame, height=9, bg='#0c1826', fg='#e5e9ef', state='disabled')
    output.pack(fill='both', expand=True)
    results = queue.Queue()
    hunt, schedule = AutomaticHunt(), HuntSchedule()
    busy = False
    next_scan = 0
    active = None
    generation = 0
    stop = threading.Event()

    def poll():
        nonlocal busy, next_scan, active, generation
        if closing():
            stop.set()
            return
        try:
            session = session_provider()
            run = bool(enabled.get() and needed() and session)
        except Exception:
            session, run = None, False
        identity = session if run else None
        if identity != active:
            generation += 1
            active = identity
            stop.set()
            next_scan = 0
            schedule.failures = 0
            output.configure(state='normal')
            output.delete('1.0', 'end')
            output.configure(state='disabled')
        try:
            token, result = results.get_nowait()
            busy = False
            if token == generation and run:
                next_scan = time.monotonic() + schedule.delay(result)
                if isinstance(result, Exception):
                    status.set(f'Automatic Hunt will retry: {result}')
                else:
                    heroes = result['heroes']
                    state = ('Ambiguous player arrays; observing only.' if result['ambiguous'] else
                             'Live values found; comparing snapshots.' if heroes else
                             'Searching the next stack region…' if result['partial'] else
                             'No solo player array yet; waiting for a run.')
                    status.set(f"PID {session.pid} · build {result['build']} · {state}")
                    lines = []
                    for hero in heroes:
                        lines.append(f"Player-array candidate 0x{hero['hero']:016X}")
                        lines.append('16 raw stat slots (names unverified): ' + ', '.join(map(str, hero['slots'])))
                        for offset, (current, maximum) in hero['pairs'].items():
                            lines.append(f"  Health-like pair at +0x{offset:X}: {current}/{maximum} · "
                                         + hero['comparisons'].get(offset, 'first observation'))
                        lines.append('')
                    output.configure(state='normal')
                    output.delete('1.0', 'end')
                    output.insert('end', '\n'.join(lines))
                    output.configure(state='disabled')
        except queue.Empty:
            pass
        if not run:
            status.set('Automatic Hunt paused: normal detection is ready.' if not needed() else
                       'Automatic Hunt is off.' if not enabled.get() else 'Waiting for the selected game process…')
        elif not busy and time.monotonic() >= next_scan:
            busy = True
            stop.clear()
            token = generation
            status.set(f'Automatic Hunt scanning in background · PID {session.pid}…')
            def work():
                from trainer import Memory
                memory = None
                try:
                    memory = Memory(session.pid, writable=False)
                    result = hunt.step(session, memory, cancelled=lambda: stop.is_set() or closing())
                except Exception as exc:
                    result = exc
                finally:
                    if memory:
                        memory.close()
                results.put((token, result))
            threading.Thread(target=work, daemon=True).start()
        root.after(250, poll)

    root.after(750, poll)
    return status
