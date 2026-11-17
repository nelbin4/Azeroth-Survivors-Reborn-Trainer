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
VERSION = '0.9.7'
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
        self.requested_options = {}
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

    def request_option(self, key, value):
        # Keep the validated request even if the game was between states.
        with self.write_gate:
            FEATURES[key].encode(float(value) * 60 if key == 'clock' else value)
            self.requested_options[key] = value
            self._set(key, value, rebase=True)

    def verify_run_options(self, cancelled):
        profile = resolve_native(self.path)
        with self.write_gate:
            if cancelled():
                raise InterruptedError('Run options check stopped.')
            if (profile.digest, profile.config) != (self.build, self.config_rva):
                raise RuntimeError('Run options layout changed. Reconnect to Reborn.')
            for rva, expected in profile.signatures.items():
                if self.memory.read(self.base + rva, len(expected)) != expected:
                    raise RuntimeError('Run options instructions do not match the running game.')
            failures = []
            for key, value in self.requested_options.items():
                if cancelled():
                    raise InterruptedError('Run options check stopped.')
                try:
                    self._set(key, value, rebase=True)
                except (ValueError, RuntimeError, OSError) as exc:
                    failures.append(str(exc))
            return self.values(), failures

    def _set(self, key, value, *, rebase=False):
        feature = FEATURES[key]
        if key == 'clock':
            value = float(value) * 60
        after = feature.encode(value)
        config = self.guard()
        before = config[feature.offset:feature.offset + len(after)]
        if before == after:
            return
        if key in self.changes and before.hex() != self.changes[key]['after']:
            if not rebase:
                raise RuntimeError('This option was changed outside the trainer. Restore changes and retry.')
            # An explicit UI request can reapply a reset option. Restore will
            # return it to the most recent value the game actually supplied.
            self.changes[key] = dict(before=before.hex(), after=before.hex())
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
        if hasattr(self, 'requested_options'):
            self.requested_options.clear()
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
    style.configure('Find.TButton', background='#56d6b0', foreground='#10251f',
                    font=('Segoe UI', 16, 'bold'), padding=(24, 12))
    style.map('Find.TButton', background=[('active', '#7ce5c6'), ('disabled', '#263c3d')],
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
    ttk.Label(header, text=f'v{VERSION}', foreground='#a9b8c8').pack(side='left', padx=16)
    status = tk.StringVar(value='Waiting for Azeroth Survivors: Reborn.')
    detail = tk.StringVar(value='')
    ttk.Label(body, textvariable=status, font=('Segoe UI', 10), foreground='#76dfc1', wraplength=1030).pack(anchor='w', pady=(0, 6))
    # Full executable path remains in settings; avoid a second empty status row.
    tabs = ttk.Notebook(body)
    tabs.pack(fill='both', expand=True)
    stats_tab = ttk.Frame(tabs)
    native_tab = ttk.Frame(tabs, padding=10)
    progression_tab = ttk.Frame(tabs, padding=12)
    tabs.add(stats_tab, text='Stats')
    tabs.add(native_tab, text='Run options')
    tabs.add(progression_tab, text='Saves')
    options = ttk.Frame(native_tab)
    options.pack(fill='x')
    variables = {}
    widgets = []
    trainer = None
    pending = queue.Queue()
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

    def error(exc):
        logging.exception('Trainer operation failed')
        status.set(str(exc))

    def toggle(key):
        if not trainer:
            return
        try:
            trainer.request_option(key, variables[key].get())
            options_status.set(f'{FEATURES[key].label} written and read back.')
            status.set(f'{FEATURES[key].label}: {"on" if variables[key].get() else "off"}')
        except Exception as exc:
            error(exc)
            options_status.set(str(exc))
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
                trainer.request_option(key, variables[key].get())
                options_status.set(f'{FEATURES[key].label} written and read back.')
                status.set(f'{FEATURES[key].label} applied and read back.')
            except Exception as exc:
                error(exc)
                options_status.set(str(exc))
                refresh_values()

    def refresh_values():
        if trainer:
            try:
                for key, value in trainer.values().items():
                    variables[key].set(bool(value) if FEATURES[key].fmt == 'B' else f'{value:g}')
            except Exception as exc:
                error(exc)

    options_status = tk.StringVar(value='Find checks Run options after player stats.')
    ttk.Label(native_tab, textvariable=options_status, wraplength=850,
              foreground='#76dfc1').pack(anchor='w', pady=(12, 0))

    def report_options(result):
        if result is None:
            options_status.set('Queued after player stats…')
            return
        if isinstance(result, Exception):
            options_status.set(f'Run options could not be verified: {result}')
            return
        _, failures = result
        # Leave text-entry drafts alone. Checkbox and numeric edits already
        # show the user's requested values, and failures are listed here.
        if failures:
            options_status.set('Some Run options could not be applied: ' + '; '.join(failures))
        else:
            options_status.set('Run options verified in game memory. Next-run settings apply when a new run starts.')

    ttk.Label(native_tab, text='Menu / next-run options take effect when the game reads them.\nThe native unlock-all flag alone is unverified; use Saves for progression upgrades.',
              wraplength=600, foreground='#a9b8c8').pack(anchor='w', pady=(14, 12))
    from stats_panel import StatsPanel
    stats_panel = StatsPanel(root, stats_tab, lambda: launcher.session, lambda: trainer, lambda: closing, report_options)

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
                raise RuntimeError('Stat discovery supports this layout, but permanent save upgrades require a separately verified save format. No save changed.')
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
        stats_panel.controller.options_pending = False
        stats_panel.controller.policy.paused = True
        stats_panel.controller.changed()
        if trainer:
            try:
                trainer.restore()
                refresh_values()
                options_status.set('Run options restored.')
                status.set('Native options restored. Stat freezes paused; current stat values remain.')
            except Exception as exc:
                error(exc)

    restore_button = ttk.Button(actions, text='Restore native options', command=restore)
    restore_button.pack(side='left', padx=(0, 6))
    widgets.append(restore_button)

    def disconnect_game():
        nonlocal trainer, connection_generation, connecting
        connection_generation += 1
        connecting = False
        stats_panel.controller.invalidate()
        if trainer:
            try:
                trainer.restore()
            except (RuntimeError, OSError, psutil.Error) as exc:
                logging.warning('Restore before disconnect: %s', exc)
            finally:
                trainer.close()
        trainer = None
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
    update_results = queue.Queue()
    update_busy = False
    latest_release = None
    update_status = tk.StringVar(value='Updates from GitHub')
    def check_updates():
        nonlocal update_busy
        if update_busy:
            return
        update_busy = True
        update_button.state(['disabled'])
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
            webbrowser.open(latest_release['page'])
            update_status.set('This release has no verified executable for automatic installation.')
            return
        update_busy = True
        update_button.state(['disabled'])
        update_status.set('Downloading and verifying update…')
        release = latest_release
        def work():
            try:
                result = updater.download(release, ROOT / '.updates')
            except Exception as exc:
                result = exc
            update_results.put(('download', result))
        threading.Thread(target=work, daemon=True).start()
    def update_action():
        if latest_release and latest_release['available']:
            install_update()
        else:
            check_updates()

    footer = ttk.Frame(body, padding=(0, 10, 0, 0))
    footer.pack(fill='x')
    update_button = ttk.Button(footer, text='Check updates', command=update_action)
    update_button.pack(side='left', padx=(0, 8))
    ttk.Button(footer, text='Github Page', command=lambda: webbrowser.open(updater.PROJECT)).pack(side='left')
    ttk.Label(footer, textvariable=update_status, foreground='#76dfc1', wraplength=540).pack(side='left', padx=12)
    root.after_idle(check_updates)

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
        nonlocal trainer, connecting, update_busy, latest_release, next_process_scan
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
                if kind == 'check':
                    latest_release = None
                update_status.set('Update failed. Try again.')
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
            update_button.configure(text='Update' if latest_release and latest_release['available'] else 'Check updates')
            update_button.state(['!disabled'])
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
                    status.set('Could not verify this game build. Find is unavailable until connected.')
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
                    status.set(f'Connected to PID {trainer.process.pid}. Run options ready; use Find in Stats to start Hunt.')
                    detail.set(trainer.path)
                    try:
                        trainer.requested_options.update(RUN_OPTION_DEFAULTS)
                        run_defaults.apply(trainer)
                    except Exception as exc:
                        error(exc)
                    refresh_values()
        except queue.Empty:
            pass
        if trainer:
            try:
                if not stats_panel.controller.connected_session():
                    raise RuntimeError('Selected game process changed.')
                trainer.guard()
            except Exception as exc:
                trainer.close()
                trainer = None
                retry.failed(time.monotonic())
                status.set(str(exc))
        stats_panel.refresh_connection()
        for widget in widgets:
            widget.configure(state='normal' if trainer else 'disabled')
        if not trainer and not connecting and launcher.session and retry.ready(time.monotonic()):
            connecting = True
            threading.Thread(target=connect_worker, args=(launcher.session, connection_generation), daemon=True).start()
        if not closing:
            root.after(500, poll)

    def close():
        nonlocal closing
        closing = True
        stats_panel.controller.close()
        if trainer:
            try:
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
    parser.add_argument('--max-player', action='store_true', help='Compatibility flag retained; use Find and auto max/freeze in Stats.')
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
