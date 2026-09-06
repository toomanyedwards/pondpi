from pondpi.signals.base import LevelSignal
from pondpi.signals.utils.rolling_average import RollingAverage


class RollingAverageSignal(LevelSignal):
    """Averages the raw reading over a rolling window."""

    def __init__(self, window_size):
        self._rolling_avg = RollingAverage(window_size)

    def add(self, raw_value):
        return self._rolling_avg.add(raw_value)

    def extra_state(self):
        return {"window_size": self._rolling_avg.window_size, "samples_in_window": self._rolling_avg.count}
