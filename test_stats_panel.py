import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest

from stats_panel import StatsPanel


class StatsPanelTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        ttk.Style(self.root).theme_use('clam')
        frame=ttk.Frame(self.root);frame.pack()
        session = SimpleNamespace(pid=123, created=456, path='game.exe')
        native = SimpleNamespace(process=SimpleNamespace(pid=123), created=456, path='game.exe')
        self.panel=StatsPanel(self.root,frame,lambda:session,lambda:native,lambda:False)

    def tearDown(self):
        self.panel.controller.close()
        self.panel.search_progress.stop()
        for callback in self.root.tk.call('after','info'):self.root.after_cancel(callback)
        self.root.destroy()

    def test_find_button_toggles_background_search(self):
        self.assertEqual(len(self.panel.table.get_children()),17)
        self.assertEqual(self.panel.find_button.cget('text'),'Find')
        self.assertFalse(self.panel.controller.policy.searching)
        self.panel.find_button.invoke()
        self.assertTrue(self.panel.controller.policy.searching)
        self.assertEqual(self.panel.find_button.cget('text'),'Stop search')
        self.panel.find_button.invoke()
        self.assertFalse(self.panel.controller.policy.searching)

    def test_selected_stat_editor_queues_validated_custom_values(self):
        self.panel.controller.policy.accept('hero',{'crit':92})
        self.panel.select('crit');self.panel.target.set('50');self.panel.apply()
        self.assertEqual(self.panel.controller.policy.request()['targets']['crit'],42)
        self.assertIn('crit',self.panel.controller.policy.pending)
        self.assertIn(self.panel.table.set('crit','state'), ('Queued', 'Checking role'))
        self.panel.target.set('101');self.panel.apply()
        self.assertTrue(self.panel.error.get())
        self.assertEqual(self.panel.controller.policy.targets['crit'],50)

    def test_checkbox_saves_and_applies_a_verified_stat_without_pressing_apply(self):
        saved = []
        self.panel.save_preferences = saved.append
        policy = self.panel.controller.policy
        policy.accept('hero', {'crit': 2})
        self.panel.select('crit')
        self.panel.toggle_selected_enabled(None)
        self.assertIn('crit', policy.request()['keys'])
        self.assertIn('crit', saved[-1]['enabled'])
        self.assertEqual(policy.request()['targets']['crit'], 92)
        self.panel.toggle_selected_enabled(None)
        self.assertNotIn('crit', policy.request()['keys'])
        self.assertNotIn('crit', saved[-1]['enabled'])

    def test_stop_freeze_pauses_writes_and_resume_restores_them(self):
        policy = self.panel.controller.policy
        policy.accept('hero', {'damage': 7})
        self.panel.refresh()
        self.assertEqual(self.panel.pause.cget('text'), 'Stop Freeze')
        self.panel.pause.invoke()
        self.assertEqual(policy.request()['keys'], set())
        self.assertEqual(self.panel.pause.cget('text'), 'Resume Freeze')
        self.panel.pause.invoke()
        self.assertIn('damage', policy.request()['keys'])

    def test_apply_once_clears_an_existing_hold(self):
        policy = self.panel.controller.policy
        policy.accept('hero', {'damage': 7})
        self.assertIn('damage', policy.holds)
        self.panel.toggle_selected_enabled(None)
        self.panel.target.set('50')
        self.panel.apply()
        self.assertNotIn('damage', policy.holds)
        self.assertIn('damage', policy.pending)

    def test_checked_stats_show_maximum_and_disable_manual_apply(self):
        self.panel.controller.policy.accept('hero', {'damage': 7})
        self.panel.refresh()
        self.assertEqual(self.panel.target.get(), '9999')
        self.assertTrue(self.panel.entry.instate(['disabled']))
        self.assertTrue(self.panel.apply_button.instate(['disabled']))

    def test_current_health_cannot_be_queued_for_apply(self):
        self.panel.controller.policy.accept('hero', {'health': 100, 'max_health': 1000000})
        self.panel.select('health')
        self.assertTrue(self.panel.apply_button.instate(['disabled']))

    def test_progress_tracks_search_role_check_completion_and_cancel(self):
        self.panel.find_button.invoke()
        self.assertTrue(self.panel.progress_running)
        self.assertIn('1–3 minutes', self.panel.search_hint.get())
        self.panel.controller.policy.accept('player', {'damage': 41})
        self.panel.refresh()
        self.assertTrue(self.panel.progress_running)
        self.assertIn('Stats found. Checking session role', self.panel.search_hint.get())
        self.panel.controller.role = 'none'
        self.panel.refresh()
        self.assertFalse(self.panel.progress_running)
        self.assertEqual(float(self.panel.search_progress['value']), 100)
        self.panel.find_button.invoke()
        self.assertEqual(self.panel.search_hint.get(), '')
        self.assertEqual(float(self.panel.search_progress['value']), 0)
        self.assertIsNone(self.panel.search_started)

    def test_toggle_checkbox_for_a_stat_before_hunt_finds_it(self):
        self.assertEqual(self.panel.table.item('damage', 'image'), (str(self.panel.checkbox_images[True]),))
        self.panel.toggle_selected_enabled(None)
        self.assertEqual(self.panel.table.item('damage', 'image'), (str(self.panel.checkbox_images[False]),))
        self.assertEqual(self.panel.table.set('damage', 'state'), 'Manual')
        self.assertTrue(self.panel.apply_button.instate(['!disabled']))
        self.panel.toggle_selected_enabled(None)
        self.assertEqual(self.panel.table.item('damage', 'image'), (str(self.panel.checkbox_images[True]),))
        self.assertTrue(self.panel.apply_button.instate(['disabled']))
        self.assertTrue(self.panel.controller.policy.searching)

    def test_advanced_opens_with_readable_detection_details(self):
        controller = self.panel.controller
        controller.role = 'host'
        controller.policy.set_enabled('echo', True)
        controller.policy.accept('hero', {'damage': 9999})
        controller.last = {
            'build': '950599aec161',
            'heroes': [{'name': 'Tester', 'level': 11}],
            'freeze': {'confirmed': {'damage': 9999}, 'errors': {'echo': 'Effect is not active.'}},
        }
        self.panel.open_advanced()
        tabs = self.panel.advanced.winfo_children()[0]
        self.assertEqual(tabs.tab(tabs.select(), 'text'), 'Detection details')
        self.assertEqual(tabs.index(tabs.select()), 0)
        self.assertEqual([tabs.tab(tab, 'text') for tab in tabs.tabs()], ['Detection details', 'Manual value search'])
        content = self.panel.diagnostics.get('1.0', 'end-1c')
        self.assertIn('Game process: PID 123', content)
        self.assertIn('Character: Tester · Level 11', content)
        self.assertRegex(content, r'Damage bonus\s+9,999%\s+9,999%\s+Frozen')
        self.assertIn('Echo chance: Effect is not active.', content)
        self.panel.advanced.destroy()
