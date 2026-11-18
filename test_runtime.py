import unittest
from runtime import RetrySchedule, same_session


class RuntimeTests(unittest.TestCase):
    def test_failures_back_off_and_reset_after_reconnect(self):
        retry = RetrySchedule()
        now = 10
        for delay in (1, 2, 4, 8, 15, 15):
            self.assertTrue(retry.ready(now))
            retry.failed(now)
            self.assertFalse(retry.ready(now + delay - 0.1))
            self.assertTrue(retry.ready(now + delay))
            now += delay
        retry.reset()
        self.assertTrue(retry.ready(now))
        retry.failed(now)
        self.assertTrue(retry.ready(now + 1))

    def test_discovery_from_previous_process_or_build_is_stale(self):
        active = (123, 456, 1000, 'build-a')
        self.assertTrue(same_session(active, list(active)))
        for other in ((124, 456, 1000, 'build-a'), (123, 457, 1000, 'build-a'),
                      (123, 456, 1001, 'build-a'), (123, 456, 1000, 'build-b')):
            self.assertFalse(same_session(active, other))
