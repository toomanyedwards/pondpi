from pondpi.signals.base import Signal


class ExponentialSmoothingSignal(Signal):
    """Exponentially-weighted moving average: each new reading gets
    weight `alpha`, with every prior reading's weight decaying
    geometrically by (1 - alpha). Unlike a rolling window, there's no
    fixed window size -- older readings are never fully dropped, just
    weighted down forever."""

    def __init__(self, alpha):
        super().__init__()
        self._alpha = alpha
        self._ema_value = None

    def add(self, raw_value):
        if self._ema_value is None:
            self._ema_value = raw_value
        else:
            self._ema_value = self._alpha * raw_value + (1 - self._alpha) * self._ema_value
        return self._ema_value

    def extra_state(self):
        return {"alpha": self._alpha}

    def _reset_state(self):
        self._ema_value = None
