from pondpi.signal_processors.base import LevelSignalProcessor


class ExponentialSmoothingSignalProcessor(LevelSignalProcessor):
    """Exponentially-weighted moving average: each new reading gets
    weight `alpha`, with every prior reading's weight decaying
    geometrically by (1 - alpha). Unlike a rolling window, there's no
    fixed window size -- older readings are never fully dropped, just
    weighted down forever."""

    def __init__(self, alpha):
        self._alpha = alpha
        self._value = None

    def add(self, raw_value):
        if self._value is None:
            self._value = raw_value
        else:
            self._value = self._alpha * raw_value + (1 - self._alpha) * self._value
        return self._value

    def extra_state(self):
        return {"alpha": self._alpha}
