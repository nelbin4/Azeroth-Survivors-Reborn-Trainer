"""Solo hero discovery and guarded stat controls for the verified Reborn build."""
from dataclasses import dataclass, replace
import struct

from stack_probe import discover

STRIDE = 0x1798
NAME_OFFSET = 0x1038
PLAYER_SIGNATURES = {
    0x6D8750F: bytes.fromhex('488b89600e0000'),
    0x6D8752F: bytes.fromhex('4869c998170000'),
    0x6D8753A: bytes.fromhex('0f10840a70160000'),
    0x6D7D728: bytes.fromhex('488d875c010000'),
    0x6DBEF36: bytes.fromhex('8b8878160000'),
    0x6D7D7F1: bytes.fromhex('4881c744170000'),
    0x6D7D79A: bytes.fromhex('488d8f00010000'),
}


@dataclass(frozen=True)
class Stat:
    label: str
    offset: int
    maximum: int
    default: int
    fmt: str = 'I'


STATS = {
    'damage': Stat('Damage bonus %', 0x1678, 9999, 999),
    'cooldown': Stat('Cooldown reduction %', 0x167C, 80, 80),
    'crit': Stat('Crit bonus % (base 8%)', 0x1680, 92, 92),
    'crit_damage': Stat('Crit damage bonus % (base 50%)', 0x1684, 9999, 999),
    'speed': Stat('Movement speed bonus %', 0x1688, 200, 50),
    'armor': Stat('Damage reduction %', 0x168C, 60, 60),
    'regen': Stat('Regeneration (flat bonus)', 0x1690, 9999, 999),
    'pickup': Stat('Pickup range bonus %', 0x169C, 9999, 999),
    'gold_bonus': Stat('Gold find bonus %', 0x16A0, 9999, 999),
    'area': Stat('Area bonus %', 0x16A4, 1000, 500),
    'targets': Stat('Extra targets', 0x16A8, 32, 8),
    'luck': Stat('Luck bonus %', 0x16AC, 9999, 999),
    'max_health': Stat('Maximum health', 0x100, 1000000, 1000000, 'Q'),
    'run_gold': Stat('Run gold', 0x15C, 99999999, 99999999),
    'rerolls': Stat('Free rerolls', 0x1744, 9999, 9999),
}


def bounded_integer(value, maximum):
    # Reject fractions rather than truncating user input.
    numeric = float(value)
    if not 0 <= numeric <= maximum or numeric != int(numeric):
        raise ValueError(f'Enter a whole number from 0 to {maximum}.')
    return int(numeric)


