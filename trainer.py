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

import psutil
from player import Player, STATS
from compatibility import resolve

APP_NAME = 'Azeroth Survivor Reborn Trainer'
VERSION = '0.4.1'
PROCESS_NAME = 'AzerothSurvivorsGame.exe'
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
    def __init__(self, path=None, writable=True, journal=JOURNAL):
        self.path = path
        self.memory = None
        self.process = None
        self.journal = journal
        self.changes = {}
        self.lock = None
        games = find_games(path)
        if not games:
            raise RuntimeError('Waiting for Azeroth Survivors: Reborn.')
        if len(games) != 1:
            raise RuntimeError('Multiple Reborn games found. Select the exact game path.')
        self.process = games[0]
        self.created = self.process.create_time()
        self.path = self.process.exe()
        self.profile = resolve(self.path)
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


def run_gui(max_player=False):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry('700x1000')
    root.minsize(700, 950)
    root.configure(bg='#14202c')
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('TFrame', background='#14202c')
    style.configure('TLabel', background='#14202c', foreground='#e5e9ef')
    style.configure('TCheckbutton', background='#14202c', foreground='#e5e9ef')
    style.map('TCheckbutton', background=[('active', '#203448')])
    body = ttk.Frame(root, padding=22)
    body.pack(fill='both', expand=True)
    ttk.Label(body, text='AZEROTH SURVIVOR REBORN', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
    ttk.Label(body, text=f'TRAINER  v{VERSION}   ·   Solo / host use', foreground='#a9b8c8').pack(anchor='w', pady=(3, 14))
    status = tk.StringVar(value='Searching for the running game…')
    detail = tk.StringVar(value='')
    ttk.Label(body, textvariable=status, font=('Segoe UI', 11, 'bold')).pack(anchor='w')
    ttk.Label(body, textvariable=detail, wraplength=600, foreground='#a9b8c8').pack(anchor='w', pady=(4, 12))
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
    native_tab = ttk.Frame(tabs, padding=10)
    progression_tab = ttk.Frame(tabs, padding=12)
    tabs.add(player_shell, text='Player stats')
    tabs.add(native_tab, text='Native options')
    tabs.add(progression_tab, text='Permanent progression')
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
    connecting = False
    closing = False
    try:
        saved = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}

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
            variable = tk.BooleanVar(value=False)
            widget = ttk.Checkbutton(row, text=feature.label, variable=variable, command=lambda k=key: toggle(k))
            widget.pack(side='left')
        else:
            variable = tk.StringVar(value='1' if key == 'timescale' else '0')
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

    ttk.Label(native_tab, text='Menu / next-run options take effect when the game reads them.\nThe native unlock-all flag alone is unverified; use Permanent progression for save upgrades.',
              wraplength=600, foreground='#a9b8c8').pack(anchor='w', pady=(14, 12))
    hero_status = tk.StringVar(value='Start a solo run and click Find hero.')
    hero_row = ttk.Frame(player_tab)
    hero_row.pack(fill='x', pady=(0, 10))
    ttk.Label(hero_row, textvariable=hero_status, wraplength=470).pack(side='left')

    def find_hero():
        nonlocal player
        if not trainer:
            return
        try:
            if player:
                player.stop()
            player = Player(trainer)
            snapshot = player.connect()
            show_hero(snapshot)
            status.set('Solo hero verified. Player controls are ready.')
            if max_player:
                maximize_player()
        except Exception as exc:
            hero_status.set(str(exc))
            error(exc)

    find_button = ttk.Button(hero_row, text='Find hero', command=find_hero)
    find_button.pack(side='right')
    widgets.append(find_button)

    def show_hero(snapshot):
        hero_status.set(f"{snapshot['name']} · Level {snapshot['level']} · HP {snapshot['hp']}/{snapshot['maximum']}\nGold {snapshot['gold']} · XP {snapshot['xp']}/{snapshot['next_xp']}")
        for key, value in snapshot['values'].items():
            player_current[key].set(str(value))

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

    for key, stat in STATS.items():
        row = ttk.Frame(player_tab)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text=stat.label, width=33).pack(side='left')
        current = tk.StringVar(value='—')
        player_current[key] = current
        ttk.Label(row, textvariable=current, width=7, anchor='e').pack(side='left', padx=5)
        target = tk.StringVar(value=str(stat.default))
        player_targets[key] = target
        entry = ttk.Entry(row, textvariable=target, width=7)
        entry.pack(side='left', padx=4)
        hold = tk.BooleanVar(value=False)
        player_holds[key] = hold
        check = ttk.Checkbutton(row, text='Hold', variable=hold, command=lambda k=key: apply_stat(k, True))
        check.pack(side='right')
        button = ttk.Button(row, text='Set', width=5, command=lambda k=key: apply_stat(k))
        button.pack(side='right', padx=3)
        player_widgets.extend([entry, check, button])

    quick = ttk.Frame(player_tab)
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
    gold_entry.pack(side='left')
    gold_button = ttk.Button(quick, text='Set run gold', command=lambda: player_action('gold'))
    gold_button.pack(side='left', padx=5)
    heal_button = ttk.Button(quick, text='Heal to full', command=lambda: player_action('heal'))
    heal_button.pack(side='left', padx=5)
    xp_button = ttk.Button(quick, text='One XP from level-up', command=lambda: player_action('xp'))
    xp_button.pack(side='right')
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

    max_button = ttk.Button(player_tab, text='Max all stats + God mode', command=maximize_player)
    max_button.pack(anchor='w', pady=5)
    player_widgets.append(max_button)
    xp_assist = tk.BooleanVar(value=False)
    def toggle_xp_assist():
        if player:
            player.xp_assist = xp_assist.get()
    xp_check = ttk.Checkbutton(player_tab, text='XP assistance: stay one pickup from the next level', variable=xp_assist, command=toggle_xp_assist)
    xp_check.pack(anchor='w', pady=3)
    player_widgets.append(xp_check)
    ttk.Label(player_tab, text='Set applies once; Hold reapplies after recalculation. Uncheck Hold to restore an unchanged value from the same level. New runs require Find hero. Gold, healing and XP are one-time actions.',
              wraplength=600, foreground='#a9b8c8').pack(anchor='w', pady=8)

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
        if trainer:
            try:
                if player:
                    player.stop()
                    xp_assist.set(False)
                    for held in player_holds.values():
                        held.set(False)
                trainer.restore()
                refresh_values()
                status.set('Restored native options and unchanged same-level player stats.')
            except Exception as exc:
                error(exc)

    restore_button = ttk.Button(actions, text='Restore changes', command=restore)
    restore_button.pack(side='left')
    widgets.append(restore_button)

    def locate():
        nonlocal trainer, player
        path = filedialog.askopenfilename(title='Locate AzerothSurvivorsGame.exe', filetypes=[('Reborn game', PROCESS_NAME)])
        if path:
            if trainer:
                try:
                    if player:
                        player.stop()
                    trainer.restore()
                except Exception as exc:
                    error(exc)
                    return
                trainer.close()
                trainer = None
                player = None
            saved['game_exe'] = path
            atomic_json(SETTINGS, saved)

    ttk.Button(actions, text='Locate game', command=locate).pack(side='left', padx=8)
    ttk.Button(actions, text='Open folder', command=lambda: os.startfile(ROOT)).pack(side='right')
    ttk.Label(body, text='Restore does not undo one-time actions or permanent save upgrades.',
              foreground='#a9b8c8', wraplength=600).pack(anchor='w', pady=(12, 0))

    def connect_worker(path):
        try:
            pending.put(Trainer(path))
        except Exception as exc:
            pending.put(exc)

    def poll():
        nonlocal trainer, connecting, player
        try:
            result = pending.get_nowait()
            connecting = False
            if isinstance(result, Exception):
                status.set(str(result))
            else:
                trainer = result
                saved['game_exe'] = trainer.path
                atomic_json(SETTINGS, saved)
                status.set(f'Connected · PID {trainer.process.pid} · layout validated · {trainer.build[:12]}')
                detail.set(trainer.path)
                refresh_values()
                root.after(100, find_hero)
        except queue.Empty:
            pass
        if trainer:
            try:
                trainer.guard()
            except Exception as exc:
                trainer.close()
                trainer = None
                player = None
                status.set(str(exc))
        for widget in widgets:
            widget.configure(state='normal' if trainer else 'disabled')
        if player and player.lease:
            try:
                show_hero(player.tick())
            except Exception as exc:
                player.frozen.clear()
                player.xp_assist = False
                xp_assist.set(False)
                player.lease = None
                for held in player_holds.values():
                    held.set(False)
                hero_status.set(str(exc))
                status.set('Player controls stopped. Find hero again after starting a solo run.')
        for widget in player_widgets:
            widget.configure(state='normal' if player and player.lease else 'disabled')
        if not trainer and not connecting:
            connecting = True
            threading.Thread(target=connect_worker, args=(saved.get('game_exe'),), daemon=True).start()
        if not closing:
            root.after(500, poll)

    def close():
        nonlocal closing
        if trainer:
            try:
                if player:
                    player.stop()
                trainer.restore()
            except (RuntimeError, OSError, psutil.Error) as exc:
                logging.warning('Restore on close: %s', exc)
            trainer.close()
        closing = True
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
    parser.add_argument('--max-player', action='store_true', help='Find a solo hero and enable Max all stats in the GUI.')
    args = parser.parse_args()
    logging.basicConfig(filename=ROOT / 'reborn-trainer.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'gui':
        run_gui(max_player=args.max_player)
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
