from pondpi.median_filter import MedianFilter
from pondpi.signal_processors.base import LevelSignalProcessor


class MedianSignalProcessor(LevelSignalProcessor):
    """Median-filters the raw reading."""

    def __init__(self, window_size):
        self._median = MedianFilter(window_size)

    def add(self, raw_value):
        return self._median.add(raw_value)

    def extra_state(self):
        return {"window_size": self._median.window_size, "samples_in_window": self._median.count}
