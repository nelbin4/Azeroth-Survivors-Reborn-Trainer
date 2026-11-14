"""Launch and select Reborn processes without depending on a supported memory layout."""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

import psutil

PROCESS_NAME = 'AzerothSurvivorsGame.exe'


def same_path(first, second):
    return os.path.normcase(str(Path(first).resolve())) == os.path.normcase(str(Path(second).resolve()))


@dataclass(frozen=True)
class GameSession:
    pid: int
    created: float
    path: str

    @classmethod
    def capture(cls, process):
        return cls(process.pid, process.create_time(), str(Path(process.exe()).resolve()))

    def process(self):
        process = psutil.Process(self.pid)
        if (not process.is_running() or process.create_time() != self.created
                or not same_path(process.exe(), self.path)):
            raise RuntimeError('Game process changed. Refresh detection before continuing.')
        return process

    def stop(self):
        # Use the validated psutil object; its terminate() also protects against PID reuse.
        self.process().terminate()


class GameLauncher:
    def __init__(self):
        self.session = None
        self.sessions = []
        self.unreadable = False

    @property
    def running(self):
        return bool(self.sessions) or self.unreadable

    def refresh(self, preferred_path=None):
        sessions = []
        self.unreadable = False
        for process in psutil.process_iter(['name']):
            try:
                if (process.info['name'] or '').casefold() != PROCESS_NAME.casefold():
                    continue
                sessions.append(GameSession.capture(process))
            except psutil.AccessDenied:
                # An inaccessible Reborn process still prevents a duplicate launch.
                self.unreadable = True
            except (psutil.NoSuchProcess, OSError, TypeError):
                continue
        self.sessions = sessions
        if self.session not in sessions:
            candidates = [s for s in sessions if not preferred_path or same_path(s.path, preferred_path)]
            if not candidates and len(sessions) == 1:
                candidates = sessions
            self.session = candidates[0] if len(candidates) == 1 else None
        return self.session

    def launch(self, path):
        self.refresh(path)
        if self.running:
            raise RuntimeError('Reborn is already running. Close it before launching another instance.')
        exe = Path(path).resolve()
        if exe.name.casefold() != PROCESS_NAME.casefold() or not exe.is_file():
            raise ValueError(f'Choose the installed {PROCESS_NAME} executable.')
        child = subprocess.Popen([str(exe)], cwd=str(exe.parent))
        # Pin the PID returned by this launch, rather than selecting by process name.
        session = GameSession.capture(psutil.Process(child.pid))
        if child.poll() is not None:
            raise RuntimeError('Reborn exited during startup. Check the game installation and try again.')
        if not same_path(session.path, exe):
            raise RuntimeError('Launched process path did not match the selected executable.')
        self.session = session
        self.sessions = [self.session]
        return self.session
