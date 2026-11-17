"""Small, testable state helpers for background discovery."""
class RetrySchedule:
    def __init__(self):
        self.at = 0
        self.delay = 1

    def ready(self, now):
        return now >= self.at

    def failed(self, now):
        self.at = now + self.delay
        self.delay = min(self.delay * 2, 15)

    def reset(self):
        self.at = 0
        self.delay = 1


def same_session(expected, actual):
    return tuple(expected) == tuple(actual)
