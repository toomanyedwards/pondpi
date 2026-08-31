from collections import deque


class MedianFilter:
    """Tracks the median of the last `window_size` values added."""

    def __init__(self, window_size=5):
        self.window_size = window_size
        self._readings = deque(maxlen=window_size)

    def add(self, value):
        self._readings.append(value)
        return self.median

    @property
    def median(self):
        if not self._readings:
            return None

        ordered = sorted(self._readings)
        mid = len(ordered) // 2

        if len(ordered) % 2 == 1:
            return ordered[mid]

        return (ordered[mid - 1] + ordered[mid]) / 2

    @property
    def count(self):
        return len(self._readings)
