import threading
from datetime import datetime, timezone


class LevelSignal:
    """Base class for a level signal.

    Most subclasses take raw sensor readings one at a time via `add()`
    (pure computation, no caching) -- composable via `input:` in
    config/sensors.yaml, fed by the sensor-to-signal wiring built in
    sensor_config.py, which calls `feed()` (not `add()` directly) as
    each reading arrives. Signals are unit-agnostic — they don't know
    or care whether the values they're passed are mm, cm, or anything
    else; unit conversion happens at the HTTP layer in server.py.

    `feed()`/`current()` are concrete on this base class and shared by
    every signal type, `owns_read_loop` or not: `feed()` computes (via
    `add()`) and thread-safely caches this signal's new output;
    `current()` reads that cache back as `{"value", "at",
    **extra_state()}`, or None before the first `feed()`. This is what
    lets a signal like RollingAverageSignal read its `input:` signal's
    live value directly (`input_signal.current()`), with no shared
    state in server.py mediating it.

    `owns_read_loop` is a capability flag, same pattern as
    `LevelSensor.supports_reset`: override it to True only for a signal
    type that maintains its own background thread instead of being fed
    via `feed()` from the sensor's own read loop (see
    RollingAverageSignal, which samples its `input:` signal's `current()`
    on its own schedule rather than being pushed a new one every poll
    tick). Such a type constructs and starts its own thread the moment
    it's initialized -- there's no separate `start()` to call, and no
    public loop-control API at all; server.py has no involvement in how
    it runs itself.
    """

    owns_read_loop = False

    def __init__(self):
        self._lock = threading.Lock()
        self._value = None
        self._at = None

    def add(self, raw_value):
        raise NotImplementedError

    def extra_state(self):
        return {}

    def feed(self, raw_value):
        """Computes this signal's new output via `add()` and caches it
        (thread-safely) as its current value -- the uniform entry point
        anything driving this signal calls, whether that's the sensor's
        own read loop (via sensor_config.py's wiring) or, for an
        `owns_read_loop` signal, its own background thread. Returns the
        raw computed value, so a downstream `input:`-chained signal can
        be fed the same call's result without an extra `current()`
        round trip."""
        value = self.add(raw_value)
        with self._lock:
            self._value = value
            self._at = datetime.now(timezone.utc).isoformat()
        return value

    def current(self):
        """Thread-safe snapshot of this signal's current output --
        `{"value", "at", **extra_state()}` -- or None before its first
        `feed()`."""
        with self._lock:
            if self._value is None:
                return None
            return {"value": self._value, "at": self._at, **self.extra_state()}

    def reset(self):
        """Clears this signal back to its just-constructed state: no
        cached current() value, and (via `_reset_state()`) any
        subclass-specific accumulator (a rolling window, an EMA) reset
        to empty. An `owns_read_loop` type additionally restarts its
        own background thread -- see RollingAverageSignal.reset()."""
        with self._lock:
            self._value = None
            self._at = None
        self._reset_state()

    def _reset_state(self):
        """Hook for a subclass holding its own accumulator to clear it
        on reset(). No-op by default, for types with no state beyond
        the cached current() value the base class already owns."""
