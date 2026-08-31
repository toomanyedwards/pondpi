from pondpi.signal_processors.base import LevelSignalProcessor


class RawSignalProcessor(LevelSignalProcessor):
    """Passes the raw reading through unchanged."""

    def add(self, raw_value):
        return raw_value
