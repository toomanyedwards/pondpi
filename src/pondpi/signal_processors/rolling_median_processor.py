from pondpi.signal_processors.base import LevelSignalProcessor
from pondpi.signal_processors.utils.rolling_median_filter import RollingMedianFilter


class RollingMedianSignalProcessor(LevelSignalProcessor):
    """Median-filters the raw reading over a rolling window."""

    def __init__(self, window_size):
        self._rolling_median = RollingMedianFilter(window_size)

    def add(self, raw_value):
        return self._rolling_median.add(raw_value)

    def extra_state(self):
        return {"window_size": self._rolling_median.window_size, "samples_in_window": self._rolling_median.count}
