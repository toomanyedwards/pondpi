import threading


class Signal:
    """Base class for a signal -- a named, composable processing step
    over a stream of numeric readings, with no notion of what those
    readings physically represent.

    Nothing is ever pushed into a signal. `read()` (concrete, shared by
    every signal type) is the sole way anything -- another signal via
    `source:`, server.py, this signal's own `owns_read_loop` background
    thread's caller -- ever gets this signal's value: it pulls
    `_pull_source()` (also concrete, see below) and, if that's newer
    than the last value this signal already incorporated, computes a
    fresh one via `add()` (the one hook every subclass implements --
    pure computation, no caching of its own) and caches it. Calling
    `read()` any number of times with no new upstream data is safe and
    cheap -- `add()` only runs when there's genuinely something new, so
    a stateful accumulator (a rolling window, an EMA) is never
    double-fed by two callers polling in quick succession. Every signal
    type except `sensor` is unit-agnostic -- they don't know or care
    whether the values they're passed are mm, cm, or anything else;
    `SensorSignal` is the one deliberate exception, converting once at
    the boundary where a sensor's canonical millimeter reading first
    enters the graph (see signals/sensor_signal.py).

    `_pull_source()`'s default implementation pulls `self._source_signal`
    (a live `Signal` instance passed into `__init__`)'s own `read()` --
    this covers every chain type for free, purely through inheritance.
    `SensorSignal` is the one type that overrides it, to pull from its
    sensor instead of another signal (see there).

    `owns_read_loop` is a capability flag, same pattern as
    `Sensor.supports_reset`: override it to True only for a signal type
    that maintains its own background thread instead of computing
    lazily at `read()` time (see RollingAverageSignal, which samples its
    `source:` signal's `read()` on its own schedule rather than
    recomputing every time something asks). Such a type constructs and
    starts its own thread the moment it's initialized -- there's no
    separate `start()` to call, and no public loop-control API at all;
    server.py has no involvement in how it runs itself. `read()` skips
    `_pull_source()`/`add()` entirely for these -- it's a pure getter,
    since the background thread already wrote the cache directly (via
    the shared `_write()` helper below).

    `reads_from_sensor` is a second, independent capability flag, same
    pattern again: override it to True only for a signal type whose
    `source:` names a sensor directly (see SensorSignal, the only type
    that sets it) rather than another signal. signal_config.py
    dispatches on this generically -- it has no notion of what settings
    a `reads_from_sensor` type actually needs beyond that; that type
    validates its own settings entirely and raises `ValueError` if
    something's wrong.
    """

    owns_read_loop = False
    reads_from_sensor = False

    def __init__(self, source_signal=None):
        self._lock = threading.Lock()
        self._value = None
        self._at = None
        self._last_source_at = None
        self._source_signal = source_signal

    def add(self, raw_value):
        raise NotImplementedError

    def extra_state(self):
        return {}

    def _pull_source(self):
        """Returns `{"value", "at"}` for whatever this signal reads
        from right now, or None if nothing's available yet. Default:
        pulls `self._source_signal.read()` -- every chain type gets this
        for free. Not called at all for `owns_read_loop` types (see
        `read()`); overridden by `SensorSignal` to pull from its sensor
        instead."""
        return self._source_signal.read() if self._source_signal else None

    def read(self):
        """Thread-safe snapshot of this signal's current value --
        `{"value", "at", **extra_state()}` -- or None before any value
        is available yet. See the class docstring for the pull/dirty-
        check mechanics. `_pull_source()` is called outside this
        signal's own lock -- it recurses into another signal's or a
        sensor's own independently-locked `read()`/`last_reading()`, so
        holding this lock across that call isn't needed and would just
        be pointless nesting. The check-and-write itself happens inside
        one lock acquisition so two threads calling `read()`
        concurrently right as new data lands can't both pass the dirty-
        check and both call `add()`."""
        if not self.owns_read_loop:
            source = self._pull_source()
            if source is not None:
                with self._lock:
                    if source["at"] != self._last_source_at:
                        self._value = self.add(source["value"])
                        self._at = source["at"]
                        self._last_source_at = source["at"]

        with self._lock:
            if self._value is None:
                return None
            return {"value": self._value, "at": self._at, **self.extra_state()}

    def _write(self, value, at):
        """Thread-safe cache write, used by an `owns_read_loop` type's
        own background thread to write its computed value directly --
        `read()`'s own compute-if-stale branch writes `_value`/`_at`
        itself, under the same lock as its dirty-check, rather than
        going through this helper."""
        with self._lock:
            self._value = value
            self._at = at

    def reset(self):
        """Clears this signal back to its just-constructed state: no
        cached `read()` value, no memory of what upstream value it's
        already incorporated (so a fresh one isn't mistaken for one
        already consumed), and (via `_reset_state()`) any subclass-
        specific accumulator (a rolling window, an EMA) reset to empty.
        An `owns_read_loop` type additionally restarts its own
        background thread -- see RollingAverageSignal.reset()."""
        with self._lock:
            self._value = None
            self._at = None
        self._last_source_at = None
        self._reset_state()

    def _reset_state(self):
        """Hook for a subclass holding its own accumulator to clear it
        on reset(). No-op by default, for types with no state beyond
        the cached read() value the base class already owns."""
