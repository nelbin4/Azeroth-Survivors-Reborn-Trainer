"""UI-independent targets and hold preferences for the unified Stats screen."""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class StatView:
    key: str
    label: str
    group: str
    minimum: int
    maximum: int
    unit: str = ''
    base: int = 0
    note: str = ''

    def native(self,value):
        if self.key=='health':return None
        number=float(value)
        if not math.isfinite(number) or not self.minimum<=number<=self.maximum or number!=int(number):
            raise ValueError(f'Enter a whole number from {self.minimum:,} to {self.maximum:,}.')
        return number/100 if self.key=='echo' else int(number)-self.base

    def display(self,value):
        if value is None:return '—'
        number=value*100 if self.key=='echo' else value+self.base
        return f'{number:,.0f}{self.unit}'


VIEWS = tuple(StatView(*row) for row in (
    ('damage','Damage bonus','Offense',0,9999,'%'),
    ('crit','Critical strike','Offense',8,100,'%',8,'Includes the native 8% base chance.'),
    ('crit_damage','Critical damage','Offense',50,10049,'%',50,'Includes the native 50% base bonus.'),
    ('cooldown','Cooldown reduction','Offense',0,80,'%'),
    ('area','Area of effect','Offense',0,1000,'%'),
    ('targets','Extra targets','Offense',0,32),
    ('echo','Echo chance','Offense',0,100,'%',0,'Waits for an active Echo effect; it does not insert an effect.'),
    ('health','Current health','Defense',0,1000000,'',0,'Follows verified maximum health automatically.'),
    ('max_health','Maximum health','Defense',1,1000000,'',0,'Minimum target: larger native health bonuses are preserved.'),
    ('armor','Damage reduction','Defense',0,60,'%'),
    ('regen','Regeneration bonus','Defense',0,9999,'',0,'Flat bonus; the in-game rate also includes native regeneration.'),
    ('speed','Movement speed bonus','Utility',0,200,'%'),
    ('pickup','Pickup range bonus','Utility',0,9999,'%'),
    ('gold_bonus','Gold find bonus','Utility',0,9999,'%'),
    ('luck','Luck bonus','Utility',0,9999,'%'),
    ('run_gold','Run gold','Resources',0,99999999),
    ('rerolls','Rerolls','Resources',0,9999),
))
BY_KEY={view.key:view for view in VIEWS}
DEFAULT_ENABLED = frozenset({'damage', 'cooldown', 'area', 'luck'})


class StatsPolicy:
    def __init__(self, preferences=None):
        if not isinstance(preferences, dict):
            preferences = {}
        self.targets = {view.key: view.maximum for view in VIEWS}
        saved_enabled = preferences.get('enabled')
        self.enabled = ({key for key in saved_enabled if isinstance(key, str) and key in BY_KEY}
                        if isinstance(saved_enabled, list) else set(DEFAULT_ENABLED))
        saved_targets = preferences.get('targets')
        if isinstance(saved_targets, dict):
            for key, value in saved_targets.items():
                if key not in BY_KEY or type(value) is not int or key == 'health':
                    continue
                try:
                    BY_KEY[key].native(value)
                except (ValueError, OverflowError):
                    continue
                self.targets[key] = value
        self.holds=set()
        self.pending=set()
        self.verified=set()
        self.values={}
        self.owner=None
        self.paused=False
        self.searching=False

    def accept(self,owner,values):
        if owner!=self.owner:
            self.owner=owner
            self.verified.clear()
            self.values.clear()
        new=set(values)-self.verified
        self.holds.update(new & self.enabled)
        self.verified=set(values)
        self.values=dict(values)

    def effective_target(self, key):
        return BY_KEY[key].maximum if key in self.enabled else self.targets[key]

    def set_target(self, key, value):
        if key in self.enabled:
            raise ValueError('Uncheck this stat to apply a one-time target.')
        if key == 'health':
            raise ValueError('Current health follows verified maximum health.')
        view = BY_KEY[key]
        view.native(value)
        self.targets[key] = int(float(value))
        self.pending.add(key)

    def preferences(self):
        return {'enabled': sorted(self.enabled), 'targets': dict(self.targets)}

    def set_enabled(self, key, enabled):
        self.pending.discard(key)
        if enabled:
            self.enabled.add(key)
            self.holds.add(key)
        else:
            self.enabled.discard(key)
            self.holds.discard(key)

    def request(self):
        automatic = self.enabled.copy() if self.searching and not self.paused else set()
        # Only fields that are currently proven may be written, even if the
        # user's hold intent survived an owner or verification change.
        held = (self.holds & self.enabled & self.verified) if not self.paused else set()
        return dict(auto_keys=automatic,
                    targets={key: view.native(self.effective_target(key))
                             for key, view in BY_KEY.items() if key != 'health'},
                    keys=held | (self.pending - self.enabled),
                    pending=self.pending.copy(), discover=self.searching)
