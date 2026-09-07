class LevelSignal:
    """Base class for a level signal.

    Most subclasses take raw sensor readings one at a time via `add()`
    and return this signal's current output -- composable via `input:`
    in config/sensors.yaml, fed by poll_sensor()'s loop (see server.py).
    `extra_state()` surfaces any signal-specific metadata (window sizes,
    sample counts, ...) for the /level response. Signals are
    unit-agnostic — they don't know or care whether the values they're
    passed are mm, cm, or anything else; unit conversion happens at the
    HTTP layer in server.py.

    `owns_read_loop` is a capability flag, same pattern as
    `LevelSensor.supports_reset`: override it to True only for a signal
    type that maintains its own background thread instead of being fed
    via `add()` by poll_sensor()'s loop (see RollingAverageSignal,
    which samples its `input:` signal's cached value on its own
    schedule rather than being pushed a new one every poll tick).
    server.py's `main()` spawns one such thread per owns_read_loop
    signal in a sensor's group -- any number, no config marker needed.
    """

    owns_read_loop = False

    def add(self, raw_value):
        raise NotImplementedError

    def extra_state(self):
        return {}
