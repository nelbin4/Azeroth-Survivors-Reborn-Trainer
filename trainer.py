"""Azeroth Survivor Reborn Trainer. Copyright (c) 2026 nelbin4. MIT license."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import queue
import time
from runtime import RetrySchedule
from game_process import GameLauncher, PROCESS_NAME, same_path
import updater
import webbrowser

import psutil
from compatibility import resolve, Layout

APP_NAME = 'Azeroth Survivor Reborn Trainer'
VERSION = '0.9.16'
ROOT = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
SETTINGS = ROOT / 'reborn-settings.json'


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
    def __init__(self, path=None, writable=True, session=None):
        self.write_gate = threading.RLock()
        self.path = path
        self.memory = None
        self.process = None
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
        except RuntimeError as exc:
            if not str(exc).startswith('Unsupported Reborn build'):
                raise
            # Find verifies newer layouts independently of legacy detection.
            self.profile = None
        self.player_supported = isinstance(self.profile, Layout)
        if self.player_supported:
            self.build = self.profile.digest
            self.signatures = self.profile.signatures
        else:
            with Path(self.path).open('rb') as image:
                self.build = hashlib.file_digest(image, 'sha256').hexdigest()
            self.signatures = {}
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
        except Exception:
            if self.memory:
                self.memory.close()
            if self.lock:
                self.lock.close()
            raise

    def guard(self):
        if not self.memory or not self.process.is_running() or self.process.create_time() != self.created:
            raise RuntimeError('Game connection lost. Reconnect before using Find.')
        if not same_path(self.process.exe(), self.path):
            raise RuntimeError('Game process path changed.')
        for rva, expected in self.signatures.items():
            if self.memory.read(self.base + rva, len(expected)) != expected:
                raise RuntimeError('Game instruction mismatch. Reconnect before using Find.')

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
    logo_path = Path(__file__).resolve().parent / 'assets' / 'trainer-logo.png'
    logo = tk.PhotoImage(master=root, file=str(logo_path))
    root.iconphoto(True, logo)
    root.geometry('1010x810')
    root.resizable(False, False)
    root.configure(bg='#14202c')
    style = ttk.Style(root)
    style.theme_use('clam')
    root.option_add('*Font', ('Segoe UI', 11))
    style.configure('TFrame', background='#14202c')
    style.configure('TLabel', font=('Segoe UI', 11), background='#14202c', foreground='#e5e9ef')
    style.configure('TCheckbutton', background='#14202c', foreground='#e5e9ef')
    style.map('TCheckbutton', background=[('active', '#203448')])
    style.configure('TButton', padding=(12, 7), background='#24364b', foreground='#e5e9ef', borderwidth=0)
    style.map('TButton', background=[('active', '#35516c')], foreground=[('disabled', '#728399')])
    style.configure('Accent.TButton', background='#56d6b0', foreground='#10251f', font=('Segoe UI', 10, 'bold'))
    style.map('Accent.TButton', background=[('active', '#7ce5c6'), ('disabled', '#263c3d')])
    style.configure('Find.TButton', background='#56d6b0', foreground='#10251f',
                    font=('Segoe UI', 21, 'bold'), padding=(12, 15))
    style.map('Find.TButton', background=[('active', '#7ce5c6'), ('disabled', '#263c3d')],
              foreground=[('disabled', '#728399')])
    style.configure('Horizontal.TProgressbar', troughcolor='#223548',
                    background='#56d6b0', bordercolor='#223548', thickness=7)
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
    ttk.Label(header, image=logo).pack(side='left', padx=(0, 10))
    ttk.Label(header, text='AZEROTH SURVIVORS REBORN', foreground='#e3be69', font=('Segoe UI', 20, 'bold')).pack(side='left')
    status = tk.StringVar(value='Waiting for Azeroth Survivors: Reborn.')
    detail = tk.StringVar(value='')
    ttk.Label(body, textvariable=status, font=('Segoe UI', 10), foreground='#76dfc1', wraplength=1030).pack(anchor='w', pady=(0, 6))
    # Full executable path remains in settings; avoid a second empty status row.
    stats_area = ttk.Frame(body)
    stats_area.pack(fill='both', expand=True)
    trainer = None
    pending = queue.Queue()
    connection_generation = 0
    launcher = GameLauncher()
    next_process_scan = 0
    retry = RetrySchedule()
    connecting = False
    closing = False
    try:
        saved = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}

    if not isinstance(saved, dict):
        saved = {}

    def error(exc):
        logging.exception('Trainer operation failed')
        status.set(str(exc))

    def save_stat_preferences(preferences):
        saved['stats'] = preferences
        atomic_json(SETTINGS, saved)

    from stats_panel import StatsPanel
    stats_panel = StatsPanel(root, stats_area, lambda: launcher.session, lambda: trainer,
                             lambda: closing, saved.get('stats'), save_stat_preferences)

    footer = ttk.Frame(body, padding=(0, 8, 0, 0))
    footer.pack(fill='x')

    def disconnect_game():
        nonlocal trainer, connection_generation, connecting
        connection_generation += 1
        connecting = False
        stats_panel.controller.invalidate()
        if trainer:
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
                status.set('Game executable saved. Use Run Game to launch it.')
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

    run_button = ttk.Button(footer, text='Run Game', command=run_game, style='Accent.TButton')
    run_button.pack(side='left', fill='y', padx=(0, 8))
    stop_button = ttk.Button(footer, text='Stop Game', command=stop_game, state='disabled')
    stop_button.pack(side='left', fill='y', padx=(0, 8))
    ttk.Button(footer, text='Locate exe file', command=locate).pack(side='left', fill='y', padx=(0, 8))
    ttk.Button(footer, text='Advanced', command=stats_panel.open_advanced).pack(side='left', fill='y', padx=(0, 8))
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

    update_button = ttk.Button(footer, text='Check updates', command=update_action)
    update_button.pack(side='left', fill='y', padx=(0, 8))
    ttk.Button(footer, text='Github Page', command=lambda: webbrowser.open(updater.PROJECT)).pack(side='left', fill='y')
    ttk.Label(footer, textvariable=update_status, foreground='#76dfc1', wraplength=200).pack(side='right', padx=(8, 0))
    ttk.Label(footer, text=f'v{VERSION}', foreground='#a9b8c8').pack(side='right', padx=(8, 0))
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
        if not launcher.session and launcher.unreadable:
            status.set('Running process inaccessible.')
        elif not launcher.session and launcher.sessions:
            status.set('Multiple games running. Use Locate exe file.')
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
                update_status.set(f"Update available: {result['tag']}" if result['available'] else 'Up to date')
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
                    status.set(f'Connected to PID {trainer.process.pid}. Use Find in Stats to start Hunt.')
                    detail.set(trainer.path)
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
    parser.add_argument('command', nargs='?', default='gui', choices=['gui', 'status'])
    parser.add_argument('--game', help='Full path to AzerothSurvivorsGame.exe')
    parser.add_argument('--max-player', action='store_true', help='Compatibility flag retained; use Find and auto max/freeze in Stats.')
    args = parser.parse_args()
    logging.basicConfig(filename=ROOT / 'reborn-trainer.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'gui':
        run_gui()
        return
    trainer = Trainer(args.game, writable=False)
    try:
        print(json.dumps(dict(app=APP_NAME, version=VERSION, pid=trainer.process.pid,
                              game=trainer.path, build=trainer.build), indent=2))
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
