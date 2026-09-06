from pondpi.signals.base import LevelSignal


class RawSignal(LevelSignal):
    """Passes the raw reading through unchanged."""

    def add(self, raw_value):
        return raw_value
