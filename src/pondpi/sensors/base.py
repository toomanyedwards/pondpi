import threading
import time
from datetime import datetime, timezone


class LevelSensor:
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

    A driver's background read thread -- calling `read()` repeatedly and
    passing each reading to `on_reading` -- starts the moment it's
    constructed (`__init__` calls it as its last step; see below).
    There's no public `start()`/loop-control method: `reset()` is the
    only way to make it stop and start a fresh one.

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
    from different threads -- this driver's own background thread calls
    `read()` continuously while `POST /reset` calls `reset()` (which
    calls `reset_hardware()`) from a request-handling thread, with no
    synchronization at that layer. It's each driver's own responsibility
    to serialize its hardware access internally (e.g. a lock around
    whatever touches the physical connection) if a concurrent reset
    could otherwise corrupt or wedge an in-flight read.
    """

    supports_reset = False
    STALE_READING_THRESHOLD_S = 3.0

    def __init__(self, on_reading, poll_interval_s):
        """Every subclass must call this as the LAST line of its own
        `__init__`, once all of its own state (serial connection, mode
        controller, ...) is fully set up -- it immediately starts a
        background thread that calls `self.read()`, so nothing it
        depends on can still be uninitialized when that first call
        happens. `on_reading(reading_key, distance_mm)` is called once
        per reading `read()` produces -- e.g. sensor_config.py's
        `_build_on_reading()`, which owns deciding what a reading
        actually feeds; this class just supplies it with each one as it
        arrives, staying fully unaware of signals."""
        self._on_reading = on_reading
        self._poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        self._last_reading_monotonic = None
        self._last_reset_at = None
        self._begin_polling()

    def read(self):
        """Returns a dict of {signal_name: distance_mm} for whichever
        signals produced a fresh valid reading since the last call, or
        an empty dict if nothing new is available this call. Must not
        block waiting for a frame -- called repeatedly from this
        driver's own background thread."""
        raise NotImplementedError

    def reset_hardware(self):
        """Only implemented by `supports_reset = True` drivers: the
        hardware-specific action of power-cycling (or otherwise
        resetting) the physical sensor. Called by `reset()`, which also
        restarts this driver's own read thread around it."""
        raise NotImplementedError

    def reset(self):
        """Stops the current read thread and waits for it to actually
        exit *before* power-cycling the hardware (`reset_hardware()`)
        and starting a fresh thread -- so there's never a moment where
        the old thread could still call `read()`/`_on_reading()`
        against state we're in the middle of resetting. Also records
        this reset's own timestamp (`last_reset_at()`)."""
        self._stop_event.set()
        self._thread.join(timeout=self._poll_interval_s + 1)
        self.reset_hardware()
        with self._lock:
            self._last_reading_monotonic = None
            self._last_reset_at = datetime.now(timezone.utc)
        self._begin_polling()

    def close(self):
        pass

    def last_reading_monotonic(self):
        """`time.monotonic()` timestamp of this driver's last non-empty
        `read()` result (any reading key, not just "raw"), or None
        before its first -- the raw fact `is_healthy()` is built on."""
        with self._lock:
            return self._last_reading_monotonic

    def last_reset_at(self):
        """`datetime` this driver was last `reset()`, or None if it
        never has been since construction."""
        with self._lock:
            return self._last_reset_at

    def is_healthy(self):
        """True if no reading is expected yet (right after construction
        -- not itself a degraded condition), or if the last one arrived
        within `STALE_READING_THRESHOLD_S`. server.py's `/health` relies
        on this alone for its ok/degraded verdict."""
        last = self.last_reading_monotonic()
        if last is None:
            return True
        return (time.monotonic() - last) <= self.STALE_READING_THRESHOLD_S

    def _poll_loop(self):
        while not self._stop_event.is_set():
            readings = self.read()
            if readings:
                with self._lock:
                    self._last_reading_monotonic = time.monotonic()
            for reading_key, distance_mm in readings.items():
                self._on_reading(reading_key, distance_mm)
            time.sleep(self._poll_interval_s)

    def _begin_polling(self):
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
