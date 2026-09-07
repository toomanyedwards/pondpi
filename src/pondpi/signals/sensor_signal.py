from pondpi.signals.base import LevelSignal


class SensorSignal(LevelSignal):
    """Passes the named sensor's raw reading through unchanged. `sensor`
    names which configured sensor this signal reads from -- one of the
    two signal types that connect directly to a sensor; every other
    signal type gets its input from another named signal via `input:`
    instead (see signal_config.py).

    `mode` selects which of that sensor's named readings this signal is
    fed -- "raw" (default) or "processed", matching the reading keys a
    LevelSensor driver's `read()` can report (see sensors/base.py).
    Most drivers report both on their own schedule; a sensor signal
    only updates when its own `mode`'s reading arrives, independent of
    signals rooted at the other mode -- see poll_sensor() in server.py.
    """

    def __init__(self, sensor, mode="raw"):
        self._sensor = sensor
        self._mode = mode

    def add(self, raw_value):
        return raw_value

    def extra_state(self):
        return {"sensor": self._sensor, "mode": self._mode}
