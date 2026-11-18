"""One background owner for discovery, live readings, edits and stat holds."""
from contextlib import nullcontext
import hashlib
from pathlib import Path
import queue
import threading
import time

from automatic_hunt import AutomaticHunt, HuntSchedule, SearchPlan, file_identity, make_plan
from compatibility import Layout
from hunt_fields import FieldProof
from game_process import same_path
from hunt_freeze import HuntFreezer
from player import STATS
from stats_model import StatsPolicy
from session_role import RoleMonitor, RoleLease


def plan_from_layout(path,layout):
    """Retain already-verified older builds without guessing new layouts."""
    before=file_identity(path)
    if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=layout.digest:
        raise RuntimeError('Game image changed before legacy proof reuse.')
    guards=tuple(layout.signatures.items())
    fields=[]
    for key,stat in STATS.items():
        offset=stat.offset
        if 0x1670<=offset<=0x16AC:
            offset=layout.stats+(layout.fields[key] if layout.fields is not None else offset-0x1670)
        elif key=='rerolls':offset=layout.rerolls
        fields.append(FieldProof(key,offset,stat.fmt,stat.maximum,guards))
    fields.append(FieldProof('health',0xF8,'Q',1000000,guards))
    if not guards or before!=file_identity(path):
        raise RuntimeError('Game image changed during legacy proof reuse.')
    rva,guard=guards[0]
    return SearchPlan(layout.digest,layout.vector,layout.index,layout.stride,layout.stats,rva,guard,tuple(fields),layout.name,guards),before


class StatsWorker:
    def __init__(self,memory_factory=None,lock_factory=None):
        self.hunt=AutomaticHunt()
        self.freezer=HuntFreezer()
        self.mode=None
        self.memory_factory=memory_factory
        self.lock_factory=lock_factory

    def run(self,session,native,request,cancelled):
        from trainer import Memory,InstanceLock
        memory_factory=self.memory_factory or Memory
        lock_factory=self.lock_factory or InstanceLock
        layout=native.profile if native and isinstance(native.profile,Layout) else None
        mode=(session,layout.digest if layout else None)
        if mode!=self.mode:
            self.hunt.reset();self.freezer.reset();self.mode=mode
        source=(lambda path:plan_from_layout(path,layout)) if layout else make_plan
        reader=writer=lock=None
        try:
            reader=memory_factory(session.pid,writable=False)
            lease=request.get('role_lease')
            role_reader=lease.reader() if isinstance(lease,RoleLease) and lease.session==session else None
            def current_role():
                if role_reader is None:return 'unknown'
                try:return role_reader.read(reader)
                except (ValueError,OSError,UnicodeError):return 'unknown'
            result=self.hunt.step(session,reader,cancelled=cancelled,plan_source=source,discover=request['discover'])
            fields=self.freezer.observe(self.hunt,result)
            role=current_role()
            result.update(values={},freeze=dict(held={},errors={}),role=role)
            if not fields:return result
            owner=result['heroes'][0]
            result['owner']=(session,self.hunt.plan.digest,owner['hero'],owner['identity'],owner.get('count',1),owner.get('index',0),owner.get('name'))
            # Auto max authorizes the first usable observation of each
            # independently proven field. UI feedback arrives after this pass.
            wanted = (request['keys'] | request.get('auto_keys', set())) & {proof.key for proof in fields}
            multiplayer=owner.get('count',1)>1
            permitted=role=='host' or (role=='none' and not multiplayer)
            result['host_required']=not permitted
            writes = bool(wanted) and permitted
            borrowed=bool(native and native.process.pid==session.pid and native.created==session.created and native.lock)
            gate=native.write_gate if borrowed else nullcontext()
            with gate:
                if cancelled():raise InterruptedError('Stats operation cancelled.')
                if borrowed:native.guard()
                if writes:
                    if not borrowed:lock=lock_factory(f'{session.pid}_{session.created}')
                    writer=memory_factory(session.pid,writable=True)
                outcome=self.freezer.apply(self.hunt,result,writer or reader,fields,cancelled,
                                          targets=request['targets'],keys=wanted,read_only=not writes,
                                          allow_multiplayer=role=='host',
                                          authorize=lambda:current_role()=='host' if multiplayer else current_role() in ('host','none'))
                result['values']=outcome['values']
                result['freeze']=outcome
            return result
        except Exception:
            self.freezer.reset()
            raise
        finally:
            if writer:writer.close()
            if lock:lock.close()
            if reader:reader.close()