class Player:
    def __init__(self, trainer):
        self.trainer = trainer
        self.memory = trainer.memory
        self.lease = None
        self.frozen = {}
        self.original = {}
        self.xp_assist = False
        profile = getattr(trainer, 'profile', None)
        # Real trainers carry a resolved Layout; test doubles use legacy defaults.
        from compatibility import Layout
        self.profile = profile if isinstance(profile, Layout) else None
        self.stride = self.profile.stride if self.profile else STRIDE
        self.name_offset = self.profile.name if self.profile else NAME_OFFSET
        self.vector = self.profile.vector if self.profile else 0xBA8
        self.index = self.profile.index if self.profile else 0xE60
        self.stats = dict(STATS)
        if self.profile:
            for key, stat in STATS.items():
                if 0x1670 <= stat.offset <= 0x16AC:
                    self.stats[key] = replace(stat, offset=stat.offset + self.profile.stats - 0x1670)
            self.stats['rerolls'] = replace(STATS['rerolls'], offset=self.profile.rerolls)
        self.verify_code()

    def verify_code(self):
        self.trainer.guard()
        # Trainer.guard already checks every resolved player/config code anchor.
        for rva, expected in ({} if self.profile else PLAYER_SIGNATURES).items():
            if self.memory.read(self.trainer.base + rva, len(expected)) != expected:
                raise RuntimeError('Reborn player instruction mismatch; player changes blocked.')

    def connect(self):
        self.verify_code()
        self.stop()
        candidates = discover(self.trainer.process, self.memory, self.profile)
        candidates = [c for c in candidates if c['count'] == 1 and c['index'] == 0]
        heroes = {(c['hero'], c['guid'], c['name']) for c in candidates}
        if len(heroes) != 1:
            raise RuntimeError(f'Found {len(heroes)} distinct verified solo heroes. Start a solo run, then Find hero.')
        # Stack frames can retain copies of the same Game. Count allocations,
        # not references; choose the outermost frame and revalidate before writes.
        self.lease = max(candidates, key=lambda c: int(c['game'], 16))
        return self.snapshot()

    def snapshot(self):
        self.verify_code()
        if not self.lease:
            raise RuntimeError('Click Find hero while playing solo.')
        game = int(self.lease['game'], 16)
        cap, pointer, count = struct.unpack('<3Q', self.memory.read(game + self.vector, 24))
        index, = struct.unpack('<Q', self.memory.read(game + self.index, 8))
        if not (count == 1 and index == 0 and 1 <= cap <= 4
                and pointer == int(self.lease['hero'], 16)):
            raise RuntimeError('Solo hero allocation changed. Stop and Find hero again.')
        raw = self.memory.read(pointer, self.stride)
        guid, hp, maximum = struct.unpack_from('<3Q', raw, 0xF0)
        level, xp, next_xp, gold = struct.unpack_from('<4I', raw, 0x150)
        capacity, name_pointer, length = struct.unpack_from('<3Q', raw, self.name_offset)
        if (guid != self.lease['guid'] or not 1 <= length <= capacity <= 128
                or not 1 <= level <= 10000 or not 1 <= maximum <= 10**12 or hp > maximum):
            raise RuntimeError('Hero identity or health changed unexpectedly. Find hero again.')
        name = self.memory.read(name_pointer, length).decode('utf-8')
        if name != self.lease['name']:
            raise RuntimeError('The active character changed. Find hero again.')
        if level < self.lease['level']:
            raise RuntimeError('A new run started. Find hero to resume player controls.')
        values = {key: struct.unpack_from('<' + stat.fmt, raw, stat.offset)[0] for key, stat in self.stats.items()}
        ceilings = {'max_health': 10**12, 'run_gold': 0xFFFFFFFF}
        if any(v > ceilings.get(k, 1000000) for k, v in values.items()):
            raise RuntimeError('Unexpected player stat layout. Changes blocked.')
        if self.memory.read(game + self.vector, 24) != struct.pack('<3Q', cap, pointer, count):
            raise RuntimeError('Hero moved during the snapshot; retry.')
        return dict(hero=pointer, guid=guid, name=name, level=level, hp=hp, maximum=maximum,
                    xp=xp, next_xp=next_xp, gold=gold, values=values)

    def write(self, offset, fmt, value, before_snapshot):
        current = self.snapshot()
        for key in ('hero', 'guid', 'name', 'level'):
            if current[key] != before_snapshot[key]:
                raise RuntimeError('Hero changed before writing. Nothing applied.')
        stat_key = next((k for k, s in self.stats.items() if s.offset == offset), None)
        if stat_key and current['values'][stat_key] != before_snapshot['values'][stat_key]:
            raise RuntimeError('The game recalculated this stat before writing. Retry.')
        address = current['hero'] + offset
        raw = struct.pack('<' + fmt, value)
        self.memory.write(address, raw)
        if self.memory.read(address, len(raw)) != raw:
            raise RuntimeError('Player value changed before readback; retry.')

    def set_stat(self, key, value):
        stat = self.stats[key]
        value = bounded_integer(value, stat.maximum)
        if key == 'max_health' and value < 1:
            raise ValueError('Maximum health must be at least 1.')
        snapshot = self.snapshot()
        before = snapshot['values'][key]
        if before == value:
            return
        if key == 'max_health' and snapshot['hp'] > value:
            self.write(0xF8, 'Q', value, snapshot)
            snapshot = self.snapshot()
        previous = self.original.get(key)
        # Recalculations supply a fresh original. Never roll back a previous level.
        if previous is None or previous['level'] != snapshot['level'] or previous['after'] != before:
            self.original[key] = dict(before=before, after=value, level=snapshot['level'])
        else:
            previous['after'] = value
        self.write(stat.offset, stat.fmt, value, snapshot)

    def hold(self, key, value, enabled=True):
        if enabled:
            value = bounded_integer(value, STATS[key].maximum)
            self.set_stat(key, value)
            self.frozen[key] = value
        else:
            self.frozen.pop(key, None)
            self.restore_field(key)

    def maximize(self):
        """Hold every supported combat stat at its trainer limit."""
        self.snapshot()
        for key, stat in STATS.items():
            self.hold(key, stat.maximum)
        self.xp_assist = True
        self.heal()
        self.ready_level()
        return self.snapshot()

    def tick(self):
        snapshot = self.snapshot()
        if snapshot['level'] < self.lease['level']:
            self.frozen.clear()
            self.original.clear()
            self.lease = None
            raise RuntimeError('A new run started. Find hero to resume player controls.')
        self.lease['level'] = snapshot['level']
        for key, value in list(self.frozen.items()):
            if snapshot['values'][key] != value:
                self.set_stat(key, value)
        if 'max_health' in self.frozen:
            if snapshot['hp'] != snapshot['maximum']:
                self.heal()
        if self.xp_assist and snapshot['xp'] != snapshot['next_xp'] - 1:
            self.ready_level()
        return self.snapshot()

    def restore_field(self, key):
        original = self.original.get(key)
        if not original:
            return
        if key in ('run_gold', 'rerolls'):
            self.original.pop(key, None)
            return
        snapshot = self.snapshot()
        if snapshot['level'] == original['level'] and snapshot['values'][key] == original['after']:
            if key == 'max_health' and snapshot['hp'] > original['before']:
                self.write(0xF8, 'Q', original['before'], snapshot)
                snapshot = self.snapshot()
            self.write(self.stats[key].offset, self.stats[key].fmt, original['before'], snapshot)
        self.original.pop(key, None)

    def stop(self):
        self.frozen.clear()
        self.xp_assist = False
        for key in list(self.original):
            try:
                self.restore_field(key)
            except (RuntimeError, OSError):
                self.original.pop(key, None)

    def gold(self, value):
        value = bounded_integer(value, 99999999)
        snapshot = self.snapshot()
        self.write(0x15C, 'I', value, snapshot)

    def heal(self):
        snapshot = self.snapshot()
        self.write(0xF8, 'Q', snapshot['maximum'], snapshot)

    def ready_level(self):
        snapshot = self.snapshot()
        if not 1 <= snapshot['next_xp'] <= 100000000:
            raise RuntimeError('Cannot verify the next-level XP requirement.')
        self.write(0x154, 'I', snapshot['next_xp'] - 1, snapshot)
