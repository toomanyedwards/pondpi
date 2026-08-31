from pondpi.signal_processors.base import LevelSignalProcessor
from pondpi.signal_processors.utils.rolling_average import RollingAverage


class RollingAverageSignalProcessor(LevelSignalProcessor):
    """Averages the raw reading over a rolling window."""

    def __init__(self, window_size):
        self._rolling_avg = RollingAverage(window_size)

    def add(self, raw_value):
        return self._rolling_avg.add(raw_value)

    def extra_state(self):
        return {"window_size": self._rolling_avg.window_size, "samples_in_window": self._rolling_avg.count}
