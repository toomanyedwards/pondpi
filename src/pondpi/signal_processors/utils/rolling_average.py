from collections import deque


class RollingAverage:
    """Tracks the average of the last `window_size` values added."""

    def __init__(self, window_size):
        self.window_size = window_size
        self._readings = deque(maxlen=window_size)

    def add(self, value):
        self._readings.append(value)
        return self.average

    @property
    def average(self):
        if not self._readings:
            return None
        return sum(self._readings) / len(self._readings)

    @property
    def count(self):
        return len(self._readings)
