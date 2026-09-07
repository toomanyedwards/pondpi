import threading
import time
from datetime import datetime, timezone


class Sensor:
    """Base class every sensor driver implements, one file per sensor
    type (e.g. `a02yyuw_sensor.py`), mirroring how `signals/` is
    structured.

    A driver's canonical readings -- distance from the sensor's mount
    point down to the water surface, in millimeters, regardless of the
    underlying sensing technology -- are always in millimeters by the
    time a caller gets them back from `read()`; different sensor
    hardware measures fundamentally different physical quantities (an
    ultrasonic sensor's raw distance vs. a resistive sensor's submerged
    length, with opposite sign conventions), so each driver is
    responsible for converting its own native reading into this shared
    unit before returning it -- nothing downstream (signals, the HTTP
    API) needs to know which sensing technology produced a given value.

    A driver may report one or more named readings -- e.g.
    `A02YYUWSensor` reports "raw" and "processed", corresponding to the
    sensor's two hardware modes, while a simpler sensor might only ever
    report one. This base class has no notion of that shape at all
    (not every driver necessarily needs more than one, or needs to
    *cache* readings by key the way `A02YYUWSensor` does) -- `read()`
    (below) is each driver's own required, driver-specific way of
    retrieving whatever it reports; how (or whether) it caches anything
    to answer that is entirely up to it.

    This class defines the *contract* every driver must satisfy --
    the capability flags below, and the health/reset tracking API -- but
    has no opinion on *how* a driver actually obtains a reading. Most
    drivers (e.g. `A02YYUWSensor`) poll continuously in their own
    background thread, but that's entirely their own implementation
    choice, not something this base class provides or assumes; a future
    driver that's push-driven instead (e.g. reacting to an async
    callback, never looping at all) is just as valid. Whatever the
    mechanism, a driver calls `_record_reading()` (below) each time it
    obtains one, to participate in `last_reading_monotonic()` -- the one
    piece of health-adjacent bookkeeping generic enough to live here,
    since every driver (regardless of how many named readings it has, or
    how it caches them) can meaningfully answer "when did I last get
    anything at all".

    `supports_reset` is a capability flag: override it to True (and
    implement `reset_hardware()`) only if the underlying hardware can
    actually be power-cycled or otherwise reset in software. Callers
    must check it before calling `reset()`.

    `is_healthy()` is concrete but holds no policy of its own -- it just
    calls `check_health()`, a required method every driver must
    implement, since what "healthy" means (a staleness threshold? a
    presence check? something hardware-specific?) can vary wildly by
    sensor type and this base class has no business guessing at a
    one-size-fits-all default. Callers (server.py's `/health`) rely
    entirely on `is_healthy()` for the ok/degraded verdict -- they hold
    no threshold and make no judgment call of their own.

    `read()` itself is not required to touch hardware at all --
    `A02YYUWSensor`, for instance, does the actual UART polling in its
    own private `_read_hardware()` instead, called from its own
    background thread, and `read()` is just a lookup against its own
    cache. Whatever internal method *does* touch hardware must be safe
    to call concurrently with `reset_hardware()` if it runs on its own
    background thread -- e.g. `A02YYUWSensor`'s thread calls
    `_read_hardware()` continuously while `POST /reset` calls `reset()`
    (which calls `reset_hardware()`) from a request-handling thread,
    with no synchronization at that layer. It's each driver's own
    responsibility to serialize its hardware access internally (e.g. a
    lock around whatever touches the physical connection) if a
    concurrent reset could otherwise corrupt or wedge an in-flight read.
    """

    supports_reset = False

    def __init__(self):
        self._lock = threading.Lock()
        self._last_reading_monotonic = None
        self._last_reset_at = None

    def read(self, options=None):
        """Returns `{"value": distance_mm, "at": ...}` for whatever
        `options` selects, or None if nothing's available yet. Must
        never block. Required override -- entirely driver-specific in
        both meaning and mechanism: nothing about how (or whether) a
        driver caches readings to answer this lives on this base class.

        `options` is the *caller's* own `source.options` -- e.g. a
        `reads_from_sensor` Signal's own `source:` mapping from
        config/sensors.yaml, passed straight through unmodified (see
        signal_config.py/sensor_signal.py) -- not this driver's
        construction settings, which it already has on `self`. It's
        optional (None for a caller with no options of its own, or that
        doesn't care) and entirely driver-specific in meaning: a driver
        that reports more than one named reading (like
        `A02YYUWSensor`'s "raw"/"processed") looks for whatever key in
        `options` picks one; a driver with only ever one reading can
        ignore `options` entirely.
        """
        raise NotImplementedError

    def reset_hardware(self):
        """Only implemented by `supports_reset = True` drivers: the
        hardware-specific action of power-cycling (or otherwise
        resetting) the physical sensor. Called by `reset()`."""
        raise NotImplementedError

    def check_health(self):
        """Required override backing `is_healthy()` (below) -- returns
        True/False for whatever "healthy" means to this specific driver.
        This base class holds no default or shared policy (not even a
        staleness threshold) since that can vary wildly by sensor type;
        see `A02YYUWSensor.check_health()` for one driver's own
        definition."""
        raise NotImplementedError

    def reset(self):
        """Resets the hardware (`reset_hardware()`) and records this
        reset's own timestamp (`last_reset_at()`). A driver that owns a
        background loop should override this to stop that loop and wait
        for it to actually exit *before* calling `super().reset()`, then
        start a fresh one afterward -- so there's never a moment where
        the old loop could still be obtaining a reading against state
        that's mid-reset (see `A02YYUWSensor.reset()` for the pattern).

        Only clears the generic `last_reading_monotonic()` bookkeeping
        this base class owns -- a driver with its own reading cache
        (e.g. `A02YYUWSensor`'s) must clear that itself too, in its own
        `reset()` override, or a signal pulling from it would
        immediately re-read the stale pre-reset value."""
        self.reset_hardware()
        with self._lock:
            self._last_reading_monotonic = None
            self._last_reset_at = datetime.now(timezone.utc)

    def close(self):
        pass

    def last_reading_monotonic(self):
        """`time.monotonic()` timestamp of this driver's last recorded
        reading, or None before its first -- updated by
        `_record_reading()` (below) every time a driver gets one,
        regardless of how many named readings it reports or how (if at
        all) it caches them. `GET /health`'s `last_reading_age_s` is
        built on this; `is_healthy()` is not -- see `check_health()`."""
        with self._lock:
            return self._last_reading_monotonic

    def last_reset_at(self):
        """`datetime` this driver was last `reset()`, or None if it
        never has been since construction."""
        with self._lock:
            return self._last_reset_at

    def is_healthy(self):
        """Delegates entirely to `check_health()` (above) -- this base
        class holds no health policy of its own. server.py's `/health`
        relies on this alone for its ok/degraded verdict."""
        return self.check_health()

    def _record_reading(self):
        """Subclasses call this whenever they obtain a fresh reading --
        however they get it (polling, a push callback, anything else)
        is entirely up to them, and however many named readings they
        report (or how they cache them, if at all) is their own
        business too. Just bumps `last_reading_monotonic()` -- the one
        piece of bookkeeping every driver participates in the same way,
        regardless of its own reading shape."""
        with self._lock:
            self._last_reading_monotonic = time.monotonic()
