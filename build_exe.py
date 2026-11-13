"""Build the self-contained Reborn trainer and its portable release zip."""
import shutil
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = 'Azeroth Survivor Reborn Trainer'
from trainer import VERSION


def main():
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
               '--onefile', '--noconsole', '--name', NAME,
               '--icon', str(ROOT / 'assets/trainer-icon.ico'),
               '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build'),
               '--specpath', str(ROOT / 'build'), str(ROOT / 'trainer.py')]
    subprocess.run(command, check=True)
    release = ROOT / 'release'
    release.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'dist' / f'{NAME}.exe', release / f'{NAME}.exe')
    shutil.copy2(ROOT / 'README.md', release / 'README.md')
    shutil.copy2(ROOT / 'LICENSE', release / 'LICENSE')
    archive = release / f'AzerothSurvivorRebornTrainer-{VERSION}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for filename in (f'{NAME}.exe', 'README.md', 'LICENSE'):
            output.write(release / filename, filename)
    files = [release / f'{NAME}.exe', archive]
    (release / 'SHA256SUMS.txt').write_text(''.join(
        f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in files), encoding='utf-8')
    print(f'Built {archive}')


if __name__ == '__main__':
    main()
