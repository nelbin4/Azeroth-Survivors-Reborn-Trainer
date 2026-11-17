"""Bounded, read-only live integer searches independent of game build layouts."""
import ctypes
from ctypes import wintypes
import re
import struct


FORMATS = {'32-bit integer': 'I', '64-bit integer': 'Q'}


def encoded(value, kind):
    value = int(str(value).strip())
    fmt = FORMATS[kind]
    if not 0 <= value < 1 << (struct.calcsize(fmt) * 8):
        raise ValueError('Value is outside the selected unsigned integer range.')
    return struct.pack('<' + fmt, value)


def private_regions(memory):
    class Region(ctypes.Structure):
        _fields_ = [('base', ctypes.c_uint64), ('allocation', ctypes.c_uint64),
                    ('allocation_protect', wintypes.DWORD), ('padding', wintypes.DWORD),
                    ('size', ctypes.c_uint64), ('state', wintypes.DWORD),
                    ('protect', wintypes.DWORD), ('type', wintypes.DWORD), ('padding2', wintypes.DWORD)]
    query = memory.api.VirtualQueryEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(Region), ctypes.c_size_t]
    query.restype = ctypes.c_size_t
    address = 0x10000
    for _ in range(100000):
        region = Region()
        if not query(memory.handle, address, ctypes.byref(region), ctypes.sizeof(region)):
            break
        end = region.base + region.size
        if end <= address:
            break
        if (region.state == 0x1000 and region.type == 0x20000 and not region.protect & 0x100
                and region.protect & 0xFF in (2, 4, 8)):
            yield region.base, region.size
        address = end


class ValueHunt:
    def __init__(self, session, kind, budget=256 * 1024 * 1024, limit=4096):
        self.session, self.kind = session, kind
        self.budget, self.limit = budget, limit
        self.addresses = []
        self.scanned = 0
        self.limited = False
        self.next_address = None
        self.needle = None

    def scan(self, memory, value, regions=None, more=False):
        self.session.process()
        needle = encoded(value, self.kind)
        if more:
            if needle != self.needle or self.next_address is None or len(self.addresses) >= self.limit:
                raise ValueError('Continue requires the same value and an unfinished search below the candidate limit.')
        else:
            self.addresses, self.scanned = [], 0
            self.next_address = 0
        self.needle = needle
        self.limited = False
        resume = self.next_address
        before = self.scanned
        width = len(needle)
        for base, size in (private_regions(memory) if regions is None else regions):
            position = max(base, resume)
            while position < base + size:
                self.session.process()
                amount = min(1024 * 1024, base + size - position, self.budget - (self.scanned - before))
                if amount <= 0:
                    self.limited = True
                    self.next_address = position
                    return self.addresses
                try:
                    raw = memory.read(position, amount)
                except OSError:
                    raw = b''
                self.scanned += amount
                # Include overlapping matches so an unaligned copy cannot hide an aligned one.
                for match in re.finditer(b'(?=' + re.escape(needle) + b')', raw):
                    address = position + match.start()
                    if address % width == 0:
                        self.addresses.append(address)
                        if len(self.addresses) >= self.limit:
                            self.limited = True
                            self.next_address = None
                            return self.addresses
                position += amount
        self.session.process()
        self.next_address = None
        return self.addresses

    def refine(self, memory, value):
        self.session.process()
        needle = encoded(value, self.kind)
        found = []
        for address in self.addresses:
            self.session.process()
            try:
                if memory.read(address, len(needle)) == needle:
                    found.append(address)
            except OSError:
                continue
        self.addresses = found
        self.next_address = None
        self.limited = False
        self.session.process()
        return found


def add_manual_panel(root, frame, session_provider, closing):
    import queue
    import threading
    import tkinter as tk
    from tkinter import ttk
    ttk.Label(frame, text='Hunt live values', font=('Segoe UI', 19, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Works independently of automatic layout detection. Enter a current in-game integer value, search, '
              'change that value in the game, then refine using its new value. Candidates are read-only; a numeric match '
              'alone does not prove which stat it represents.', wraplength=1020).pack(anchor='w', pady=12)
    row = ttk.Frame(frame)
    row.pack(fill='x')
    ttk.Label(row, text='Current value').pack(side='left')
    value = tk.StringVar()
    ttk.Entry(row, textvariable=value, width=18).pack(side='left', padx=10)
    kind = tk.StringVar(value='32-bit integer')
    ttk.Combobox(row, textvariable=kind, values=list(FORMATS), state='readonly', width=18).pack(side='left')
    status = tk.StringVar(value='Use 32-bit for stat bonuses and gold; 64-bit for health. Prefer a distinctive nonzero value.')
    ttk.Label(frame, textvariable=status, wraplength=1020).pack(anchor='w', pady=12)
    output = tk.Text(frame, height=16, bg='#0c1826', fg='#e5e9ef', state='disabled')
    output.pack(fill='both', expand=True)
    results = queue.Queue()
    hunt = None
    busy = False

    def start(refine=False, more=False):
        nonlocal busy
        if busy:
            return
        try:
            encoded(value.get(), kind.get())
            session = session_provider()
            if not session:
                raise RuntimeError('Start one Reborn process, or select its executable to disambiguate.')
            session.process()
            if (refine or more) and (not hunt or hunt.session != session or hunt.kind != kind.get()):
                raise RuntimeError('Process or value type changed. Start a new search.')
            candidate = hunt if refine or more else ValueHunt(session, kind.get())
            requested = value.get()
            busy = True
            status.set(f'{"Refining" if refine else "Searching private game memory"} · PID {session.pid}…')
            def work():
                from trainer import Memory
                memory = None
                try:
                    memory = Memory(session.pid, writable=False)
                    candidate.refine(memory, requested) if refine else candidate.scan(memory, requested, more=more)
                    result = candidate
                except Exception as exc:
                    result = exc
                finally:
                    if memory:
                        memory.close()
                results.put(result)
            threading.Thread(target=work, daemon=True).start()
        except Exception as exc:
            status.set(str(exc))

    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=12)
    search = ttk.Button(buttons, text='New hunt', command=start)
    search.pack(side='left')
    refine = ttk.Button(buttons, text='Refine with new value', command=lambda: start(True))
    refine.pack(side='left', padx=10)
    more = ttk.Button(buttons, text='Continue hunt', command=lambda: start(more=True))
    more.pack(side='left')

    def poll():
        nonlocal hunt, busy
        if closing():
            return
        try:
            result = results.get_nowait()
            busy = False
            if isinstance(result, Exception):
                hunt = None
                status.set(str(result))
            else:
                hunt = result
                if hunt.limited:
                    instruction = ('Continue hunt or refine these candidates.' if hunt.next_address is not None
                                   else 'Candidate limit reached; use a more distinctive value or refine.')
                else:
                    instruction = 'Change the value in-game, then refine.'
                status.set(f'{len(hunt.addresses)} candidates · {hunt.scanned / 1048576:.1f} MiB examined · {instruction}')
                output.configure(state='normal')
                output.delete('1.0', 'end')
                output.insert('end', '\n'.join(f'0x{address:016X}' for address in hunt.addresses[:500]))
                output.configure(state='disabled')
        except queue.Empty:
            pass
        search.configure(state='disabled' if busy else 'normal')
        refine.configure(state='normal' if not busy and hunt and hunt.addresses else 'disabled')
        more.configure(state='normal' if not busy and hunt and hunt.next_address is not None else 'disabled')
        if not closing():
            root.after(200, poll)
    root.after(200, poll)
    return frame
