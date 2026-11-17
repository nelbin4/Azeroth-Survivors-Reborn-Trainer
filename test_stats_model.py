import unittest
from stats_model import StatsPolicy,BY_KEY


class StatsPolicyTests(unittest.TestCase):
    def test_find_is_explicit_and_targets_are_in_display_units(self):
        policy=StatsPolicy()
        self.assertFalse(policy.searching)
        self.assertEqual(policy.request()['keys'],set())
        self.assertEqual(policy.request()['targets']['crit'],92)
        self.assertEqual(policy.request()['targets']['crit_damage'],9999)
        self.assertEqual(policy.request()['targets']['echo'],1)

    def test_custom_targets_keep_concrete_native_units_and_validate(self):
        policy=StatsPolicy()
        policy.set_target('crit',50,True)
        policy.set_target('echo',35,True)
        self.assertEqual(policy.request()['targets']['crit'],42)
        self.assertEqual(policy.request()['targets']['echo'],.35)
        for invalid in (-1,101,50.5,'nan','inf'):
            with self.assertRaises(ValueError):policy.set_target('crit',invalid)
        self.assertEqual(policy.targets['crit'],50)

    def test_auto_max_is_incremental_and_respects_per_stat_opt_out(self):
        policy=StatsPolicy()
        policy.accept('hero',{'damage':7})
        self.assertEqual(policy.holds,{'damage'})
        policy.freeze('damage',False)
        policy.accept('hero',{})
        policy.accept('hero',{'damage':9,'armor':3})
        self.assertEqual(policy.holds,{'armor'})

    def test_pause_stops_writes_but_preserves_search_and_hold_intent(self):
        policy=StatsPolicy();policy.searching=True
        policy.accept('hero',{'damage':7});policy.paused=True
        self.assertEqual(policy.request()['keys'],set())
        self.assertTrue(policy.request()['discover'])
        self.assertIn('damage',policy.holds)
        policy.paused=False
        self.assertIn('damage',policy.request()['keys'])

    def test_one_time_apply_and_stopping_search_do_not_force_holds(self):
        policy=StatsPolicy();policy.auto=False
        policy.accept('hero',{'damage':7});policy.set_target('damage',123)
        self.assertNotIn('damage',policy.holds)
        self.assertIn('damage',policy.request()['keys'])
        self.assertFalse(policy.request()['discover'])
        policy.pending.clear()
        self.assertNotIn('damage',policy.request()['keys'])

    def test_max_all_resets_custom_targets_without_unverifying_fields(self):
        policy=StatsPolicy();policy.auto=False;policy.accept('hero',{'damage':7})
        policy.set_target('damage',100);policy.paused=True;policy.maximize()
        self.assertEqual(policy.targets['damage'],9999)
        self.assertIn('damage',policy.pending)
        self.assertEqual(policy.verified,{'damage'})
        self.assertFalse(policy.paused)

    def test_readouts_match_display_targets_without_double_base(self):
        self.assertEqual(BY_KEY['crit'].display(92),'100%')
        self.assertEqual(BY_KEY['crit_damage'].display(9999),'10,049%')
        self.assertEqual(BY_KEY['echo'].display(.35),'35%')
        self.assertEqual(BY_KEY['health'].display(None),'—')

    def test_immediate_auto_targets_respect_search_pause_and_per_stat_opt_out(self):
        policy = StatsPolicy()
        self.assertFalse(policy.request()['auto_keys'])
        policy.searching = True
        policy.freeze('damage', False)
        self.assertNotIn('damage', policy.request()['auto_keys'])
        self.assertIn('crit', policy.request()['auto_keys'])
        policy.paused = True
        self.assertFalse(policy.request()['auto_keys'])
        policy.paused = False
        policy.auto = False
        self.assertFalse(policy.request()['auto_keys'])

    def test_disabled_stat_is_excluded_from_auto_bulk_and_queued_writes(self):
        policy = StatsPolicy()
        policy.searching = True
        policy.accept('hero', {'damage': 7, 'armor': 3})
        policy.set_target('damage', 125, freeze=True)
        policy.set_enabled('damage', False)
        policy.maximize()
        request = policy.request()
        self.assertEqual(policy.targets['damage'], 125)
        self.assertEqual(policy.values['damage'], 7)
        self.assertNotIn('damage', request['keys'] | request['auto_keys'] | request['pending'])
        self.assertIn('armor', request['keys'])
        with self.assertRaises(ValueError):
            policy.set_target('damage', 50)
        policy.set_enabled('damage', True)
        self.assertIn('damage', policy.request()['keys'])
