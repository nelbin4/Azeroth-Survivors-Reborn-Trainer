"""Read the live lobby role from the game's rooted Lua 5.1 global environment.

No script execution, injection, addon changes, names or game offsets. The x64
Lua ABI is validated through object tags and the main-thread/global-state cycle.
ABI reference: https://www.lua.org/source/5.1/lstate.h.html
"""
import re
import struct
import time
import threading
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from value_hunt import private_regions


def pointer(value):
    return 0x10000 <= value < 0x7FFFFFFFF000 and value % 8 == 0


def string(memory, address):
    if not pointer(address):raise ValueError('Invalid Lua string pointer.')
    raw=memory.read(address,24)
    length=struct.unpack_from('<Q',raw,16)[0]
    if raw[8]!=4 or not 0<length<=128:raise ValueError('Invalid Lua string.')
    return memory.read(address+24,length)


def table(memory,address):
    if not pointer(address):raise ValueError('Invalid Lua table pointer.')
    raw=memory.read(address,64)
    nodes=struct.unpack_from('<Q',raw,32)[0]
    if raw[8]!=5 or raw[12]>16 or not pointer(nodes):raise ValueError('Invalid Lua table.')
    count=1<<raw[12]
    free=struct.unpack_from('<Q',raw,40)[0]
    if not nodes<=free<=nodes+count*40 or (free-nodes)%40:raise ValueError('Invalid Lua node bounds.')
    # GC marks and linked-list pointers may change every frame. They do not
    # affect the table's key storage, and comparing them would reject valid
    # reads while the collector is running.
    stable=raw[12:13]+raw[32:48]
    return stable,nodes,count


def lookup(memory,address,key):
    before,nodes,count=table(memory,address)
    hash_value=len(key)
    step=(len(key)>>5)+1
    for index in range(len(key),step-1,-step):
        hash_value=(hash_value ^ ((hash_value<<5)+(hash_value>>2)+key[index-1])) & 0xFFFFFFFF
    node=nodes+(hash_value & (count-1))*40
    found=[]
    visited=set()
    for _ in range(min(count,256)):
        if node in visited or not nodes<=node<nodes+count*40 or (node-nodes)%40:raise ValueError('Invalid Lua key chain.')
        visited.add(node);raw=memory.read(node,40)
        if struct.unpack_from('<I',raw,24)[0]==4:
            try:
                if string(memory,struct.unpack_from('<Q',raw,16)[0])==key:
                    found.append((node,raw[:16]))
            except (ValueError,OSError):pass
        node=struct.unpack_from('<Q',raw,32)[0]
        if not node:break
    if table(memory,address)[0]!=before or len(found)!=1:raise ValueError('Lua table changed or key is ambiguous.')
    return found[0]


def rooted_globals(memory,state):
    raw=memory.read(state,184)
    global_state=struct.unpack_from('<Q',raw,32)[0]
    env,tag=struct.unpack_from('<QI',raw,120)
    if raw[8]!=8 or not pointer(global_state) or tag!=5:raise ValueError('Invalid Lua main thread.')
    if struct.unpack('<Q',memory.read(global_state+176,8))[0]!=state:raise ValueError('Unrooted Lua environment.')
    _,self_value=lookup(memory,env,b'_G')
    if struct.unpack_from('<QI',self_value)!=(env,5):raise ValueError('Lua globals are not self rooted.')
    return env


# Scan for the Lua thread tag, its global-state pointer, and the table-tagged
# global environment. A match is only a candidate until the live root and
# lobby table checks pass.
LUA_THREAD_PATTERN = re.compile(
    rb'\x08.{23}.{5}[\x00-\x7f]\x00\x00.{80}.{5}[\x00-\x7f]\x00\x00\x05\x00\x00\x00',
    re.DOTALL,
)


