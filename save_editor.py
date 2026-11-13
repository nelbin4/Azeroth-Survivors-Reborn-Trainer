"""Authenticated, build-specific Reborn save inspection and upgrade preparation."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import struct
import tomllib
import time
import shutil

from trainer import BUILD


class GameData:
    def __init__(self, executable):
        self.blob = Path(executable).read_bytes()
        if hashlib.sha256(self.blob).hexdigest() != BUILD:
            raise ValueError('Unknown Reborn build; save editing is blocked.')
        pe = struct.unpack_from('<I', self.blob, 60)[0]
        optional = struct.unpack_from('<H', self.blob, pe + 20)[0]
        self.base = struct.unpack_from('<Q', self.blob, pe + 48)[0]
        self.sections = [struct.unpack_from('<8sIIII', self.blob, pe + 24 + optional + i * 40)
                         for i in range(struct.unpack_from('<H', self.blob, pe + 6)[0])]
        self.stream_key = self.read(0x70B7C59 + 8 + 0x22F555A, 16) + self.read(0x70B7C51 + 8 + 0x22F5572, 16)
        self.tag_key = self.read(0x70B7198 + 7 + 0x22F601C, 16) + self.read(0x70B71A4 + 7 + 0x22F6020, 16)

    def read(self, rva, size):
        for _, _, address, length, offset in self.sections:
            if address <= rva and rva + size <= address + length:
                return self.blob[offset + rva - address:offset + rva - address + size]
        raise ValueError('Game data address is outside the executable.')

    def crypt(self, nonce, plain):
        out = bytearray(plain)
        for i in range(0, len(out), 20):
            stream = hashlib.sha1(self.stream_key + nonce + struct.pack('<Q', i // 20)).digest()
            for j in range(min(20, len(out) - i)):
                out[i + j] ^= stream[j]
        return bytes(out)

    def unseal(self, raw):
        if len(raw) < 38 or raw[:5] != b'AZSV1' or raw[5] not in (0, 1):
            raise ValueError('Not a supported Reborn save envelope.')
        plain = self.crypt(raw[6:18], raw[18:-20])
        if not hmac.compare_digest(hashlib.sha1(self.tag_key + raw[:18] + plain).digest(), raw[-20:]):
            raise ValueError('Save authentication failed; nothing changed.')
        text = plain.decode('utf-8')
        if not text.startswith('VER\t1\n') or not text.endswith('\n'):
            raise ValueError('Unknown save payload version.')
        return text

    def seal(self, text, flag=0):
        header = b'AZSV1' + bytes([flag]) + os.urandom(12)
        plain = text.encode('utf-8')
        result = header + self.crypt(header[6:], plain) + hashlib.sha1(self.tag_key + header + plain).digest()
        if self.unseal(result) != text:
            raise RuntimeError('Upgraded save did not pass round-trip authentication.')
        return result

    def assets(self):
        # Embedded assets are (path pointer, path length, TOML pointer, TOML length).
        for _, _, _, length, offset in self.sections:
            if not self.blob[offset:offset + 8]:
                continue
            if self.sections[0][4] == offset:
                continue
            for pos in range(offset, offset + length - 32, 8):
                pointer, size, data, count = struct.unpack_from('<4Q', self.blob, pos)
                if not 6 <= size <= 140 or not 1 <= count <= 2000000:
                    continue
                try:
                    path = self.read(pointer - self.base, size).decode('ascii')
                    if not re.fullmatch(r'[a-zA-Z0-9_./-]+\.toml', path) or '/' not in path:
                        continue
                    raw = self.read(data - self.base, count).decode('utf-8')
                    yield path, tomllib.loads(raw.replace('\r\r\n', '\n'))
                except (ValueError, UnicodeError, struct.error):
                    continue

    def maximize(self, text):
        assets = dict(self.assets())
        classes = {}
        for path, data in assets.items():
            if path.endswith('/class.toml'):
                # ChrClasses identifiers are part of the installed content.
                entry = data.get('class', data)
                if isinstance(entry, dict) and 'id' in entry:
                    classes[int(entry['id'])] = entry.get('key', path.split('/')[-2])
        if len(classes) != 12:
            raise ValueError(f'Expected 12 class definitions, found {len(classes)}.')
        achievement_keys = {item['key'] for data in assets.values() for item in data.get('achievement', [])}
        if len(achievement_keys) < 80:
            raise ValueError('The achievement catalog is incomplete.')
        rows = [line.split('\t') for line in text.splitlines()]
        accounts = {r[1] for r in rows if r[0] == 'C' and len(r) >= 12}
        if not accounts:
            raise ValueError('No character accounts found in the save.')

        def upsert(tag, account, class_id, fields):
            match = next((r for r in rows if r[:3] == [tag, account, str(class_id)]), None)
            if match is None:
                rows.append([tag, account, str(class_id)] + fields)
            else:
                match[3:] = fields

        pieces = 0
        rarities = 6 + sum(len(data.get('gear_tier', [])) for data in assets.values())
        if rarities != 9:
            raise ValueError('Unexpected gear tier catalog.')
        for account in sorted(accounts):
            aggregate = next((r for r in rows if r[:2] == ['A', account]), None)
            if aggregate is None or len(aggregate) < 21:
                raise ValueError('Account summary has an unsupported layout.')
            known = set(filter(None, aggregate[13].split(',')))
            if not known <= achievement_keys:
                raise ValueError('The save names an achievement outside this build catalog.')
            aggregate[13] = ','.join(sorted(known | achievement_keys))
            wallet = next((r for r in rows if r[:2] == ['W', account]), None)
            if wallet is not None:
                wallet[2] = str(max(int(wallet[2]), 99999999))
            # Account XP (Z), shared by every class; exact level-30 threshold.
            levels = assets['base/account/levels.toml']
            count = levels['max_level'] - 1
            cap_xp = count * levels['xp_first'] + levels['xp_step'] * count * (count - 1) // 2
            account_xp = next((r for r in rows if r[:2] == ['Z', account]), None)
            if account_xp is None:
                rows.append(['Z', account, str(cap_xp)])
            else:
                account_xp[2] = str(max(int(account_xp[2]), cap_xp))
            for class_id in sorted(classes):
                gear = next((r for r in rows if r[:3] == ['G', account, str(class_id)]), None)
                # Native parser 0x70f11d0: owned entries are ID*STARS,
                # with ! selecting the worn item. The following column is
                # duplicate progress, not upgrade stars.
                inventory = {}
                owned_index = 4 if gear and len(gear) >= 6 else 3
                if gear:
                    for item in filter(None, gear[owned_index].split(',')):
                        identifier = int(item.rstrip('!').split('*')[0])
                        inventory[identifier] = item.rstrip('!')
                for slot in range(10):
                    for rarity in range(rarities):
                        identifier = 100000 + class_id * 1000 + slot * 100 + rarity * 10
                        inventory[identifier] = f'{identifier}*5' + ('!' if rarity == rarities - 1 else '')
                        pieces += 1
                fields = gear[3:].copy() if gear else ['', '']
                fields[owned_index - 3] = ','.join(inventory[i] for i in sorted(inventory))
                upsert('G', account, class_id, fields)
                # Class talent experience. The game's own point getter applies its cap.
                xp = next((r for r in rows if r[:3] == ['X', account, str(class_id)]), None)
                upsert('X', account, class_id, [str(max(int(xp[3]) if xp else 0, 10000000))])
        return '\n'.join('\t'.join(row) for row in rows) + '\n', dict(accounts=len(accounts), classes=len(classes), gear=pieces, achievements=len(achievement_keys), account_level=30)


def backup_save(executable, output_dir):
    """Copy a coherent authenticated disk save without changing the running game."""
    game = GameData(executable)
    source = Path(executable).parent / 'UserData/survivors-save.tsv'
    original = source.read_bytes()
    game.unseal(original)
    if source.read_bytes() != original:
        raise RuntimeError('Reborn saved during backup. Try again.')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    target = output_dir / 'survivors-save.tsv'
    target.write_bytes(original)
    if target.read_bytes() != original:
        raise RuntimeError('Backup readback failed.')
    # Talent allocations are client CVars, separate from earned talent XP.
    config_root = Path(executable).parent / 'WTF'
    if config_root.exists():
        for source_config in config_root.rglob('*.wtf'):
            destination = output_dir / 'WTF' / source_config.relative_to(config_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_config, destination)
    return str(target)


def prepare_upgrade(executable, output_dir):
    game = GameData(executable)
    source = Path(executable).parent / 'UserData/survivors-save.tsv'
    original = source.read_bytes()
    text, summary = game.maximize(game.unseal(original))
    upgraded = game.seal(text, original[5])
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    backup = output_dir / 'before-max.sav'
    if backup.exists():
        raise ValueError('This backup folder already exists; choose a new folder.')
    backup.write_bytes(original)
    output = output_dir / 'maxed.sav'
    output.write_bytes(upgraded)
    if game.unseal(output.read_bytes()) != text:
        raise ValueError('Written upgrade failed verification.')
    return dict(summary, path=str(output), backup=str(backup), source=str(source))


def apply_upgrade(executable, upgrade, backup):
    from trainer import find_games
    if find_games():
        raise RuntimeError('Exit Reborn before applying permanent upgrades; the running game holds the old save.')
    game = GameData(executable)
    source = Path(executable).parent / 'UserData/survivors-save.tsv'
    original = source.read_bytes()
    if original != Path(backup).read_bytes():
        raise RuntimeError('Your save changed after preparation. Prepare a fresh upgrade.')
    upgraded = Path(upgrade).read_bytes()
    game.unseal(upgraded)
    temporary = source.with_name(source.name + '.trainer-new')
    with temporary.open('wb') as stream:
        stream.write(upgraded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, source)
    if source.read_bytes() != upgraded:
        raise RuntimeError('Upgrade readback failed; the original backup is intact.')


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game', default='D:/Azeroth Survivors/AzerothSurvivorsGame.exe')
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--assets', action='store_true')
    parser.add_argument('--prepare', help='Folder for the upgraded save and original backup')
    args = parser.parse_args()
    game = GameData(args.game)
    if args.inspect:
        print(game.unseal((Path(args.game).parent / 'UserData/survivors-save.tsv').read_bytes()))
    if args.prepare:
        print(json.dumps(prepare_upgrade(args.game, args.prepare), indent=2))
    if args.assets:
        items = list(game.assets())
        Path(__file__).with_name('probe-content.json').write_text(json.dumps(dict(items), indent=2), encoding='utf-8')
        for path, content in items:
            if any(term in path for term in ('level', 'progress', 'gear/', 'achievement')):
                print(path, json.dumps(content)[:900])
