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
        self.panel.select('crit');self.panel.target.set('50');self.panel.apply(True)
        self.assertEqual(self.panel.controller.policy.request()['targets']['crit'],42)
        self.assertIn('crit',self.panel.controller.policy.pending)
        self.assertIn(self.panel.table.set('crit','state'), ('Queued', 'Checking role'))
        self.panel.target.set('101');self.panel.apply(True)
        self.assertTrue(self.panel.error.get())
        self.assertEqual(self.panel.controller.policy.targets['crit'],50)

    def test_max_all_updates_editor_and_preserves_verification(self):
        self.panel.controller.policy.accept('hero',{'damage':7})
        self.panel.target.set('50');self.panel.apply(True)
        self.panel.max_all.invoke()
        self.assertEqual(self.panel.target.get(),'9999')
        self.assertEqual(self.panel.controller.policy.verified,{'damage'})

    def test_filter_preserves_table_data_and_selects_a_visible_row(self):
        self.panel.category.set('Resources')
        self.panel.filter_rows()
        self.assertEqual(self.panel.table.get_children(), ('run_gold', 'rerolls'))
        self.assertEqual(self.panel.selected, 'run_gold')
        self.panel.category.set('All')
        self.panel.filter_rows()
        self.assertEqual(len(self.panel.table.get_children()), 17)

    def test_host_indicator_tracks_verified_role(self):
        self.panel.controller.role = 'host'
        self.panel.refresh()
        self.assertIn('Host', self.panel.role_text.get())
        self.panel.controller.role = 'guest'
        self.panel.refresh()
        self.assertIn('read-only', self.panel.role_text.get())

    def test_apply_once_clears_an_existing_hold(self):
        policy = self.panel.controller.policy
        policy.accept('hero', {'damage': 7})
        self.assertIn('damage', policy.holds)
        self.panel.target.set('50')
        self.panel.apply(False)
        self.assertNotIn('damage', policy.holds)
        self.assertIn('damage', policy.pending)

    def test_periodic_refresh_updates_hold_without_overwriting_editor_choice(self):
        policy = self.panel.controller.policy
        policy.accept('hero', {'damage': 7})
        self.panel.refresh()
        self.assertTrue(self.panel.editor_frozen.get())
        self.panel.editor_check.invoke()
        self.panel.refresh()
        self.assertFalse(self.panel.editor_frozen.get())

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
        self.assertEqual(self.panel.table.set('damage', 'enabled'), '☑')
        self.panel.toggle_selected_enabled(None)
        self.assertEqual(self.panel.table.set('damage', 'enabled'), '☐')
        self.assertEqual(self.panel.table.set('damage', 'state'), 'Disabled')
        self.assertTrue(self.panel.apply_button.instate(['disabled']))
        self.panel.toggle_selected_enabled(None)
        self.assertEqual(self.panel.table.set('damage', 'enabled'), '☑')
        self.assertTrue(self.panel.apply_button.instate(['!disabled']))
