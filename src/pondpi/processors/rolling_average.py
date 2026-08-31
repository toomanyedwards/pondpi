from pondpi.processors.base import LevelProcessor
from pondpi.rolling_average import RollingAverage


class RollingAverageProcessor(LevelProcessor):
    """Averages the raw reading over a rolling window."""

    def __init__(self, window_size):
        self._rolling_avg = RollingAverage(window_size)

    def add(self, raw_mm):
        return self._rolling_avg.add(raw_mm)

    def extra_state(self):
        return {"window_size": self._rolling_avg.window_size, "samples_in_window": self._rolling_avg.count}
