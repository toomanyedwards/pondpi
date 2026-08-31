from median_filter import MedianFilter
from rolling_average import RollingAverage


class LevelStrategy:
    """Base class for a level signal processing strategy.

    Subclasses take raw sensor readings (mm) one at a time via `add()` and
    return this strategy's current output (mm). `extra_state()` surfaces
    any strategy-specific metadata (window sizes, sample counts, ...) for
    the /level response.
    """

    def add(self, raw_mm):
        raise NotImplementedError

    def extra_state(self):
        return {}


class RawStrategy(LevelStrategy):
    """Passes the raw reading through unchanged."""

    def add(self, raw_mm):
        return raw_mm


class MedianStrategy(LevelStrategy):
    """Median-filters the raw reading."""

    def __init__(self, window_size):
        self._median = MedianFilter(window_size)

    def add(self, raw_mm):
        return self._median.add(raw_mm)

    def extra_state(self):
        return {"window_size": self._median.window_size, "samples_in_window": self._median.count}


class RollingAverageStrategy(LevelStrategy):
    """Averages the raw reading over a rolling window."""

    def __init__(self, window_size):
        self._rolling_avg = RollingAverage(window_size)

    def add(self, raw_mm):
        return self._rolling_avg.add(raw_mm)

    def extra_state(self):
        return {"window_size": self._rolling_avg.window_size, "samples_in_window": self._rolling_avg.count}


class MedianThenRollingAverageStrategy(LevelStrategy):
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


STRATEGY_TYPES = {
    "raw": RawStrategy,
    "median": MedianStrategy,
    "rolling_average": RollingAverageStrategy,
    "median_then_rolling_average": MedianThenRollingAverageStrategy,
}
