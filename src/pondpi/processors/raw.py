from pondpi.processors.base import LevelProcessor


class RawProcessor(LevelProcessor):
    """Passes the raw reading through unchanged."""

    def add(self, raw_mm):
        return raw_mm
