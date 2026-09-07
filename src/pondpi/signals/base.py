class LevelSignal:
    """Base class for a level signal.

    Most subclasses take raw sensor readings one at a time via `add()`
    and return this signal's current output -- composable via `input:`
    in config/sensors.yaml, fed by each sensor's own background read
    loop (see LevelSensor.poll_loop()) routed through server.py's
    `_route_reading()`. `extra_state()` surfaces any signal-specific
    metadata (window sizes, sample counts, ...) for the /level response.
    Signals are unit-agnostic — they don't know or care whether the
    values they're passed are mm, cm, or anything else; unit conversion
    happens at the HTTP layer in server.py.

    `owns_read_loop` is a capability flag, same pattern as
    `LevelSensor.supports_reset`: override it to True only for a signal
    type that maintains its own background thread instead of being fed
    via `add()` from each sensor's own read loop (see RollingAverageSignal,
    which samples its `input:` signal's cached value on its own
    schedule rather than being pushed a new one every poll tick).
    server.py's `main()` calls `start()` once per owns_read_loop signal
    in a sensor's group -- any number, no config marker needed -- and
    otherwise has no involvement in how that signal runs itself.
    """

    owns_read_loop = False

    def add(self, raw_value):
        raise NotImplementedError

    def extra_state(self):
        return {}

    def start(self, stop_event, get_raw_value):
        """Only implemented by `owns_read_loop = True` types: spawns
        and owns whatever background thread this signal needs to keep
        itself updated. `get_raw_value` is a zero-arg callable returning
        this signal's `input:` signal's current cached value (in mm),
        or None if it isn't ready yet -- supplied by server.py, which
        owns the shared per-sensor state it reads from. `stop_event` is
        shared with every other background thread in the process; this
        signal's own thread must exit once it's set."""
        raise NotImplementedError
