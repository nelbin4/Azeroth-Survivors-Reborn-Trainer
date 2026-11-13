"""Locate the solo server Game on its thread stack without injecting code."""
import ctypes
from ctypes import wintypes
import json
import struct

import psutil



class ThreadInfo(ctypes.Structure):
    _fields_ = [('exit', wintypes.LONG), ('teb', ctypes.c_void_p),
                ('pid', ctypes.c_void_p), ('tid', ctypes.c_void_p),
                ('mask', ctypes.c_size_t), ('priority', wintypes.LONG), ('base_priority', wintypes.LONG)]


def stacks(process, memory):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    query = ctypes.WinDLL('ntdll').NtQueryInformationThread
    query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p]
    query.restype = wintypes.LONG
    for thread in process.threads():
        handle = kernel.OpenThread(0x40, False, thread.id)
        if not handle:
            continue
        try:
            info = ThreadInfo()
            if query(handle, 0, ctypes.byref(info), ctypes.sizeof(info), None) != 0:
                continue
            top, bottom = struct.unpack('<2Q', memory.read(info.teb + 8, 16))
            if 0 < top - bottom <= 16 * 1024 * 1024:
                yield thread.id, bottom, top
        except OSError:
            continue
        finally:
            kernel.CloseHandle(handle)


def discover(process, memory):
    found = []
    for tid, bottom, top in stacks(process, memory):
        try:
            raw = memory.read(bottom, top - bottom)
        except OSError:
            continue
        for i in range(0xBA8, len(raw) - 0x300, 8):
            cap, pointer, count = struct.unpack_from('<3Q', raw, i)
            if not 1 <= count <= cap <= 4 or pointer % 8 or not 0x10000 <= pointer < 0x7FFFFFFFF000:
                continue
            game = bottom + i - 0xBA8
            index = struct.unpack_from('<Q', raw, i - 0xBA8 + 0xE60)[0]
            if index >= count:
                continue
            hero = pointer + index * 0x1798
            try:
                h = memory.read(hero, 0x1798)
                guid, hp, maximum = struct.unpack_from('<3Q', h, 0xF0)
                level = struct.unpack_from('<I', h, 0x150)[0]
                ncap, nptr, nlen = struct.unpack_from('<3Q', h, 0x1038)
                if not (guid and 1 <= maximum <= 10**12 and hp <= maximum and 1 <= level <= 10000
                        and 1 <= nlen <= ncap <= 128 and 0x10000 <= nptr < 0x7FFFFFFFF000):
                    continue
                name = memory.read(nptr, nlen).decode('utf-8')
                if not name.isprintable():
                    continue
                found.append(dict(game=hex(game), hero=hex(hero), guid=guid, name=name, level=level,
                                  hp=hp, maximum=maximum, count=count, index=index, tid=tid))
            except (OSError, UnicodeError):
                continue
    return found


if __name__ == '__main__':
    from trainer import Memory, find_games
    p, = find_games()
    memory = Memory(p.pid)
    try:
        print(json.dumps(discover(p, memory), indent=2))
    finally:
        memory.close()
