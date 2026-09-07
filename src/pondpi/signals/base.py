import threading


class Signal:
    """Base class for a signal -- a named, composable processing step
    over a stream of numeric readings, with no notion of what those
    readings physically represent.

    Nothing is ever pushed into a signal. `read()` is the sole way
    anything -- another signal via `source:`, server.py -- ever gets
    this signal's value: it pulls `_pull_source()` (concrete, see
    below) and, if that's newer than the last value this signal already
    incorporated, computes a fresh one via `add()` (the one hook every
    subclass implements -- pure computation, no caching of its own) and
    caches it. Calling `read()` any number of times with no new
    upstream data is safe and cheap -- `add()` only runs when there's
    genuinely something new, so a stateful accumulator (a rolling
    window, an EMA) is never double-fed by two callers polling in quick
    succession. Every signal type except `sensor` is unit-agnostic --
    they don't know or care whether the values they're passed are mm,
    cm, or anything else; `SensorSignal` is the one deliberate
    exception, converting once at the boundary where a sensor's
    canonical millimeter reading first enters the graph (see
    signals/sensor_signal.py).

    `_pull_source()`'s default implementation pulls `self._source_signal`
    (a live `Signal` instance passed into `__init__`)'s own `read()` --
    this covers every chain type for free, purely through inheritance.
    `SensorSignal` is the one type that overrides it, to pull from its
    sensor instead of another signal (see there).

    A signal type that maintains its own background thread instead of
    computing lazily at `read()` time (see RollingAverageSignal, which
    samples its `source:` signal's `read()` on its own schedule rather
    than recomputing every time something asks) simply overrides
    `read()` itself to skip the pull entirely and return `_snapshot()`
    -- its own thread writes the cache directly instead, via the shared
    `_write()` helper below. There's no capability flag for this: a
    type either behaves like the default (compute lazily on `read()`)
    or overrides `read()` to say otherwise, the same as any other
    polymorphic method -- nothing outside `Signal` itself needs to know
    which one a given type does.

    `reads_from_sensor` is a capability flag, same pattern as
    `Sensor.supports_reset`: override it to True only for a signal type
    whose `source:` names a sensor directly (see SensorSignal, the only
    type that sets it) rather than another signal. signal_config.py
    dispatches on this generically -- it has no notion of what settings
    a `reads_from_sensor` type actually needs beyond that; that type
    validates its own settings entirely and raises `ValueError` if
    something's wrong.
    """

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
        for free. Overridden by `SensorSignal` to pull from its sensor
        instead; not called at all by a type that overrides `read()`
        itself (see RollingAverageSignal)."""
        return self._source_signal.read() if self._source_signal else None

    def read(self):
        """Pulls `_pull_source()` and, if that's newer than the last
        value this signal already incorporated, computes a fresh one via
        `add()` and caches it -- then returns the cached snapshot either
        way (see `_snapshot()`). `_pull_source()` is called outside this
        signal's own lock -- it recurses into another signal's or a
        sensor's own independently-locked `read()`/`last_reading()`, so
        holding this lock across that call isn't needed and would just
        be pointless nesting. The check-and-write itself happens inside
        one lock acquisition so two threads calling `read()`
        concurrently right as new data lands can't both pass the dirty-
        check and both call `add()`.

        A type driven by its own background thread instead (see
        RollingAverageSignal) overrides this entirely to skip the pull
        and just return `_snapshot()` -- its thread writes the cache
        directly, on its own schedule, via `_write()`."""
        source = self._pull_source()
        if source is not None:
            with self._lock:
                if source["at"] != self._last_source_at:
                    self._value = self.add(source["value"])
                    self._at = source["at"]
                    self._last_source_at = source["at"]

        return self._snapshot()

    def _snapshot(self):
        """Thread-safe read of this signal's current cached value --
        `{"value", "at", **extra_state()}` -- or None before any value
        is available yet. The read side of the cache `_write()` (below)
        writes; shared by `read()`'s own compute-if-stale branch and by
        a background-thread-driven type's own `read()` override."""
        with self._lock:
            if self._value is None:
                return None
            return {"value": self._value, "at": self._at, **self.extra_state()}

    def _write(self, value, at):
        """Thread-safe cache write, used by a background-thread-driven
        type's own thread to write its computed value directly --
        `read()`'s own compute-if-stale branch writes `_value`/`_at`
        itself, under the same lock as its dirty-check, rather than
        going through this helper."""
        with self._lock:
            self._value = value
            self._at = at

    def reset(self):
        """Clears this signal back to its just-constructed state: no
        cached value, no memory of what upstream value it's already
        incorporated (so a fresh one isn't mistaken for one already
        consumed), and (via `_reset_state()`) any subclass-specific
        accumulator (a rolling window, an EMA) reset to empty. A type
        driven by its own background thread additionally restarts it --
        see RollingAverageSignal.reset()."""
        with self._lock:
            self._value = None
            self._at = None
        self._last_source_at = None
        self._reset_state()

    def _reset_state(self):
        """Hook for a subclass holding its own accumulator to clear it
        on reset(). No-op by default, for types with no state beyond
        the cached value the base class already owns."""
