"""GitHub release checks and verified, opt-in Windows self-updates."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
from urllib.parse import urlparse

REPOSITORY = 'nelbin4/Azeroth-Survivors-Reborn-Trainer'
RELEASES = f'https://github.com/{REPOSITORY}/releases'
API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_DOWNLOAD = 64 * 1024 * 1024


def version_tuple(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    if not match:
        raise ValueError('Unsupported release version.')
    return tuple(map(int, match.groups()))


def parse_release(data, current):
    if data.get('draft') or data.get('prerelease'):
        raise ValueError('Only stable published releases are supported.')
    tag = data['tag_name']
    available = version_tuple(tag) > version_tuple(current)
    asset = next((a for a in data.get('assets', []) if a.get('name') in
                  ('Azeroth.Survivor.Reborn.Trainer.exe', 'Azeroth Survivor Reborn Trainer.exe')), None)
    result = dict(tag=tag, available=available, page=f'{RELEASES}/tag/{tag}', asset=None)
    if asset:
        expected = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
        url = asset.get('browser_download_url', '')
        digest = asset.get('digest', '')
        size = asset.get('size', 0)
        if not url.startswith(expected) or urlparse(url).query or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('Release asset source or SHA-256 digest is invalid.')
        if not isinstance(size, int) or not 1024 <= size <= MAX_DOWNLOAD:
            raise ValueError('Release asset size is invalid.')
        result['asset'] = dict(url=url, digest=digest[7:], size=size)
    return result


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        'User-Agent': 'AzerothSurvivorRebornTrainer', 'Accept': 'application/vnd.github+json'}), timeout=20)


def check(current):
    with request(API) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Release response is too large.')
    return parse_release(json.loads(raw), current)


def download(release, directory):
    # Validate even when called without a preceding check().
    tag = release['tag']
    version_tuple(tag)
    asset = release['asset']
    if not asset or not asset['url'].startswith(f'https://github.com/{REPOSITORY}/releases/download/{tag}/'):
        raise ValueError('A verified executable is not available for this release.')
    if not re.fullmatch('[0-9a-f]{64}', asset['digest']) or not 1024 <= asset['size'] <= MAX_DOWNLOAD:
        raise ValueError('Invalid executable metadata.')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'trainer-{tag}.download'
    hasher = hashlib.sha256()
    total = 0
    with request(asset['url']) as response, path.open('wb') as output:
        final = urlparse(response.geturl())
        if final.scheme != 'https' or not (final.hostname == 'github.com' or
                (final.hostname or '').endswith('.githubusercontent.com')):
            raise ValueError('Unexpected download redirect.')
        while block := response.read(256 * 1024):
            total += len(block)
            if total > asset['size'] or total > MAX_DOWNLOAD:
                raise ValueError('Download exceeded its declared size.')
            hasher.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if total != asset['size'] or hasher.hexdigest() != asset['digest']:
        raise ValueError('Download verification failed. Nothing installed.')
    raw = path.read_bytes()
    pe = int.from_bytes(raw[60:64], 'little')
    if raw[:2] != b'MZ' or raw[pe:pe + 6] != b'PE\0\0\x64\x86':
        raise ValueError('Download is not a Windows x64 executable.')
    return path


def install_command(staged, target, pid):
    staged, target = Path(staged).resolve(), Path(target).resolve()
    if staged.parent != target.parent / '.updates' or not staged.is_file():
        raise ValueError('Staged update must be in this trainer’s .updates folder.')
    quote = lambda s: "'" + str(s).replace("'", "''") + "'"
    digest = hashlib.sha256(staged.read_bytes()).hexdigest()
    # Copy the old executable before replacement; retry while the one-file
    # bootloader exits. A failed replacement leaves the old installation intact.
    script = f"""$ErrorActionPreference='Stop'
$env:PYINSTALLER_RESET_ENVIRONMENT='1'
$target={quote(target)}
$staged={quote(staged)}
$backup={quote(target.with_suffix('.previous.exe'))}
$deadline=(Get-Date).AddSeconds(90)
$backedUp=$false
while (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{
  if ((Get-Date) -gt $deadline) {{ exit 1 }}
  Start-Sleep -Milliseconds 300
}}
while ((Get-Date) -lt $deadline) {{
  try {{
    $sha=[Security.Cryptography.SHA256]::Create()
    try {{ $actual=[BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($staged))).Replace('-','').ToLowerInvariant() }} finally {{ $sha.Dispose() }}
    if ($actual -ne '{digest}') {{ exit 2 }}
    if (-not $backedUp) {{ [IO.File]::Copy($target,$backup,$true); $backedUp=$true }}
    [IO.File]::Copy($staged,$target,$true)
    try {{ Start-Process -FilePath $target -WorkingDirectory {quote(target.parent)} }}
    catch {{ [IO.File]::Copy($backup,$target,$true); throw }}
    exit 0
  }} catch {{ Start-Sleep -Milliseconds 500 }}
}}
if ($backedUp) {{ try {{ [IO.File]::Copy($backup,$target,$true) }} catch {{}} }}
exit 1
"""
    return ['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', script]


def launch_install(staged):
    if not getattr(sys, 'frozen', False):
        raise RuntimeError('Self-update is available in the standalone executable. Use the release page for source installs.')
    return subprocess.Popen(install_command(staged, sys.executable, os.getpid()),
                            creationflags=0x08000000, env=restart_environment())


def restart_environment():
    # A one-file restart must extract its own runtime. The old bootloader removes
    # its _MEI directory on exit; inherited worker metadata would reuse that path.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith('_PYI_') and key.upper() != '_MEIPASS2'}
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return environment
