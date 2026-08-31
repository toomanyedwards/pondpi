from pondpi.median_filter import MedianFilter
from pondpi.processors.base import LevelProcessor
from pondpi.rolling_average import RollingAverage


class MedianThenRollingAverageProcessor(LevelProcessor):
    """Median-filters the raw reading, then averages the result over a rolling window."""

    def __init__(self, median_window_size, rolling_window_size):
        self._median = MedianFilter(median_window_size)
        self._rolling_avg = RollingAverage(rolling_window_size)

    def add(self, raw_mm):
        median_mm = self._median.add(raw_mm)
        return self._rolling_avg.add(median_mm)

    def extra_state(self):
        return {
            "median_window_size": self._median.window_size,
            "rolling_window_size": self._rolling_avg.window_size,
            "samples_in_rolling_window": self._rolling_avg.count,
        }