class StatsController:
    def __init__(self,root,session_provider,native_provider,closing,on_change=lambda:None,preferences=None):
        self.root=root
        self.session_provider=session_provider
        self.native_provider=native_provider
        self.closing=closing
        self.on_change=on_change
        self.policy=StatsPolicy(preferences)
        self.worker=StatsWorker()
        from trainer import ROOT
        self.role_monitor=RoleMonitor(ROOT/'reborn-role-cache.json')
        self.role='unknown'
        self.role_lease=None
        self.schedule=HuntSchedule()
        self.results=queue.Queue()
        self.epoch=0
        self.cancel=threading.Event()
        self.busy=False
        self.next_scan=0
        self.session=None
        self.last={}
        self.message='Ready. Click Find to start automatic Hunt.'
        self.closed=False
        root.after(250,self.poll)

    def changed(self):
        self.epoch+=1
        self.cancel.set()
        self.last.pop('freeze', None)
        self.next_scan=0
        self.on_change()

    def connected_session(self):
        session = self.session_provider()
        trainer = self.native_provider()
        if (session and trainer and trainer.process.pid == session.pid
                and trainer.created == session.created and same_path(trainer.path, session.path)):
            return session
        return None

    def find(self):
        if not self.connected_session():
            self.message='Wait for Reborn to connect before using Find.'
            self.on_change();return
        self.policy.searching=True
        self.message='Finding verified stats in the background…'
        self.changed()

    def stop_search(self):
        self.policy.searching=False
        self.message='Search stopped. Existing holds can continue.'
        self.changed()

    def pause(self):
        self.policy.paused=not self.policy.paused
        self.message='Stat freezes stopped.' if self.policy.paused else 'Stat freezes resumed.'
        self.changed()

    def close(self):
        self.closed=True;self.cancel.set();self.role_monitor.close()

    def invalidate(self):
        self.policy.pending.clear()
        self.policy.accept(None,{})
        self.last={}
        self.changed()

    def poll(self):
        if self.closed or self.closing():self.close();return
        session=self.connected_session()
        self.role_monitor.start(session)
        role,lease=self.role_monitor.snapshot(session)
        if (role,lease)!=(self.role,self.role_lease):
            self.role,self.role_lease=role,lease
            if role == 'guest':
                self.message = 'Guest session · stats are read-only.'
            elif self.policy.searching:
                self.message = 'Rechecking the selected character…'
            self.changed()
        if session!=self.session:
            if self.session is not None:
                self.policy.pending.clear()
            self.session=session
            self.policy.accept(None,{})
            self.last={}
            self.message='Finding this game session…' if self.policy.searching and session else 'Ready. Click Find to start automatic Hunt.'
            self.changed()
        try:
            epoch,result=self.results.get_nowait()
            self.busy=False
            if epoch==self.epoch:
                expedite=False
                if isinstance(result,Exception):
                    self.last={}
                    self.policy.accept(None,{})
                    self.message=f'Waiting to retry: {result}'
                else:
                    old=set(self.policy.holds)
                    self.last=result
                    self.policy.accept(result.get('owner'),result['values'])
                    successful=set(result['freeze'].get('held',{}))
                    self.policy.pending.difference_update(successful)
                    held = 0 if self.policy.paused else len(set(result['freeze'].get('confirmed', {})) & self.policy.holds)
                    selected=result.get('heroes',[{}])[0] if result.get('heroes') else {}
                    who = f" · {selected.get('name') or 'player'}" if selected else ''
                    if selected.get('level') is not None:
                        who += f" (level {selected['level']})"
                    if result.get('host_required'):self.message='Guest session: stat writes disabled.' if result.get('role')=='guest' else 'Waiting to verify a solo or host session.'
                    elif self.policy.paused:self.message=f'Freezes stopped · {len(self.policy.verified)} verified stats.'
                    elif held:self.message=f'{held} stats frozen{who} · '+('Hunt continues in the background.' if self.policy.searching else 'Monitoring existing fields.')
                    elif successful:self.message=f'{len(successful)} stats applied{who} · checking persistence…'
                    elif self.policy.verified:self.message=f'{len(self.policy.verified)} verified stats available.'
                    elif result['ambiguous']:self.message='Multiple player allocations found; observing without writes.'
                    else:self.message='Finding the selected player allocation…' if self.policy.searching else 'Click Find to locate the current player.'
                    if old!=self.policy.holds:expedite=True
                delay=self.schedule.delay(result)
                if not isinstance(result,Exception) and self.policy.pending&self.policy.verified:delay=min(delay,.25)
                self.next_scan=0 if expedite else time.monotonic()+delay
                self.on_change()
        except queue.Empty:pass
        cached=(self.worker.mode is not None and self.worker.mode[0]==session and self.worker.hunt.plan is not None)
        monitor=bool(self.policy.searching or self.policy.verified or
                     (cached and (self.policy.holds or self.policy.pending)))
        if session and monitor and not self.busy and time.monotonic()>=self.next_scan:
            request=self.policy.request()
            request['role_lease']=self.role_lease
            native=self.native_provider()
            epoch=self.epoch
            cancel=threading.Event();self.cancel=cancel
            self.busy=True
            # next_scan is a deadline, not an instruction to keep an old
            # cancellation event alive across UI changes.
            self.next_scan=time.monotonic()+1
            def work():
                try:result=self.worker.run(session,native,request,lambda:cancel.is_set() or self.closed or self.closing())
                except Exception as exc:result=exc
                self.results.put((epoch,result))
            threading.Thread(target=work,daemon=True).start()
        self.root.after(250,self.poll)
