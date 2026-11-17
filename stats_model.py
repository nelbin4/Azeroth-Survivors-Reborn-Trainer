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


class StatsPolicy:
    def __init__(self):
        self.targets={v.key:v.maximum for v in VIEWS}
        self.enabled = set(BY_KEY)
        self.holds=set()
        self.overrides=set()
        self.pending=set()
        self.verified=set()
        self.values={}
        self.owner=None
        self.auto=True
        self.paused=False
        self.searching=False
        self.xp=False
        self.xp_once=False

    def accept(self,owner,values):
        if owner!=self.owner:
            self.owner=owner
            self.verified.clear()
            self.values.clear()
        new=set(values)-self.verified
        if self.auto:
            self.holds.update((new & self.enabled) - self.overrides)
        self.verified=set(values)
        self.values=dict(values)

    def set_target(self,key,value,freeze=False,apply=True):
        view=BY_KEY[key]
        if key not in self.enabled:
            raise ValueError('Enable this stat before applying a target.')
        view.native(value)
        self.targets[key]=view.maximum if key=='health' else int(float(value))
        self.freeze(key, freeze)
        if apply:self.pending.add(key)

    def freeze(self,key,enabled):
        if enabled:self.holds.add(key)
        else:
            self.holds.discard(key)
            self.pending.discard(key)
        self.overrides.add(key)

    def set_enabled(self, key, enabled):
        if enabled:
            self.enabled.add(key)
            if self.auto and key in self.verified and key not in self.overrides:
                self.holds.add(key)
        else:
            self.enabled.discard(key)
            self.pending.discard(key)

    def maximize(self):
        for key in self.enabled:
            self.targets[key] = BY_KEY[key].maximum
        self.holds.update(self.enabled)
        self.overrides.update(self.enabled)
        self.pending.update(self.verified & self.enabled)
        self.paused = False

    def request(self):
        automatic = self.enabled - self.overrides if self.auto and self.searching and not self.paused else set()
        return dict(auto_keys=automatic,
                    targets={k:BY_KEY[k].native(v) for k,v in self.targets.items() if k!='health'},
                    keys=set() if self.paused else (self.holds | self.pending) & self.enabled,
                    pending=set(self.pending),discover=self.searching,paused=self.paused,
                    xp=self.xp and not self.paused,xp_once=self.xp_once and not self.paused)
