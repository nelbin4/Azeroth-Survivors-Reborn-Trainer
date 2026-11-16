"""Small, testable state helpers for background discovery."""
RUN_OPTION_DEFAULTS = {'timescale': 3, 'clock': 20, 'rift_clock': True}


class RunOptionDefaults:
    """Apply launch defaults once per process identity, preserving later edits."""
    def __init__(self):
        self.sessions = []

    def apply(self, trainer):
        identity = tuple(trainer.identity())
        if identity in self.sessions:
            return False
        for key, value in RUN_OPTION_DEFAULTS.items():
            trainer.set(key, value)
        self.sessions.append(identity)
        self.sessions = self.sessions[-32:]
        return True


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
