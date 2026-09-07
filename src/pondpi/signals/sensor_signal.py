from typing import ClassVar

from pondpi.signals.base import LevelSignal


class SensorSignal(LevelSignal):
    """Passes the named sensor's raw reading through, converted into its
    own declared `unit`. `sensor` names which configured sensor this
    signal reads from -- one of the two signal types that connect
    directly to a sensor; every other signal type gets its input from
    another named signal via `input:` instead (see signal_config.py).

    Every other `LevelSignal` type is genuinely unit-agnostic -- a pure
    numeric transform that inherits its unit from `input:` and never
    looks at it. `SensorSignal` is the deliberate one exception: it's
    the boundary where a sensor's canonical reading (always millimeters
    -- `LevelSensor.read()`'s fixed contract, see sensors/base.py) first
    enters the signal graph, so it's the one place that conversion needs
    to happen at all. Doing it here, once, is what lets everything
    downstream -- including every other signal type and server.py itself
    -- stay unaware that millimeters were ever involved.

    `UNIT_DIVISORS` is the single source of truth for which units this
    type supports (`signal_config.py` validates `params.unit` against it
    before construction) and how to convert into each one from the
    sensor's raw millimeter reading.

    `mode` selects which of that sensor's named readings this signal is
    fed -- "raw" (default) or "processed", matching the reading keys a
    LevelSensor driver's `read()` can report (see sensors/base.py).
    Most drivers report both on their own schedule; a sensor signal
    only updates when its own `mode`'s reading arrives, independent of
    signals rooted at the other mode -- see sensor_config.py's
    `_build_on_reading()`.
    """

    UNIT_DIVISORS: ClassVar[dict] = {"cm": 10.0}

    def __init__(self, sensor, unit, mode="raw"):
        super().__init__()
        self._sensor = sensor
        self._unit = unit
        self._mode = mode

    def add(self, raw_value):
        return raw_value / self.UNIT_DIVISORS[self._unit]

    def extra_state(self):
        return {"sensor": self._sensor, "mode": self._mode}
