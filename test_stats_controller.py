from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from compatibility import resolve,NativeLayout
from hunt_fields import FieldProof
from stats_controller import StatsController,StatsWorker,plan_from_layout
from stats_model import StatsPolicy
from test_compatibility import fixture


class Root:
    def after(self,*_):pass


class StatsControllerTests(unittest.TestCase):
    def controller(self):
        session=SimpleNamespace(pid=123,created=456,path='game.exe')
        native = SimpleNamespace(process=SimpleNamespace(pid=session.pid), created=session.created, path=session.path)
        controller=StatsController(Root(),lambda:session,lambda:native,lambda:False)
        controller.role_monitor=Mock(snapshot=Mock(return_value=('unknown',None)))
        return controller

    def test_no_stat_discovery_or_writes_start_before_find(self):
        controller=self.controller()
        controller.worker=SimpleNamespace(mode=None,run=Mock())
        with patch('stats_controller.threading.Thread') as thread:
            controller.poll()
        thread.assert_not_called()
        controller.worker.run.assert_not_called()

    def test_find_starts_background_work_and_pause_preserves_discovery(self):
        controller=self.controller();controller.worker=SimpleNamespace(mode=None)
        with patch('stats_controller.threading.Thread') as thread:
            controller.find();controller.poll()
            thread.assert_called_once()
        controller.pause()
        self.assertTrue(controller.policy.searching)
        self.assertEqual(controller.policy.request()['keys'],set())

    def test_stale_results_are_discarded_and_cancel_events_are_not_reused(self):
        controller=self.controller();controller.worker=SimpleNamespace(mode=None)
        with patch('stats_controller.threading.Thread'):
            controller.find();controller.poll()
            epoch=controller.epoch;cancel=controller.cancel
            controller.pause()
            controller.results.put((epoch,RuntimeError('stale')))
            controller.poll()
        self.assertTrue(cancel.is_set())
        self.assertIsNot(cancel,controller.cancel)
        self.assertNotIn('stale',controller.message)

    def test_verified_readings_enable_incremental_holds_then_apply_on_next_round(self):
        controller=self.controller();controller.worker=SimpleNamespace(mode=None)
        controller.session=controller.session_provider()
        controller.policy.searching=True
        response=dict(heroes=[{}],values={'damage':7},freeze=dict(held={}),partial=False,ambiguous=False,owner='hero')
        controller.results.put((controller.epoch,response))
        with patch('stats_controller.threading.Thread') as thread:
            controller.poll()
        self.assertIn('damage',controller.policy.holds)
        self.assertEqual(controller.policy.values['damage'],7)
        thread.assert_called_once()

    def test_existing_holds_are_retried_after_read_error_even_with_search_stopped(self):
        controller=self.controller();session=controller.session_provider();controller.session=session
        controller.policy.accept('hero',{'damage':7})
        controller.worker=SimpleNamespace(mode=(session,None),hunt=SimpleNamespace(plan=object()))
        controller.results.put((controller.epoch,RuntimeError('temporary read failure')))
        with patch('stats_controller.threading.Thread') as thread:
            controller.poll()
        self.assertEqual(controller.policy.verified,set())
        # Backoff is respected, but the retry remains eligible after it elapses.
        controller.next_scan=0
        with patch('stats_controller.threading.Thread') as thread:
            controller.poll()
            thread.assert_called_once()

    def test_reuses_verified_legacy_fields_without_dynamic_formatter_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'game.exe';path.write_bytes(fixture())
            layout=resolve(path);plan,_=plan_from_layout(path,layout)
            fields={p.key:p for p in plan.fields}
            self.assertEqual(fields['damage'].offset,layout.stats+8)
            self.assertEqual(len(fields),16)
            self.assertEqual(fields['rerolls'].offset,layout.rerolls)
            path.write_bytes(path.read_bytes()+b'changed')
            with self.assertRaisesRegex(RuntimeError,'changed'):plan_from_layout(path,layout)

    def test_worker_auto_maxes_first_verified_field_under_native_writer_gate(self):
        session=SimpleNamespace(pid=123,created=456,path='game.exe')
        reader=Mock();writer=Mock();factory=Mock(side_effect=[reader,writer]);lock=Mock()
        worker=StatsWorker(factory,lock)
        worker.hunt=Mock();worker.freezer=Mock()
        worker.hunt.plan.digest='abc'
        proof=FieldProof('damage',0x200,'I',9999,((0x100,b'proof'),))
        worker.freezer.observe.return_value=(proof,)
        worker.hunt.step.return_value=dict(heroes=[dict(hero=1000,identity=91)],ambiguous=False,partial=False)
        gate=threading.RLock()
        native=SimpleNamespace(profile=NativeLayout('abc',0,{}),process=SimpleNamespace(pid=123),created=456,
                               lock=object(),write_gate=gate,guard=Mock())
        def apply(*args,**kwargs):
            self.assertTrue(gate._is_owned())
            self.assertEqual(kwargs['keys'],{'damage'})
            return dict(held={'damage':9999},errors={},values={'damage':9999})
        worker.freezer.apply.side_effect=apply
        policy=StatsPolicy();policy.searching=True;policy.freeze('armor',False)
        self.assertEqual(policy.request()['keys'], set())
        from session_role import RoleLease
        request=policy.request();request['role_lease']=RoleLease(session,0x10000,0x20000)
        with patch.object(RoleLease,'reader',return_value=SimpleNamespace(read=lambda memory:'none')):
            result=worker.run(session,native,request,lambda:False)
        self.assertEqual(result['values']['damage'],9999)
        lock.assert_not_called()
        native.guard.assert_called_once()
        reader.close.assert_called_once();writer.close.assert_called_once()

    def test_role_change_cancels_old_writes_and_clears_frozen_status(self):
        controller=self.controller();controller.session=controller.session_provider()
        controller.role='host';controller.last={'freeze':{'confirmed':{'damage':9999}}}
        controller.role_monitor.snapshot.return_value=('guest',None)
        cancel=controller.cancel
        controller.worker=SimpleNamespace(mode=None)
        controller.poll()
        self.assertTrue(cancel.is_set())
        self.assertEqual(controller.role,'guest')
        self.assertNotIn('freeze',controller.last)

    def test_guest_or_unknown_role_cannot_open_a_writer_even_with_legacy_host_flag(self):
        from session_role import RoleLease
        for role in ('guest','unknown'):
            with self.subTest(role=role):
                session=SimpleNamespace(pid=123,created=456,path='game.exe')
                reader=Mock();factory=Mock(return_value=reader);lock=Mock()
                worker=StatsWorker(factory,lock);worker.hunt=Mock();worker.freezer=Mock()
                worker.hunt.plan.digest='abc'
                worker.hunt.step.return_value=dict(heroes=[dict(hero=1000,identity=91,count=1)],ambiguous=False,partial=False)
                worker.freezer.observe.return_value=(FieldProof('damage',0x200,'I',9999,((0x100,b'proof'),)),)
                worker.freezer.apply.return_value=dict(held={},confirmed={},errors={},values={'damage':7})
                policy=StatsPolicy();policy.freeze('damage',True)
                request=policy.request();request.update(host=True,role_lease=RoleLease(session,0x10000,0x20000))
                with patch.object(RoleLease,'reader',return_value=SimpleNamespace(read=lambda memory:role)):
                    result=worker.run(session,None,request,lambda:False)
                factory.assert_called_once_with(123,writable=False)
                self.assertTrue(worker.freezer.apply.call_args.kwargs['read_only'])
                self.assertTrue(result['host_required'])
                lock.assert_not_called()

    def test_find_waits_for_matching_connected_process(self):
        controller = self.controller()
        native = controller.native_provider()
        native.process.pid += 1
        controller.find()
        self.assertFalse(controller.policy.searching)
        native.process.pid -= 1
        controller.find()
        self.assertTrue(controller.policy.searching)
