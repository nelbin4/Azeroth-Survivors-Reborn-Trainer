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
        policy.set_target('crit',50)
        policy.set_target('echo',35)
        self.assertEqual(policy.request()['targets']['crit'],42)
        self.assertEqual(policy.request()['targets']['echo'],.35)
        for invalid in (-1,101,50.5,'nan','inf'):
            with self.assertRaises(ValueError):policy.set_target('crit',invalid)
        self.assertEqual(policy.targets['crit'],50)

    def test_auto_max_is_incremental_and_respects_per_stat_opt_out(self):
        policy=StatsPolicy()
        policy.accept('hero',{'damage':7})
        self.assertEqual(policy.holds,{'damage'})
        policy.set_enabled('damage',False)
        policy.accept('hero',{})
        policy.accept('hero',{'damage':9,'luck':3})
        self.assertEqual(policy.holds,{'luck'})

    def test_pause_stops_writes_but_preserves_search_and_hold_intent(self):
        policy=StatsPolicy();policy.searching=True
        policy.accept('hero',{'damage':7});policy.paused=True
        self.assertEqual(policy.request()['keys'],set())
        self.assertTrue(policy.request()['discover'])
        self.assertIn('damage',policy.holds)
        policy.paused=False
        self.assertIn('damage',policy.request()['keys'])

    def test_one_time_apply_and_stopping_search_do_not_force_holds(self):
        policy=StatsPolicy()
        policy.accept('hero',{'damage':7})
        policy.set_enabled('damage', False)
        policy.set_target('damage',123)
        self.assertNotIn('damage',policy.holds)
        self.assertIn('damage',policy.request()['keys'])
        self.assertFalse(policy.request()['discover'])
        policy.pending.clear()
        self.assertNotIn('damage',policy.request()['keys'])

    def test_preferences_round_trip_defaults_and_an_explicit_empty_selection(self):
        policy = StatsPolicy()
        self.assertEqual(policy.enabled, {'damage', 'cooldown', 'area', 'luck'})
        policy.set_enabled('damage', False)
        policy.set_target('crit', 51)
        policy.set_enabled('crit', True)
        reloaded = StatsPolicy(policy.preferences())
        self.assertEqual(reloaded.enabled, {'crit', 'cooldown', 'area', 'luck'})
        self.assertEqual(reloaded.targets['crit'], 51)
        empty = StatsPolicy({'enabled': [], 'targets': {'damage': -1, 'crit': True}})
        self.assertEqual(empty.enabled, set())
        self.assertEqual(empty.targets['damage'], 9999)
        self.assertEqual(empty.targets['crit'], 100)

    def test_readouts_match_display_targets_without_double_base(self):
        self.assertEqual(BY_KEY['crit'].display(92),'100%')
        self.assertEqual(BY_KEY['crit_damage'].display(9999),'10,049%')
        self.assertEqual(BY_KEY['echo'].display(.35),'35%')
        self.assertEqual(BY_KEY['health'].display(None),'—')

    def test_immediate_auto_targets_respect_search_pause_and_per_stat_opt_out(self):
        policy = StatsPolicy()
        self.assertFalse(policy.request()['auto_keys'])
        policy.searching = True
        policy.set_enabled('damage', False)
        self.assertNotIn('damage', policy.request()['auto_keys'])
        self.assertIn('luck', policy.request()['auto_keys'])
        policy.paused = True
        self.assertFalse(policy.request()['auto_keys'])
        policy.paused = False
        policy.searching = False
        self.assertFalse(policy.request()['auto_keys'])

    def test_checking_always_uses_maximum_and_cancels_pending_custom_value(self):
        policy = StatsPolicy()
        policy.accept('hero', {'crit': 3})
        policy.set_target('crit', 51)
        self.assertEqual(policy.request()['targets']['crit'], 43)
        policy.set_enabled('crit', True)
        self.assertNotIn('crit', policy.pending)
        self.assertIn('crit', policy.holds)
        self.assertEqual(policy.request()['targets']['crit'], 92)
        with self.assertRaises(ValueError):
            policy.set_target('crit', 50)
        policy.set_enabled('crit', False)
        self.assertEqual(policy.effective_target('crit'), 51)
        self.assertNotIn('crit', policy.request()['keys'])

    def test_stop_freeze_allows_explicit_one_time_apply(self):
        policy = StatsPolicy()
        policy.accept('hero', {'damage': 7, 'crit': 3})
        policy.paused = True
        policy.set_target('crit', 50)
        self.assertEqual(policy.request()['keys'], {'crit'})
        self.assertFalse(policy.request()['auto_keys'])

    def test_current_health_cannot_be_set_as_a_one_time_target(self):
        policy = StatsPolicy()
        with self.assertRaises(ValueError):
            policy.set_target('health', 500)
        self.assertEqual(policy.targets['health'], 1000000)
