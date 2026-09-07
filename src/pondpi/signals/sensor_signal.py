from typing import ClassVar

from pondpi.signals.base import Signal


class SensorSignal(Signal):
    """Pulls the named sensor's last known reading and converts it into
    its own declared `unit`. `sensor` is which configured sensor this
    signal reads from -- one of the two signal types that connect
    directly to a sensor; every other signal type pulls from another
    named signal instead (see signal_config.py). In the YAML both kinds
    of "where do I get data from" are spelled the same way, a top-level
    `source:` -- signal_config.py resolves what it names generically
    (another signal, or, for a `reads_from_sensor` type like this one, a
    sensor) and passes it into this constructor as `sensor`, alongside
    `sensor_objects` (a `dict[name -> Sensor instance]`) so this class
    can hold onto the live object it needs to actually pull from.

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
    constructs this type with a `sensor_objects` kwarg (every configured
    sensor, by name) purely so `__init__` can validate `sensor` against
    it and hold onto the one it names; `sensor_objects` itself isn't
    stored. Every other param (`sensor`, `unit`, `mode`) is validated
    here too -- `UNIT_DIVISORS`/`VALID_MODES` are this class's own
    declared single source of truth for what it supports, not something
    signal_config.py knows or checks itself.

    `mode` selects which of that sensor's named readings this signal
    pulls -- "raw" (default) or "processed", matching the reading keys a
    Sensor driver can report. `_pull_source()` (below) passes this
    signal's own settings (just `{"mode": self._mode}` -- `unit` isn't
    the sensor's concern) into `Sensor.read()`, the caller-settings-
    driven entry point every sensor exposes (see sensors/base.py) --
    each mode updates independently, on whatever schedule the sensor
    itself keeps that reading fresh, so this signal only ever sees its
    own `mode`'s reading, independent of signals rooted at the other.
    """

    reads_from_sensor = True
    UNIT_DIVISORS: ClassVar[dict] = {"cm": 10.0}
    VALID_MODES: ClassVar[tuple] = ("raw", "processed")

    def __init__(self, sensor_objects, sensor=None, unit=None, mode="raw"):
        super().__init__()
        if sensor not in sensor_objects:
            raise ValueError(f"invalid or missing 'source' '{sensor}' (expected one of {sorted(sensor_objects)})")
        if not unit:
            raise ValueError("missing required 'unit'")
        if unit not in self.UNIT_DIVISORS:
            raise ValueError(f"invalid 'unit' '{unit}' (expected one of {sorted(self.UNIT_DIVISORS)})")
        if mode not in self.VALID_MODES:
            raise ValueError(f"invalid 'mode' '{mode}' (expected one of {self.VALID_MODES})")
        self._sensor = sensor
        self._sensor_obj = sensor_objects[sensor]
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

    def _pull_source(self):
        return self._sensor_obj.read({"mode": self._mode})

    def add(self, raw_value):
        return raw_value / self.UNIT_DIVISORS[self._unit]

    def extra_state(self):
        return {"sensor": self._sensor, "mode": self._mode}
