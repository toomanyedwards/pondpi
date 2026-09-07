import time

from pondpi.signals.base import LevelSignal
from pondpi.signals.utils.rolling_average import RollingAverage


class PollingRollingAverageSignal(LevelSignal):
    """Averages over a rolling window, like RollingAverageSignal, but only
    actually admits a new sample into that window once `poll_interval_s`
    has elapsed since the last one it accepted -- calls in between just
    return the current average unchanged, without touching the window.

    Signals have no timer of their own; they're purely synchronous
    transforms driven by poll_sensor()'s loop (see server.py), which
    calls `add()` every time a matching reading arrives -- normally every
    `poll_interval_s` (the sensor's own, e.g. ~150ms). This lets one
    signal's window represent a longer real-world time span at a coarser,
    independently-configured cadence, without changing how often the
    sensor itself is actually read.
    """

    def __init__(self, window_size, poll_interval_s):
        self._rolling_avg = RollingAverage(window_size)
        self._poll_interval_s = poll_interval_s
        self._last_accepted_monotonic = None

    def add(self, raw_value):
        now = time.monotonic()

        if self._last_accepted_monotonic is None or (now - self._last_accepted_monotonic) >= self._poll_interval_s:
            self._last_accepted_monotonic = now
            return self._rolling_avg.add(raw_value)

        return self._rolling_avg.average

    def extra_state(self):
        return {
            "window_size": self._rolling_avg.window_size,
            "samples_in_window": self._rolling_avg.count,
            "poll_interval_s": self._poll_interval_s,
        }