class SessionRole:
    def __init__(self,budget=128*1024*1024,seconds=.15):
        self.budget,self.seconds=budget,seconds
        self.reset()

    def reset(self):
        self.session = None
        self.cursor = 0
        self.candidates = {}
        self.regions = None
        self.state=None;self.env=None;self.slot=None;self.header=None;self.role='unknown';self.retry=0

    def read(self,memory):
        if self.state is None:raise ValueError('Lobby role is still being located.')
        # The _G self-key and main-thread cycle anchor the live environment.
        # Rehashing changes the header, so reacquire the global slot then.
        raw=memory.read(self.state,184)
        g=struct.unpack_from('<Q',raw,32)[0]
        if (raw[8]!=8 or struct.unpack_from('<QI',raw,120)!=(self.env,5)
                or not pointer(g) or struct.unpack('<Q',memory.read(g+176,8))[0]!=self.state):
            raise ValueError('Lua runtime ownership changed.')
        header,_,_=table(memory,self.env)
        if header!=self.header or self.slot is None:
            if rooted_globals(memory,self.state)!=self.env:raise ValueError('Lua environment changed.')
            self.slot,_=lookup(memory,self.env,b'AZS_RUN_LOBBY');self.header=header
        before=memory.read(self.slot,40)
        if struct.unpack_from('<I',before,24)[0]!=4 or string(memory,struct.unpack_from('<Q',before,16)[0])!=b'AZS_RUN_LOBBY':
            raise ValueError('Lobby role slot changed.')
        lobby,tag=struct.unpack_from('<QI',before)
        if tag!=5:raise ValueError('Lobby role table is unavailable.')
        _,value=lookup(memory,lobby,b'role')
        name,tag=struct.unpack_from('<QI',value)
        if tag!=4:raise ValueError('Lobby role is not a string.')
        role=string(memory,name).decode('ascii')
        if role not in ('host','guest','none'):raise ValueError('Unrecognized lobby role.')
        if memory.read(self.slot,40)!=before or table(memory,self.env)[0]!=header:
            raise ValueError('Lobby changed while reading its role.')
        self.role=role
        return role

    def step(self,session,memory,cancelled=lambda:False,regions=None):
        if session!=self.session:
            self.reset();self.session=session
        session.process()
        if self.state is not None:
            try:
                return self.read(memory)
            except (ValueError, OSError, UnicodeError):
                # Lua can replace the lobby table while the role is being
                # read. Keep the rooted runtime through that short race.
                try:
                    if rooted_globals(memory, self.state) == self.env:
                        self.slot = None
                        return 'unknown'
                except (ValueError, OSError):
                    pass
                self.reset()
                self.session = session
        if time.monotonic() < self.retry:
            return 'unknown'
        if regions is None:
            if self.regions is None:
                self.regions = tuple(private_regions(memory))
            regions = self.regions

        started = time.monotonic()
        examined = 0
        for base, size in regions:
            pos = max(base, self.cursor)
            while pos < base + size:
                if cancelled():
                    raise InterruptedError('Role detection stopped.')
                if examined >= self.budget or time.monotonic() - started >= self.seconds:
                    self.cursor = pos
                    return 'unknown'
                amount = min(1024 * 1024, base + size - pos, self.budget - examined)
                try:
                    raw = memory.read(pos, min(amount + 124, base + size - pos))
                except OSError:
                    raw = b''
                offset = 0
                while match := LUA_THREAD_PATTERN.search(raw, offset):
                    offset = match.start() + 1
                    if match.start() >= amount:
                        break
                    state = pos + match.start() - 8
                    if not pointer(state):
                        continue
                    try:
                        env = rooted_globals(memory, state)
                        lookup(memory, env, b'AZS_RUN_LOBBY')
                        self.candidates[state] = env
                    except (ValueError, OSError):
                        continue
                examined += amount
                pos += amount

        if len(self.candidates) == 1:
            self.state, self.env = next(iter(self.candidates.items()))
            self.candidates.clear()
            self.regions = None
            try:
                return self.read(memory)
            except (ValueError, OSError, UnicodeError):
                return 'unknown'

        self.reset()
        self.session = session
        self.retry = time.monotonic() + 10
        return 'unknown'


@dataclass(frozen=True)
class RoleLease:
    session: object
    state: int
    env: int

    def reader(self):
        probe=SessionRole();probe.state=self.state;probe.env=self.env
        return probe


class RoleMonitor:
    """Independent read-only scanner; stat discovery still starts with Find."""
    def __init__(self,cache=None):
        self.cache=Path(cache) if cache else None
        self.lock=threading.Lock();self.session=None;self.cancel=threading.Event()
        self.role='unknown';self.lease=None;self.last_error=None;self._cached=None

    def snapshot(self,session):
        with self.lock:
            return (self.role,self.lease) if session==self.session else ('unknown',None)

    def close(self):self.cancel.set()

    def start(self,session):
        with self.lock:
            if session==self.session:return
            self.cancel.set();self.cancel=threading.Event();cancel=self.cancel
            self.session=session;self.role='unknown';self.lease=None
            self.last_error=None;self._cached=None
        if session is None:return
        def work():
            from trainer import Memory
            probe=SessionRole(budget=128*1024*1024,seconds=.2)
            identity=[session.pid,session.created,session.path]
            try:
                data=json.loads(self.cache.read_text(encoding='utf-8')) if self.cache else {}
                if (data.get('session')==identity and isinstance(data.get('state'),int) and pointer(data['state'])
                        and isinstance(data.get('env'),int) and pointer(data['env'])):
                    probe.session=session;probe.state=data['state'];probe.env=data['env']
            except (OSError,ValueError,KeyError,TypeError):pass
            while not cancel.is_set():
                memory=None
                try:
                    session.process();memory=Memory(session.pid,writable=False)
                    role=probe.step(session,memory,cancelled=cancel.is_set)
                    lease=RoleLease(session,probe.state,probe.env) if role!='unknown' else None
                    with self.lock:
                        if cancel.is_set():return
                        self.role,self.lease=role,lease
                        self.last_error=None
                    if lease and self.cache and lease != self._cached:
                        data=dict(session=identity,state=lease.state,env=lease.env)
                        try:
                            self.cache.write_text(json.dumps(data),encoding='utf-8')
                            self._cached=lease
                        except OSError:pass
                except InterruptedError:return
                except Exception as exc:
                    message=f'{type(exc).__name__}: {exc}'
                    with self.lock:
                        if cancel.is_set():return
                        self.role,self.lease='unknown',None
                        changed=self.last_error!=message
                        self.last_error=message
                    if changed:
                        logging.debug('Session role scan failed: %s', message)
                    probe.reset()
                finally:
                    if memory:memory.close()
                cancel.wait(.5 if self.lease else .2)
        threading.Thread(target=work,daemon=True,name='Reborn session role').start()
