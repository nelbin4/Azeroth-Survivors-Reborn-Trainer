"""Exercise an actual PyInstaller one-file self-update before publishing a release."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from updater import restart_environment


def main():
    root = Path(__file__).resolve().parent
    work = root / 'build' / 'frozen-update-smoke'
    work.mkdir(parents=True, exist_ok=True)
    source = work / 'probe.py'
    source.write_text('''import json
from pathlib import Path
import sys
import updater
root = Path(sys.executable).parent
if '--update' in sys.argv:
    (root / 'parent.json').write_text(json.dumps({'runtime': sys._MEIPASS}))
    updater.launch_install(root / '.updates/update.download')
else:
    dll = f'python{sys.version_info.major}{sys.version_info.minor}.dll'
    (root / 'started.json').write_text(json.dumps({
        'runtime': sys._MEIPASS, 'dll_exists': (Path(sys._MEIPASS) / dll).is_file(),
        'frozen': sys.frozen}))
''', encoding='utf-8')
    output = work / 'out'
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--noconsole',
                    '--log-level', 'WARN', '--name', 'UpdateSmoke', '--paths', str(root),
                    '--distpath', str(output), '--workpath', str(work / 'work'),
                    '--specpath', str(work), str(source)], check=True)
    executable = output / 'UpdateSmoke.exe'
    for name in ('parent.json', 'started.json'):
        (output / name).unlink(missing_ok=True)
    staged = output / '.updates' / 'update.download'
    staged.parent.mkdir(exist_ok=True)
    shutil.copy2(executable, staged)
    subprocess.run([str(executable), '--update'], cwd=output, env=restart_environment(),
                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=30, check=True)
    marker = output / 'started.json'
    deadline = time.monotonic() + 30
    while not marker.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('The frozen trainer restart did not start a fresh Python runtime.')
        time.sleep(0.25)
    before = json.loads((output / 'parent.json').read_text())
    after = json.loads(marker.read_text())
    if not after['frozen'] or not after['dll_exists'] or after['runtime'] == before['runtime']:
        raise RuntimeError('The restarted executable reused an invalid Python runtime.')
    if not executable.with_suffix('.previous.exe').exists():
        raise RuntimeError('The update did not preserve its executable backup.')
    print('Frozen self-update passed: fresh Python runtime, DLL loaded, backup preserved.')


if __name__ == '__main__':
    main()
