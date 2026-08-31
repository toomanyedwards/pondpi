from pondpi.processors.base import LevelProcessor


class RawProcessor(LevelProcessor):
    """Passes the raw reading through unchanged."""

    def add(self, raw_value):
        return raw_value
