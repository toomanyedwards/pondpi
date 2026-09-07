import threading
import time
from datetime import datetime, timezone


class Sensor:
    """Base class every sensor driver implements, one file per sensor
    type (e.g. `a02yyuw_sensor.py`), mirroring how `signals/` is
    structured.

    `read()` returns canonical readings -- distance from the sensor's
    mount point down to the water surface, in millimeters -- regardless
    of the underlying sensing technology. Different sensor hardware
    measures fundamentally different physical quantities (an ultrasonic
    sensor's raw distance vs. a resistive sensor's submerged length,
    with opposite sign conventions), so each driver is responsible for
    converting its own native reading into this shared unit before
    returning it; nothing downstream (signals, the HTTP API) needs to
    know which sensing technology produced a given value.

    A driver reports one or more named signals -- e.g. `A02YYUWSensor`
    reports "raw" and "processed", corresponding to the sensor's two
    hardware modes, while a simpler sensor might only ever report one.

    This class defines the *contract* every driver must satisfy --
    canonical readings, the capability flags below, and the health/reset
    tracking API -- but has no opinion on *how* a driver actually obtains
    a reading. Most drivers (e.g. `A02YYUWSensor`) poll continuously in
    their own background thread, but that's entirely their own
    implementation choice, not something this base class provides or
    assumes; a future driver that's push-driven instead (e.g. reacting to
    an async callback, never looping at all) is just as valid. Whatever
    the mechanism, a driver calls `_record_reading(readings)` (below)
    each time it obtains one or more, to participate in
    `last_reading_monotonic()`/`is_healthy()` and to populate
    `last_reading()`'s cache -- there's no push path out of a driver at
    all; a `reads_from_sensor` signal (see signals/sensor_signal.py)
    pulls from that cache on its own schedule instead.

    `supports_reset` is a capability flag: override it to True (and
    implement `reset_hardware()`) only if the underlying hardware can
    actually be power-cycled or otherwise reset in software. Callers
    must check it before calling `reset()`.

    `STALE_READING_THRESHOLD_S`/`is_healthy()` are this driver's own
    health check, the counterpart to `supports_reset`/`reset_hardware()`:
    a class attribute (default 3s, comfortably above a typical sensor's
    response time and default poll interval) any driver type can
    override with its own value, and a concrete method built on it.
    Callers (server.py's `/health`) rely entirely on `is_healthy()` for
    the ok/degraded verdict -- they hold no threshold and make no
    judgment call of their own about what counts as stale for a given
    sensor type.

    `read()` and `reset_hardware()` must be safe to call concurrently
    from different threads if a driver polls in its own background
    thread -- e.g. `A02YYUWSensor`'s own thread calls `read()`
    continuously while `POST /reset` calls `reset()` (which calls
    `reset_hardware()`) from a request-handling thread, with no
    synchronization at that layer. It's each driver's own responsibility
    to serialize its hardware access internally (e.g. a lock around
    whatever touches the physical connection) if a concurrent reset
    could otherwise corrupt or wedge an in-flight read.
    """

    supports_reset = False
    STALE_READING_THRESHOLD_S = 3.0

    def __init__(self):
        self._lock = threading.Lock()
        self._last_reading_monotonic = None
        self._last_reset_at = None
        self._last_readings = {}

    def read(self):
        """Returns a dict of {signal_name: distance_mm} for whichever
        signals produced a fresh valid reading since the last call, or
        an empty dict if nothing new is available this call. Must not
        block waiting for a frame -- called repeatedly from this
        driver's own background thread, if it has one."""
        raise NotImplementedError

    def reset_hardware(self):
        """Only implemented by `supports_reset = True` drivers: the
        hardware-specific action of power-cycling (or otherwise
        resetting) the physical sensor. Called by `reset()`."""
        raise NotImplementedError

    def reset(self):
        """Resets the hardware (`reset_hardware()`) and records this
        reset's own timestamp (`last_reset_at()`). A driver that owns a
        background loop should override this to stop that loop and wait
        for it to actually exit *before* calling `super().reset()`, then
        start a fresh one afterward -- so there's never a moment where
        the old loop could still be obtaining a reading against state
        that's mid-reset (see `A02YYUWSensor.reset()` for the pattern).

        Also clears `last_reading()`'s cache -- otherwise a signal
        pulling from this sensor would immediately re-read the stale
        pre-reset value and repopulate its own cache, defeating the
        point of a reset."""
        self.reset_hardware()
        with self._lock:
            self._last_reading_monotonic = None
            self._last_reset_at = datetime.now(timezone.utc)
            self._last_readings = {}

    def close(self):
        pass

    def last_reading_monotonic(self):
        """`time.monotonic()` timestamp of this driver's last recorded
        reading (any reading key, not just "raw"), or None before its
        first -- the raw fact `is_healthy()` is built on."""
        with self._lock:
            return self._last_reading_monotonic

    def last_reset_at(self):
        """`datetime` this driver was last `reset()`, or None if it
        never has been since construction."""
        with self._lock:
            return self._last_reset_at

    def last_reading(self, key):
        """Thread-safe snapshot `{"value": distance_mm, "at": ...}` of
        this driver's last recorded reading for one reading key (e.g.
        "raw"/"processed" -- see `read()`), or None before it's ever
        arrived. This is the only way a `reads_from_sensor` signal gets
        data out of a sensor -- it pulls this on its own schedule,
        there's no push path in the other direction."""
        with self._lock:
            return self._last_readings.get(key)

    def is_healthy(self):
        """True if no reading is expected yet (right after construction
        -- not itself a degraded condition), or if the last one arrived
        within `STALE_READING_THRESHOLD_S`. server.py's `/health` relies
        on this alone for its ok/degraded verdict."""
        last = self.last_reading_monotonic()
        if last is None:
            return True
        return (time.monotonic() - last) <= self.STALE_READING_THRESHOLD_S

    def _record_reading(self, readings):
        """Subclasses call this whenever they obtain one or more fresh
        readings -- however they get them (polling, a push callback,
        anything else) is entirely up to them. `readings` is a dict of
        `{reading_key: distance_mm}`, the same shape `read()` returns.
        Updates both the timestamp `last_reading_monotonic()`/
        `is_healthy()` are built on and the per-key cache `last_reading()`
        reads back."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._last_reading_monotonic = time.monotonic()
            for key, value in readings.items():
                self._last_readings[key] = {"value": value, "at": now_iso}
