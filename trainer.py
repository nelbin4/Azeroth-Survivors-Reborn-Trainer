"""Azeroth Survivor Reborn Trainer. Copyright (c) 2026 nelbin4. MIT license."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import struct
import sys
import threading
import queue
import time
from runtime import RetrySchedule, same_session, RunOptionDefaults, RUN_OPTION_DEFAULTS
from game_process import GameLauncher, GameSession, PROCESS_NAME, same_path
import updater
import webbrowser

import psutil
from player import Player, STATS
from compatibility import resolve, resolve_native, Layout

APP_NAME = 'Azeroth Survivor Reborn Trainer'
VERSION = '0.8.2'
ROOT = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
SETTINGS = ROOT / 'reborn-settings.json'
JOURNAL = ROOT / 'reborn-recovery.json'
BUILD = 'ab7b6332a6c47e4c29188e44932939e560b274cf06e0f212cb3ada119a3a3d35'
CONFIG_RVA = 0xA6EF610
# Instructions independently identify the config constructor, comparison and live reader.
SIGNATURES = {
    0x7139363: bytes.fromhex('0f958208020000'),
    0x71417F8: bytes.fromhex('0fb68208020000'),
    0x6EB6123: bytes.fromhex('0fb635ee968303'),
}


@dataclass(frozen=True)
class Feature:
    label: str
    offset: int
    fmt: str
    minimum: float = 0
    maximum: float = 1
    note: str = 'Live config'

    def encode(self, value):
        if self.fmt == 'B':
            if value not in (0, 1, False, True):
                raise ValueError(f'{self.label}: choose on or off.')
            return struct.pack('<B', int(value))
        value = float(value)
        if not math.isfinite(value) or not self.minimum <= value <= self.maximum:
            raise ValueError(f'{self.label}: enter {self.minimum:g} to {self.maximum:g}.')
        return struct.pack('<' + self.fmt, value)


FEATURES = {
    'god': Feature('God mode', 0x208, 'B', note='Invulnerability; live verification pending'),
    'autopick': Feature('Auto-pick level-up cards', 0x209, 'B', note='Random card selection'),
    'all_zones': Feature('Open all zones / difficulties', 0x20B, 'B', note='Refresh the hero menu'),
    'showcase': Feature('Showcase pickups', 0x20A, 'B', note='At the start of the next run'),
    'unlock_all': Feature('Unlock gear / cosmetics / talent points', 0x214, 'B', note='Test-save option; next login, unverified'),
    'rift': Feature('Enable boss rift bar', 0x20D, 'B'),
    'rift_clock': Feature('Bosses also arrive on the clock', 0x20E, 'B'),
    'rifts': Feature('Enable rifts', 0x20F, 'B'),
    'vaults': Feature('Enable vault portals', 0x210, 'B'),
    'timescale': Feature('Run clock speed', 0x1F8, 'f', 0.1, 10, '1 = normal; affects the run clock'),
    'clock': Feature('Next run starts at minute', 0x1FC, 'f', 0, 3600, 'Stored as seconds; earlier bosses count as slain'),
}


def find_games(path=None):
    result = []
    for process in psutil.process_iter(['name', 'exe', 'create_time']):
        try:
            if (process.info['name'] or '').casefold() != PROCESS_NAME.casefold():
                continue
            exe = Path(process.info['exe']).resolve()
            if path and os.path.normcase(str(exe)) != os.path.normcase(str(Path(path).resolve())):
                continue
            result.append(process)
        except (psutil.Error, OSError, TypeError):
            continue
    return result


class Memory:
    def __init__(self, pid, writable=False):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        for name in ('ReadProcessMemory', 'WriteProcessMemory'):
            method = getattr(self.api, name)
            method.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
            method.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.OpenProcess(0x410 | (0x28 if writable else 0), False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def read(self, address, size):
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.api.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(count)) or count.value != size:
            raise OSError(f'Cannot read game memory at {address:#x}.')
        return buffer.raw

    def write(self, address, raw):
        count = ctypes.c_size_t()
        buffer = ctypes.create_string_buffer(raw)
        if not self.api.WriteProcessMemory(self.handle, address, buffer, len(raw), ctypes.byref(count)) or count.value != len(raw):
            raise OSError(f'Cannot write game memory at {address:#x}.')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def atomic_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(temp, path)


class Trainer:
    def __init__(self, path=None, writable=True, journal=JOURNAL, session=None):
        self.write_gate = threading.RLock()
        self.path = path
        self.memory = None
        self.process = None
        self.journal = journal
        self.changes = {}
        self.lock = None
        if session and path and not same_path(session.path, path):
            raise RuntimeError('Selected process does not match the game path.')
        games = [session.process()] if session else find_games(path)
        if not games:
            raise RuntimeError('Waiting for Azeroth Survivors: Reborn.')
        if len(games) != 1:
            raise RuntimeError('Multiple Reborn games found. Select the exact game path.')
        self.process = games[0]
        self.created = self.process.create_time()
        self.path = self.process.exe()
        try:
            self.profile = resolve(self.path)
        except RuntimeError as full_error:
            if not str(full_error).startswith('Unsupported Reborn build'):
                raise
            try:
                self.profile = resolve_native(self.path)
            except Exception:
                raise full_error
        self.player_supported = isinstance(self.profile, Layout)
        self.build = self.profile.digest
        self.config_rva = self.profile.config
        self.signatures = self.profile.signatures
        modules = [m for m in self.process.memory_maps(grouped=False)
                   if os.path.normcase(m.path) == os.path.normcase(self.path)]
        if not modules:
            raise RuntimeError('Cannot locate the game module.')
        self.base = min(int(m.addr.split('-')[0], 16) for m in modules)
        try:
            if writable:
                self.lock = InstanceLock(f'{self.process.pid}_{self.created}')
            self.memory = Memory(self.process.pid, writable)
            self.guard()
            if journal.exists():
                previous = json.loads(journal.read_text(encoding='utf-8'))
                if (previous.get('pid'), previous.get('created'), previous.get('base'), previous.get('build')) == self.identity():
                    for key, change in previous.get('changes', {}).items():
                        if key not in FEATURES:
                            raise RuntimeError('Recovery journal contains an unknown field.')
                        for field in ('before', 'after'):
                            raw = bytes.fromhex(change[field])
                            feature = FEATURES[key]
                            if len(raw) != struct.calcsize('<' + feature.fmt):
                                raise RuntimeError('Recovery journal has an invalid field size.')
                            feature.encode(struct.unpack('<' + feature.fmt, raw)[0])
                        self.changes[key] = change
        except Exception:
            if self.memory:
                self.memory.close()
            if self.lock:
                self.lock.close()
            raise

    def identity(self):
        return self.process.pid, self.created, self.base, getattr(self, 'build', BUILD)

    def guard(self):
        if not self.process.is_running() or self.process.create_time() != self.created:
            raise RuntimeError('Game restarted; reconnect before changing options.')
        if os.path.normcase(self.process.exe()) != os.path.normcase(self.path):
            raise RuntimeError('Game process path changed.')
        for rva, expected in getattr(self, 'signatures', SIGNATURES).items():
            if self.memory.read(self.base + rva, len(expected)) != expected:
                raise RuntimeError('Game instruction mismatch; no changes applied.')
        config = self.memory.read(self.base + getattr(self, 'config_rva', CONFIG_RVA), 0x234)
        if struct.unpack_from('<I', config, 0x230)[0] != 0:
            raise RuntimeError('Reborn configuration is not initialized.')
        if any(config[FEATURES[k].offset] not in (0, 1) for k in FEATURES if FEATURES[k].fmt == 'B'):
            raise RuntimeError('Unexpected configuration flags; changes blocked.')
        speed, clock = struct.unpack_from('<2f', config, 0x1F8)
        if not math.isfinite(speed) or not 0 < speed <= 1000 or not math.isfinite(clock) or not 0 <= clock <= 1e9:
            raise RuntimeError('Unexpected configuration values; changes blocked.')
        return config

    def values(self):
        config = self.guard()
        result = {k: struct.unpack_from('<' + f.fmt, config, f.offset)[0] for k, f in FEATURES.items()}
        result['clock'] /= 60
        return result

    def persist(self):
        pid, created, base, build = self.identity()
        atomic_json(self.journal, dict(pid=pid, created=created, base=base, build=build, changes=self.changes))

    def set(self, key, value):
        with getattr(self,'write_gate',threading.RLock()):
            return self._set(key,value)

    def _set(self, key, value):
        feature = FEATURES[key]
        if key == 'clock':
            value = float(value) * 60
        after = feature.encode(value)
        config = self.guard()
        before = config[feature.offset:feature.offset + len(after)]
        if before == after:
            return
        if key in self.changes and before.hex() != self.changes[key]['after']:
            raise RuntimeError('This option was changed outside the trainer. Restore changes and retry.')
        change = self.changes.setdefault(key, dict(before=before.hex(), after=before.hex()))
        change['after'] = after.hex()
        self.persist()  # recovery information reaches disk before any game write
        self.guard()
        address = self.base + getattr(self, 'config_rva', CONFIG_RVA) + feature.offset
        if self.memory.read(address, len(before)) != before:
            raise RuntimeError('Game configuration changed during the operation.')
        self.memory.write(address, after)
        if self.memory.read(address, len(after)) != after:
            raise RuntimeError('Game did not retain the requested option.')
        logging.info('PID %s: %s = %s', self.process.pid, key, value)

    def restore(self):
        with getattr(self,'write_gate',threading.RLock()):
            return self._restore()

    def _restore(self):
        self.guard()
        for key, change in list(self.changes.items()):
            self.guard()
            feature = FEATURES[key]
            address = self.base + getattr(self, 'config_rva', CONFIG_RVA) + feature.offset
            before, after = bytes.fromhex(change['before']), bytes.fromhex(change['after'])
            if self.memory.read(address, len(after)) == after:
                self.memory.write(address, before)
                if self.memory.read(address, len(before)) != before:
                    raise RuntimeError(f'Could not restore {feature.label}.')
            del self.changes[key]
            self.persist()

    def close(self):
        with getattr(self,'write_gate',threading.RLock()):
            self._close()

    def _close(self):
        if self.memory:
            self.memory.close()
            self.memory = None
        if self.lock:
            self.lock.close()
            self.lock = None


class InstanceLock:
    """Named-object lifetime lock; no thread ownership or abandoned-mutex races."""
    def __init__(self, identity='test'):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        ctypes.set_last_error(0)
        self.handle = self.api.CreateMutexW(None, False, 'Local\\RebornTrainer_' + key)
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        if error == 183:
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError('Another Reborn trainer is already controlling this game.')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        if getattr(self, 'handle', None):
            self.close()


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry('1200x860')
    root.minsize(1100, 760)
    root.configure(bg='#14202c')
    style = ttk.Style(root)
    style.theme_use('clam')
    root.option_add('*Font', ('Segoe UI', 10))
    style.configure('TFrame', background='#14202c')
    style.configure('TLabel', background='#14202c', foreground='#e5e9ef')
    style.configure('TCheckbutton', background='#14202c', foreground='#e5e9ef')
    style.map('TCheckbutton', background=[('active', '#203448')])
    style.configure('TButton', padding=(12, 7), background='#24364b', foreground='#e5e9ef', borderwidth=0)
    style.map('TButton', background=[('active', '#35516c')], foreground=[('disabled', '#728399')])
    style.configure('Accent.TButton', background='#56d6b0', foreground='#10251f', font=('Segoe UI', 10, 'bold'))
    style.map('Accent.TButton', background=[('active', '#7ce5c6'), ('disabled', '#263c3d')])
    style.configure('FindHero.TButton', background='#56d6b0', foreground='#10251f',
                    font=('Segoe UI', 16, 'bold'), padding=(24, 12))
    style.map('FindHero.TButton', background=[('active', '#7ce5c6'), ('disabled', '#263c3d')],
              foreground=[('disabled', '#728399')])
    style.configure('TEntry', fieldbackground='#0c1826', foreground='#edf5ff', insertcolor='#edf5ff', padding=5)
    style.configure('TLabelframe', background='#14202c', bordercolor='#34475d', relief='solid')
    style.configure('TLabelframe.Label', background='#14202c', foreground='#76dfc1', font=('Segoe UI', 10, 'bold'))
    style.configure('TNotebook', background='#14202c', borderwidth=0)
    style.configure('TNotebook.Tab', background='#24364b', foreground='#bdcbdc', padding=(20, 10))
    style.map('TNotebook.Tab', background=[('selected', '#36536a')], foreground=[('selected', '#ffffff')],
              padding=[('selected', (20, 10)), ('!selected', (20, 10))])
    body = ttk.Frame(root, padding=18)
    body.pack(fill='both', expand=True)
    header = ttk.Frame(body)
    header.pack(fill='x', pady=(0, 8))
    ttk.Label(header, text='AZEROTH SURVIVORS REBORN', foreground='#e3be69', font=('Segoe UI', 20, 'bold')).pack(side='left')
    ttk.Label(header, text=f'v{VERSION}  ·  Solo controls', foreground='#a9b8c8').pack(side='left', padx=16)
    status = tk.StringVar(value='Waiting for Azeroth Survivors: Reborn.')
    detail = tk.StringVar(value='')
    ttk.Label(body, textvariable=status, font=('Segoe UI', 10), foreground='#76dfc1', wraplength=1030).pack(anchor='w', pady=(0, 6))
    # Full executable path remains in settings; avoid a second empty status row.
    tabs = ttk.Notebook(body)
    tabs.pack(fill='both', expand=True)
    player_shell = ttk.Frame(tabs)
    player_canvas = tk.Canvas(player_shell, background='#14232e', highlightthickness=0)
    player_scroll = ttk.Scrollbar(player_shell, orient='vertical', command=player_canvas.yview)
    player_canvas.configure(yscrollcommand=player_scroll.set)
    player_scroll.pack(side='right', fill='y')
    player_canvas.pack(side='left', fill='both', expand=True)
    player_tab = ttk.Frame(player_canvas, padding=10)
    player_content = player_canvas.create_window((0, 0), window=player_tab, anchor='nw')
    player_tab.bind('<Configure>', lambda event: player_canvas.configure(scrollregion=player_canvas.bbox('all')))
    player_canvas.bind('<Configure>', lambda event: player_canvas.itemconfigure(player_content, width=event.width))
    def wheel(event):
        if tabs.select() == str(player_shell):
            player_canvas.yview_scroll(-int(event.delta / 120), 'units')
    root.bind('<MouseWheel>', wheel)
    native_tab = ttk.Frame(tabs, padding=10)
    progression_tab = ttk.Frame(tabs, padding=12)
    tabs.add(player_shell, text='Live controls')
    tabs.add(native_tab, text='Run options')
    tabs.add(progression_tab, text='Saves')
    updates_tab = ttk.Frame(tabs, padding=24)
    tabs.add(updates_tab, text='Updates')
    options = ttk.Frame(native_tab)
    options.pack(fill='x')
    variables = {}
    widgets = []
    trainer = None
    player = None
    player_widgets = []
    player_targets = {}
    player_holds = {}
    player_current = {}
    pending = queue.Queue()
    hero_pending = queue.Queue()
    hero_searching = False
    hero_generation = 0
    connection_generation = 0
    launcher = GameLauncher()
    next_process_scan = 0
    retry = RetrySchedule()
    run_defaults = RunOptionDefaults()
    connecting = False
    closing = False
    try:
        saved = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}

    from value_hunt import add_hunt_tab
    hunt_tab = add_hunt_tab(root, tabs, lambda: launcher.session, lambda: closing,
                            lambda: trainer is None or not trainer.player_supported, lambda: trainer)

    def error(exc):
        logging.exception('Trainer operation failed')
        status.set(str(exc))

    def toggle(key):
        if not trainer:
            return
        try:
            trainer.set(key, variables[key].get())
            status.set(f'{FEATURES[key].label}: {"on" if variables[key].get() else "off"}')
        except Exception as exc:
            error(exc)
            refresh_values()

    for key, feature in FEATURES.items():
        row = ttk.Frame(options)
        row.pack(fill='x', pady=5)
        if feature.fmt == 'B':
            variable = tk.BooleanVar(value=bool(RUN_OPTION_DEFAULTS.get(key, False)))
            widget = ttk.Checkbutton(row, text=feature.label, variable=variable, command=lambda k=key: toggle(k))
            widget.pack(side='left')
        else:
            variable = tk.StringVar(value=str(RUN_OPTION_DEFAULTS.get(key, 0)))
            ttk.Label(row, text=feature.label).pack(side='left')
            widget = ttk.Entry(row, textvariable=variable, width=8)
            widget.pack(side='right')
            button = ttk.Button(row, text='Apply', width=7, command=lambda k=key: apply_number(k))
            button.pack(side='right', padx=8)
            widgets.append(button)
        variables[key] = variable
        widgets.append(widget)

    def apply_number(key):
        if trainer:
            try:
                trainer.set(key, variables[key].get())
                status.set(f'{FEATURES[key].label} applied and read back.')
            except Exception as exc:
                error(exc)
                refresh_values()

    def refresh_values():
        if trainer:
            try:
                for key, value in trainer.values().items():
                    variables[key].set(bool(value) if FEATURES[key].fmt == 'B' else f'{value:g}')
            except Exception as exc:
                error(exc)

    ttk.Label(native_tab, text='Menu / next-run options take effect when the game reads them.\nThe native unlock-all flag alone is unverified; use Saves for progression upgrades.',
              wraplength=600, foreground='#a9b8c8').pack(anchor='w', pady=(14, 12))
    hero_status = tk.StringVar(value='No solo hero connected. Start a run and use Find hero.')
    xp_assist = tk.BooleanVar(value=False)
    hold_all = tk.BooleanVar(value=False)
    selected_stat = tk.StringVar(value='damage')
    hero_row = ttk.Frame(player_tab)
    hero_row.pack(fill='x', pady=(0, 10))
    ttk.Label(hero_row, textvariable=hero_status, wraplength=760, font=('Segoe UI', 11)).pack(side='left')
    workspace = ttk.Frame(player_tab)
    workspace.pack(fill='both', expand=True)
    workspace.columnconfigure(0, weight=2, uniform='split')
    workspace.columnconfigure(2, weight=1, uniform='split')
    left = ttk.Frame(workspace, padding=(0, 6, 24, 0))
    left.grid(row=0, column=0, sticky='nsew')
    ttk.Separator(workspace, orient='vertical').grid(row=0, column=1, sticky='ns', padx=8)
    inspector = ttk.Frame(workspace, padding=(18, 6, 0, 0))
    inspector.grid(row=0, column=2, sticky='nsew')
    ttk.Label(left, text='Essential controls', font=('Segoe UI', 19, 'bold')).pack(anchor='w', pady=(6, 14))
    for label, variable, callback in (
        ('God mode', variables['god'], lambda: toggle('god')),
        ('XP assistance', xp_assist, lambda: toggle_xp_assist()),
        ('Hold max stats', hold_all, lambda: maximize_player() if hold_all.get() else pause_holds())):
        line = ttk.Frame(left)
        line.pack(fill='x', pady=2)
        ttk.Label(line, text=label, font=('Segoe UI', 12, 'bold')).pack(side='left')
        switch = ttk.Checkbutton(line, text='Enabled', variable=variable, command=callback)
        switch.pack(side='right')
        (widgets if label == 'God mode' else player_widgets).append(switch)
        ttk.Separator(left).pack(fill='x', pady=3)
    hero_actions = ttk.Frame(left)
    hero_actions.pack(fill='x', pady=(10, 12))

    def find_hero():
        nonlocal player, hero_searching, hero_generation
        if not trainer or hero_searching:
            return
        try:
            if player:
                player.stop()
            player = None
            for held in player_holds.values():
                held.set(False)
            xp_assist.set(False)
            hold_all.set(False)
            hero_searching = True
            hero_generation += 1
            generation = hero_generation
            identity = trainer.identity()
            path = trainer.path
            session = GameSession(trainer.process.pid, trainer.created, trainer.path)
            hero_status.set('Finding your solo hero… You can keep using this window.')
            def search():
                reader = None
                try:
                    reader = Trainer(path, writable=False, session=session)
                    if not same_session(identity, reader.identity()):
                        raise RuntimeError('Game restarted during discovery. Find hero again.')
                    candidate = Player(reader)
                    candidate.connect()
                    result = dict(candidate.lease)
                except Exception as exc:
                    result = exc
                finally:
                    if reader:
                        reader.close()
                hero_pending.put((generation, identity, result))
            threading.Thread(target=search, daemon=True).start()
        except Exception as exc:
            hero_searching = False
            hero_status.set(str(exc))
            error(exc)

    find_button = ttk.Button(header, text='Find Hero', width=18, style='FindHero.TButton', command=find_hero)
    find_button.pack(side='right')
    widgets.append(find_button)

    def show_hero(snapshot):
        hero_status.set(f"{snapshot['name']}  ·  Level {snapshot['level']}  ·  Health {snapshot['hp']:,} / {snapshot['maximum']:,}\nGold {snapshot['gold']:,}  ·  XP {snapshot['xp']:,} / {snapshot['next_xp']:,}  ·  Holds {len(player.frozen)} / {len(STATS)}")
        for key, value in snapshot['values'].items():
            player_current[key].set(display_stat(key, value))
        hold_all.set(len(player.frozen) == len(STATS))

    def apply_stat(key, hold=False):
        if not player:
            return
        try:
            if hold:
                player.hold(key, player_targets[key].get(), player_holds[key].get())
            else:
                player.set_stat(key, player_targets[key].get())
                if player_holds[key].get():
                    player.hold(key, player_targets[key].get())
            show_hero(player.snapshot())
            status.set(f'{STATS[key].label} applied.')
        except Exception as exc:
            player_holds[key].set(False)
            player.frozen.pop(key, None)
            error(exc)

    labels = {'damage':'Damage bonus', 'crit':'Critical strike', 'crit_damage':'Critical damage',
              'cooldown':'Cooldown reduction', 'armor':'Damage reduction', 'speed':'Movement speed',
              'regen':'Regeneration bonus', 'pickup':'Pickup range', 'gold_bonus':'Gold find',
              'area':'Area of effect', 'max_health':'Maximum health', 'run_gold':'Run gold',
              'targets':'Extra targets', 'luck':'Luck', 'rerolls':'Free rerolls'}
    favorites = ('damage', 'crit', 'cooldown', 'armor', 'pickup', 'rerolls')
    show_all = tk.BooleanVar(value=False)
    stat_rows = {}
    stat_list = ttk.Frame(left)
    stat_list.pack(fill='x')
    def display_stat(key, value):
        value += 8 if key == 'crit' else 50 if key == 'crit_damage' else 0
        return f'{value:,}' + ('' if key in ('targets', 'regen', 'max_health', 'run_gold', 'rerolls') else '%')

    def select_stat(key):
        selected_stat.set(key)
        player_canvas.yview_moveto(0)
        editor_title.set(labels[key])
        editor_live.configure(textvariable=player_current[key])
        editor_target.configure(textvariable=player_targets[key])
        editor_hold.configure(variable=player_holds[key])
        minimum = 1 if key == 'max_health' else 0
        editor_range.set(f'Supported range: {minimum:,} – {STATS[key].maximum:,}' +
                         (' · bonus above base 8%' if key == 'crit' else ' · bonus above base 50%' if key == 'crit_damage' else ''))
        for item, row in stat_rows.items():
            row.configure(style='Selected.TFrame' if item == key else 'TFrame')

    style.configure('Selected.TFrame', background='#23443f')
    for key, stat in STATS.items():
        current = tk.StringVar(value='—')
        player_current[key] = current
        player_targets[key] = tk.StringVar(value=str(stat.default))
        player_holds[key] = tk.BooleanVar(value=False)
        row = ttk.Frame(stat_list, padding=(8, 1))
        stat_rows[key] = row
        ttk.Label(row, text=labels[key], width=23, font=('Segoe UI', 11)).pack(side='left')
        live_label = ttk.Label(row, textvariable=current, foreground='#76dfc1', font=('Segoe UI', 11, 'bold'))
        ttk.Button(row, text='Edit', width=5, command=lambda k=key: select_stat(k)).pack(side='right')
        live_label.pack(side='right', padx=24)
    def show_stats():
        for row in stat_rows.values():
            row.pack_forget()
        for key in (STATS if show_all.get() else favorites):
            stat_rows[key].pack(fill='x', pady=2)
        all_button.configure(text='Show essential controls' if show_all.get() else 'Show all 15 controls')
    def toggle_stats():
        show_all.set(not show_all.get())
        show_stats()
    all_button = ttk.Button(left, text='Show all 15 controls', command=toggle_stats)
    all_button.pack(anchor='w', pady=(12, 0))
    show_stats()

    editor_title = tk.StringVar(value='Damage bonus')
    ttk.Label(inspector, textvariable=editor_title, font=('Segoe UI', 19, 'bold')).pack(anchor='w', pady=(6, 14))
    ttk.Label(inspector, text='Current value', foreground='#a9b8c8').pack(anchor='w')
    editor_live = ttk.Label(inspector, foreground='#76dfc1', font=('Segoe UI', 30, 'bold'))
    editor_live.pack(anchor='w', pady=(4, 14))
    ttk.Label(inspector, text='Target value').pack(anchor='w', pady=(0, 6))
    editor_target = ttk.Entry(inspector, font=('Segoe UI', 19))
    editor_target.pack(fill='x', pady=(0, 8))
    editor_range = tk.StringVar()
    ttk.Label(inspector, textvariable=editor_range, wraplength=310, foreground='#a9b8c8').pack(anchor='w')
    editor_hold = ttk.Checkbutton(inspector, text='Hold this value', command=lambda: apply_stat(selected_stat.get(), True))
    editor_hold.pack(anchor='w', pady=(16, 12))
    editor_apply = ttk.Button(inspector, text='Apply value', style='Accent.TButton', command=lambda: apply_stat(selected_stat.get()))
    editor_apply.pack(fill='x')
    player_widgets.extend((editor_target, editor_hold, editor_apply))
    select_stat('damage')
    ttk.Label(inspector, text='Hold reapplies the target after the game recalculates it.', wraplength=300, foreground='#a9b8c8').pack(anchor='w', pady=16)
    ttk.Separator(inspector).pack(fill='x', pady=(0, 14))
    quick = ttk.Frame(inspector)
    quick.pack(fill='x', pady=(12, 5))
    gold_target = tk.StringVar(value='999999')

    def player_action(action):
        if player:
            try:
                if action == 'gold':
                    player.gold(gold_target.get())
                elif action == 'heal':
                    player.heal()
                else:
                    player.ready_level()
                show_hero(player.snapshot())
                status.set('Player action applied and read back.')
            except Exception as exc:
                error(exc)

    gold_entry = ttk.Entry(quick, width=10, textvariable=gold_target)
    # Run gold is edited through the resource stat inspector.
    gold_button = ttk.Button(quick, text='Set run gold', command=lambda: player_action('gold'))
    # Keep the existing action available to backend calls, without duplicate UI.
    heal_button = ttk.Button(quick, text='Heal to full', command=lambda: player_action('heal'))
    heal_button.pack(fill='x', pady=3)
    xp_button = ttk.Button(quick, text='One XP from level-up', command=lambda: player_action('xp'))
    xp_button.pack(fill='x', pady=3)
    player_widgets.extend([gold_entry, gold_button, heal_button, xp_button])
    def maximize_player():
        if not player or not player.lease:
            return
        try:
            snapshot = player.maximize()
            trainer.set('god', 1)
            for key, stat in STATS.items():
                player_targets[key].set(str(stat.maximum))
                player_holds[key].set(True)
            refresh_values()
            show_hero(snapshot)
            xp_assist.set(True)
            status.set('15 stats/resources held; full health; XP assistance and God mode enabled.')
        except Exception as exc:
            for key in STATS:
                player_holds[key].set(key in player.frozen)
            error(exc)

    max_button = ttk.Button(hero_actions, text='Max all stats', command=maximize_player, style='Accent.TButton')
    max_button.pack(side='left', padx=(0, 8))
    player_widgets.append(max_button)
    def pause_holds():
        if player:
            player.frozen.clear()
            player.xp_assist = False
            for held in player_holds.values():
                held.set(False)
            xp_assist.set(False)
            hold_all.set(False)
            status.set('Holds paused. Current values remain; use Restore changes to restore eligible stats.')
    pause_button = ttk.Button(hero_actions, text='Pause', command=pause_holds)
    pause_button.pack(side='left')
    player_widgets.append(pause_button)
    def toggle_xp_assist():
        if player:
            player.xp_assist = xp_assist.get()
    ttk.Label(progression_tab, text='Max permanent progression', font=('Segoe UI', 14, 'bold')).pack(anchor='w', pady=(0, 12))
    ttk.Label(progression_tab, text='Creates and verifies an upgraded save from this exact game build, with an original backup.\n\n• All class gear, every rarity, upgraded\n• All catalog achievements\n• Maximum class talent experience\n• Account level 30\n\nExit the game before applying. Your character identities and other save records are kept. Individual talent allocations are not changed.',
              wraplength=590).pack(anchor='w')
    progression_status = tk.StringVar(value='Backups and upgraded saves are kept in the trainer folder.')
    ttk.Label(progression_tab, textvariable=progression_status, wraplength=590, foreground='#a9b8c8').pack(anchor='w', pady=15)

    def max_account():
        from save_editor import prepare_upgrade, apply_upgrade
        from datetime import datetime
        path = saved.get('game_exe')
        if not path:
            status.set('Locate the Reborn game executable first.')
            return
        try:
            if resolve(path).digest != BUILD:
                raise RuntimeError('Live controls support this layout, but permanent save upgrades require a separately verified save format. No save changed.')
            folder = ROOT / 'backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            result = prepare_upgrade(path, folder)
            if find_games(path):
                progression_status.set(f"Upgrade prepared: {result['path']}\nExit Reborn, then press Max progression again to apply a fresh upgrade.")
            else:
                apply_upgrade(path, result['path'], result['backup'])
                progression_status.set(f"Applied. Start Reborn to load the upgrade.\nOriginal backup: {result['backup']}")
        except Exception as exc:
            progression_status.set(str(exc))
            error(exc)

    ttk.Button(progression_tab, text='Max progression', command=max_account).pack(anchor='w')
    def save_backup():
        from save_editor import backup_save
        from datetime import datetime
        path = saved.get('game_exe')
        if not path:
            status.set('Locate the Reborn game executable first.')
            return
        try:
            folder = ROOT / 'backups' / ('save-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            target = backup_save(path, folder)
            status.set(f'Save backup verified: {target}')
            progression_status.set(f'Save and on-disk talent settings backed up: {folder}')
        except Exception as exc:
            error(exc)

    ttk.Button(progression_tab, text='Save game backup', command=save_backup).pack(anchor='w', pady=10)
    ttk.Label(progression_tab, text='Backs up the latest authenticated disk save and on-disk talent settings. Reborn saves progression automatically. This does not force a running fight to save or upload to the cloud.', wraplength=590).pack(anchor='w')
    actions = ttk.Frame(body)
    actions.pack(fill='x')

    def restore():
        nonlocal hero_generation, hero_searching
        hero_generation += 1
        hero_searching = False
        if trainer:
            try:
                if player:
                    player.stop()
                    xp_assist.set(False)
                    hold_all.set(False)
                    for held in player_holds.values():
                        held.set(False)
                trainer.restore()
                refresh_values()
                status.set('Restored native options and unchanged same-level player stats.')
            except Exception as exc:
                error(exc)

    restore_button = ttk.Button(hero_actions, text='Restore changes', command=restore)
    restore_button.pack(side='left', padx=6)
    widgets.append(restore_button)

    def disconnect_game():
        nonlocal trainer, player, hero_generation, hero_searching, connection_generation, connecting
        connection_generation += 1
        connecting = False
        hero_generation += 1
        hero_searching = False
        if trainer:
            try:
                if player:
                    player.stop()
                trainer.restore()
            except (RuntimeError, OSError, psutil.Error) as exc:
                logging.warning('Restore before disconnect: %s', exc)
            finally:
                trainer.close()
        trainer = None
        player = None
        for held in player_holds.values():
            held.set(False)
        for value in player_current.values():
            value.set('—')
        xp_assist.set(False)
        hold_all.set(False)
        hero_status.set('No solo hero connected. Start a run and use Find hero.')
        retry.reset()

    def choose_executable():
        path = filedialog.askopenfilename(title='Choose Azeroth Survivors Reborn executable',
                                         initialfile=PROCESS_NAME, filetypes=[('Reborn game', PROCESS_NAME)])
        if not path:
            return None
        exe = Path(path).resolve()
        if not exe.is_file() or exe.name.casefold() != PROCESS_NAME.casefold():
            raise ValueError(f'Choose the installed {PROCESS_NAME} executable.')
        return str(exe)

    def locate():
        nonlocal next_process_scan
        try:
            path = choose_executable()
            if path:
                disconnect_game()
                launcher.session = None
                saved['game_exe'] = path
                atomic_json(SETTINGS, saved)
                next_process_scan = 0
                status.set('Game executable saved. Use Run Azeroth Survivors Reborn to launch it.')
        except Exception as exc:
            error(exc)

    def run_game():
        nonlocal next_process_scan
        try:
            launcher.refresh(saved.get('game_exe'))
            if launcher.running:
                raise RuntimeError('Reborn is already running. Use the detected process below.')
            path = saved.get('game_exe')
            if not path or not Path(path).is_file():
                path = choose_executable()
                if not path:
                    return
            saved['game_exe'] = path
            atomic_json(SETTINGS, saved)
            disconnect_game()
            session = launcher.launch(path)
            next_process_scan = 0
            run_button.configure(state='disabled')
            status.set(f'Launched Reborn · PID {session.pid} · waiting for game initialization…')
        except Exception as exc:
            error(exc)

    def stop_game():
        nonlocal next_process_scan
        try:
            session = launcher.session
            if not session:
                raise RuntimeError('No single Reborn process is selected. Choose its executable to disambiguate.')
            session.process()
            if not messagebox.askyesno('Stop Azeroth Survivors Reborn',
                                      f'Stop Reborn PID {session.pid}? Unsaved run progress may be lost.\n\n{session.path}'):
                return
            # The dialog may stay open while the game exits or restarts: revalidate before acting.
            session.process()
            disconnect_game()
            session.stop()
            next_process_scan = 0
            stop_button.configure(state='disabled')
            status.set(f'Stop requested for Reborn PID {session.pid}.')
        except Exception as exc:
            next_process_scan = 0
            error(exc)

    run_button = ttk.Button(actions, text='Run Azeroth Survivors Reborn', command=run_game, style='Accent.TButton')
    run_button.pack(side='left', padx=(0, 8))
    stop_button = ttk.Button(actions, text='Stop game', command=stop_game, state='disabled')
    stop_button.pack(side='left', padx=(0, 8))
    ttk.Button(actions, text='Game executable…', command=locate).pack(side='left')
    process_status = tk.StringVar(value='Checking game processes…')
    ttk.Label(actions, textvariable=process_status, foreground='#a9b8c8', wraplength=230).pack(side='left', padx=12)
    ttk.Button(actions, text='Open folder', command=lambda: os.startfile(ROOT)).pack(side='right')
    ttk.Label(body, text='Restore does not undo one-time actions or permanent save upgrades.',
              foreground='#a9b8c8', wraplength=600).pack(anchor='w', pady=(12, 0))

    update_results = queue.Queue()
    update_busy = False
    latest_release = None
    update_status = tk.StringVar(value='Updates from GitHub')
    automatic = tk.BooleanVar(value=saved.get('check_updates', True))
    def save_update_preference():
        saved['check_updates'] = automatic.get()
        atomic_json(SETTINGS, saved)
    ttk.Label(updates_tab, text='Trainer updates', font=('Segoe UI', 20, 'bold')).pack(anchor='w', pady=(0, 16))
    ttk.Label(updates_tab, text=f'Installed version: {VERSION}', font=('Segoe UI', 12)).pack(anchor='w')
    ttk.Label(updates_tab, textvariable=update_status, wraplength=850).pack(anchor='w', pady=16)
    ttk.Label(updates_tab, text='Checks stable releases from nelbin4/Azeroth-Survivors-Reborn-Trainer. Downloads are verified with GitHub SHA-256. Installation runs only when you choose it. The trainer restores eligible live changes and restarts; the previous executable is kept beside the app.', wraplength=800, foreground='#a9b8c8').pack(anchor='w', pady=(0, 20))
    def check_updates():
        nonlocal update_busy
        if update_busy:
            return
        update_busy = True
        update_status.set('Checking GitHub…')
        def work():
            try:
                result = updater.check(VERSION)
            except Exception as exc:
                result = exc
            update_results.put(('check', result))
        threading.Thread(target=work, daemon=True).start()
    def install_update():
        nonlocal update_busy
        if update_busy or not latest_release or not latest_release.get('available'):
            return
        if not getattr(sys, 'frozen', False):
            webbrowser.open(updater.RELEASES)
            update_status.set('Source installation: download the standalone executable from the release page.')
            return
        if not latest_release.get('asset'):
            update_status.set('No verified executable is available. Open the release page.')
            return
        if not messagebox.askyesno('Install trainer update', f"Download {latest_release['tag']} from GitHub, verify it, and restart this trainer? Settings and save backups stay in place."):
            return
        update_busy = True
        update_status.set('Downloading and verifying update…')
        release = latest_release
        def work():
            try:
                result = updater.download(release, ROOT / '.updates')
            except Exception as exc:
                result = exc
            update_results.put(('download', result))
        threading.Thread(target=work, daemon=True).start()
    update_buttons = ttk.Frame(updates_tab)
    update_buttons.pack(anchor='w')
    ttk.Button(update_buttons, text='Check updates', command=check_updates).pack(side='left', padx=(0, 8))
    install_button = ttk.Button(update_buttons, text='Install update & restart', command=install_update, style='Accent.TButton', state='disabled')
    install_button.pack(side='left', padx=8)
    ttk.Button(update_buttons, text='Release notes', command=lambda: webbrowser.open(latest_release['page'] if latest_release else updater.RELEASES)).pack(side='left', padx=8)
    ttk.Checkbutton(updates_tab, text='Automatically check GitHub on startup and every 6 hours', variable=automatic, command=save_update_preference).pack(anchor='w', pady=22)
    footer = ttk.Frame(body, padding=(0, 10, 0, 0))
    footer.pack(fill='x')
    ttk.Label(footer, textvariable=update_status, foreground='#76dfc1', wraplength=370).pack(side='left')
    ttk.Button(footer, text='Check updates', command=check_updates).pack(side='left', padx=12)
    ttk.Button(footer, text='Release page', command=lambda: webbrowser.open(updater.RELEASES)).pack(side='left')
    ttk.Checkbutton(footer, text='Automatic checks', variable=automatic, command=save_update_preference).pack(side='right')
    def scheduled_update_check():
        if automatic.get():
            check_updates()
        if not closing:
            root.after(6 * 60 * 60 * 1000, scheduled_update_check)
    root.after(1500, scheduled_update_check)

    def connect_worker(session, generation):
        try:
            result = Trainer(session.path, session=session)
            if closing:
                result.close()
            else:
                pending.put((generation, result))
        except Exception as exc:
            pending.put((generation, exc))

    def poll():
        nonlocal trainer, connecting, player, hero_searching, hero_generation, update_busy, latest_release, next_process_scan
        now = time.monotonic()
        if now >= next_process_scan:
            try:
                launcher.refresh(saved.get('game_exe'))
            except (OSError, psutil.Error) as exc:
                launcher.session = None
                launcher.sessions = []
                launcher.unreadable = True
                logging.warning('Process detection failed: %s', exc)
            next_process_scan = now + 2
        run_button.configure(state='disabled' if launcher.running else 'normal')
        stop_button.configure(state='normal' if launcher.session else 'disabled')
        if launcher.session:
            extra = f' · {len(launcher.sessions)} instances' if len(launcher.sessions) > 1 else ''
            process_status.set(f'Running · PID {launcher.session.pid}{extra}')
        elif launcher.unreadable:
            process_status.set('Running process inaccessible')
        elif launcher.sessions:
            process_status.set('Multiple games · choose executable')
        else:
            process_status.set('Game is not running')
        try:
            kind, result = update_results.get_nowait()
            update_busy = False
            if isinstance(result, Exception):
                logging.warning('Update %s failed: %s', kind, result)
                update_status.set('Update check/download failed. Use Check updates to retry.')
            elif kind == 'check':
                latest_release = result
                update_status.set(f"Update available: {result['tag']}" if result['available'] else f'Up to date · v{VERSION}')
            else:
                try:
                    updater.launch_install(result)
                    close()
                    return
                except Exception as exc:
                    update_status.set(str(exc))
            install_button.configure(state='normal' if latest_release and latest_release.get('available') and latest_release.get('asset') and not update_busy else 'disabled')
        except queue.Empty:
            pass
        try:
            generation, result = pending.get_nowait()
            if generation != connection_generation:
                if isinstance(result, Trainer):
                    result.close()
            elif isinstance(result, Exception):
                connecting = False
                retry.failed(time.monotonic())
                if 'Memory controls are blocked' in str(result):
                    logging.warning('Full layout discovery: %s',result)
                    status.set('Full layout unavailable for this build. Open Hunt values for automatic discovery and verified stat holds.')
                else:
                    status.set(str(result))
            else:
                connecting = False
                selected = launcher.session
                if (not selected or selected.pid != result.process.pid or selected.created != result.created
                        or not same_path(selected.path, result.path)):
                    result.close()
                    retry.reset()
                else:
                    trainer = result
                    retry.reset()
                    saved['game_exe'] = trainer.path
                    atomic_json(SETTINGS, saved)
                    status.set(f'Connected · PID {trainer.process.pid} · layout validated · {trainer.build[:12]}'
                               if trainer.player_supported else 'Run options verified. Automatic Hunt is verifying and freezing player stats.')
                    if not trainer.player_supported:
                        hero_status.set('Player stats are managed by Automatic Hunt. Open Hunt values to view frozen fields.')
                    detail.set(trainer.path)
                    try:
                        run_defaults.apply(trainer)
                    except Exception as exc:
                        error(exc)
                    refresh_values()
                    if trainer.player_supported:
                        root.after(100, find_hero)
        except queue.Empty:
            pass
        try:
            generation, identity, result = hero_pending.get_nowait()
            if generation == hero_generation:
                hero_searching = False
                if trainer and same_session(identity, trainer.identity()):
                    if isinstance(result, Exception):
                        hero_status.set(str(result))
                        status.set('Native options ready. Start a solo run, then Find hero.')
                    else:
                        try:
                            candidate = Player(trainer)
                            candidate.lease = result
                            snapshot = candidate.snapshot()
                            player = candidate
                            show_hero(snapshot)
                            status.set('Solo hero verified. Live controls are ready.')
                            maximize_player()
                        except Exception as exc:
                            hero_status.set(str(exc))
                            error(exc)
        except queue.Empty:
            pass
        if trainer:
            try:
                trainer.guard()
            except Exception as exc:
                trainer.close()
                trainer = None
                player = None
                hero_generation += 1
                hero_searching = False
                for held in player_holds.values():
                    held.set(False)
                xp_assist.set(False)
                hold_all.set(False)
                hero_status.set('Disconnected. Start the game to reconnect.')
                for value in player_current.values():
                    value.set('—')
                retry.failed(time.monotonic())
                status.set(str(exc))
        for widget in widgets:
            widget.configure(state='normal' if trainer else 'disabled')
        native_only = trainer is not None and not trainer.player_supported
        find_button.configure(state='normal' if trainer and not hero_searching else 'disabled')
        find_button.configure(text='View Hunt stats' if native_only else 'Finding Hero…' if hero_searching else 'Find Hero',
                              command=(lambda: tabs.select(hunt_tab)) if native_only else find_hero)
        if player and player.lease:
            try:
                show_hero(player.tick())
            except Exception as exc:
                player.frozen.clear()
                player.xp_assist = False
                xp_assist.set(False)
                hold_all.set(False)
                player.lease = None
                for held in player_holds.values():
                    held.set(False)
                hero_status.set(str(exc))
                for value in player_current.values():
                    value.set('—')
                status.set('Player controls stopped. Find hero again after starting a solo run.')
        for widget in player_widgets:
            widget.configure(state='normal' if player and player.lease else 'disabled')
        if not trainer and not connecting and launcher.session and retry.ready(time.monotonic()):
            connecting = True
            threading.Thread(target=connect_worker, args=(launcher.session, connection_generation), daemon=True).start()
        if not closing:
            root.after(500, poll)

    def close():
        nonlocal closing, hero_generation
        closing = True
        hero_generation += 1
        if trainer:
            try:
                if player:
                    player.stop()
                trainer.restore()
            except (RuntimeError, OSError, psutil.Error) as exc:
                logging.warning('Restore on close: %s', exc)
            trainer.close()
        while not pending.empty():
            _, result = pending.get_nowait()
            if isinstance(result, Trainer):
                result.close()
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)
    poll()
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument('command', nargs='?', default='gui', choices=['gui', 'status', 'set', 'restore'])
    parser.add_argument('--game', help='Full path to AzerothSurvivorsGame.exe')
    parser.add_argument('--feature', choices=FEATURES)
    parser.add_argument('--value', type=float)
    parser.add_argument('--max-player', action='store_true', help='Compatibility flag: Find Hero now always enables Max all stats.')
    args = parser.parse_args()
    logging.basicConfig(filename=ROOT / 'reborn-trainer.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'gui':
        run_gui()
        return
    trainer = Trainer(args.game, writable=args.command != 'status')
    try:
        if args.command == 'set':
            if args.feature is None or args.value is None:
                parser.error('set requires --feature and --value')
            trainer.set(args.feature, args.value)
        elif args.command == 'restore':
            trainer.restore()
        print(json.dumps(dict(app=APP_NAME, version=VERSION, pid=trainer.process.pid,
                              game=trainer.path, build=trainer.build, values=trainer.values()), indent=2))
    finally:
        trainer.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logging.exception('Trainer failed')
        if len(sys.argv) == 1 or 'gui' in sys.argv[1:]:
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, str(exc))
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
