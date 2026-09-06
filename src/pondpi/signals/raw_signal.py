from pondpi.signals.base import LevelSignal


class RawSignal(LevelSignal):
    """Passes the raw reading through unchanged. `sensor` names which
    configured sensor this signal reads from -- the only signal type
    that connects directly to a sensor; every other signal type gets
    its input from another named signal via `input:` instead (see
    signal_config.py)."""

    def __init__(self, sensor):
        self._sensor = sensor

    def add(self, raw_value):
        return raw_value

    def extra_state(self):
        return {"sensor": self._sensor}
