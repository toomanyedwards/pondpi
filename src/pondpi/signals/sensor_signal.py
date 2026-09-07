from typing import ClassVar

from pondpi.signals.base import Signal


class SensorSignal(Signal):
    """Passes the named sensor's raw reading through, converted into its
    own declared `unit`. `sensor` is which configured sensor this signal
    reads from -- one of the two signal types that connect directly to a
    sensor; every other signal type gets its input from another named
    signal instead (see signal_config.py). In the YAML both kinds of
    "where do I get data from" are spelled the same way, a top-level
    `source:` -- signal_config.py resolves what it names generically
    (another signal, or, for a `reads_from_sensor` type like this one, a
    sensor) and passes it into this constructor as `sensor`.

    Every other `Signal` type is genuinely unit-agnostic -- a pure
    numeric transform that inherits its unit from `source:` and never
    looks at it. `SensorSignal` is the deliberate one exception: it's
    the boundary where a sensor's canonical reading (always millimeters
    -- `Sensor.read()`'s fixed contract, see sensors/base.py) first
    enters the signal graph, so it's the one place that conversion needs
    to happen at all. Doing it here, once, is what lets everything
    downstream -- including every other signal type and server.py itself
    -- stay unaware that millimeters were ever involved.

    `reads_from_sensor = True` (see Signal) -- signal_config.py
    constructs this type with a `sensor_names` kwarg (the full set of
    configured sensor names) purely so `__init__` can validate `sensor`
    against it; it's not stored. Every other param (`sensor`, `unit`,
    `mode`) is validated here too -- `UNIT_DIVISORS`/`VALID_MODES` are
    this class's own declared single source of truth for what it
    supports, not something signal_config.py knows or checks itself.

    `mode` selects which of that sensor's named readings this signal is
    fed -- "raw" (default) or "processed", matching the reading keys a
    Sensor driver's `read()` can report (see sensors/base.py).
    Most drivers report both on their own schedule; a sensor signal
    only updates when its own `mode`'s reading arrives, independent of
    signals rooted at the other mode -- see sensor_config.py's
    `_build_on_reading()`.
    """

    reads_from_sensor = True
    UNIT_DIVISORS: ClassVar[dict] = {"cm": 10.0}
    VALID_MODES: ClassVar[tuple] = ("raw", "processed")

    def __init__(self, sensor_names, sensor=None, unit=None, mode="raw"):
        super().__init__()
        if sensor not in sensor_names:
            raise ValueError(f"invalid or missing 'source' '{sensor}' (expected one of {sorted(sensor_names)})")
        if not unit:
            raise ValueError("missing required 'unit'")
        if unit not in self.UNIT_DIVISORS:
            raise ValueError(f"invalid 'unit' '{unit}' (expected one of {sorted(self.UNIT_DIVISORS)})")
        if mode not in self.VALID_MODES:
            raise ValueError(f"invalid 'mode' '{mode}' (expected one of {self.VALID_MODES})")
        self._sensor = sensor
        self._unit = unit
        self._mode = mode

    @property
    def sensor(self):
        return self._sensor

    @property
    def unit(self):
        return self._unit

    @property
    def mode(self):
        return self._mode

    def add(self, raw_value):
        return raw_value / self.UNIT_DIVISORS[self._unit]

    def extra_state(self):
        return {"sensor": self._sensor, "mode": self._mode}
