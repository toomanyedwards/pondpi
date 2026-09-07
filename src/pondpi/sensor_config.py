import yaml

from pondpi.sensors import discover_sensor_types
from pondpi.signal_config import load_signals


def load_sensors(path, simulate=False):
    """Loads named sensors from a YAML config file (config/sensors.yaml),
    each bundled with its own driver instance, plus that sensor's own
    slice of the file's top-level `signals:` list -- every signal
    ultimately rooted (via `source:` chains, see signal_config.py) at a
    `sensor` signal naming this sensor.

    Returns dict[name -> {"driver", "signals", "emit_flags", "configs"}]
    -- the last three fields are exactly what `signal_config.
    load_signals()` returns for that sensor's group.

    Signals are built *before* any sensor driver is constructed (the
    reverse of the natural YAML order) -- a driver's background read
    thread starts the moment it's constructed (see LevelSensor.__init__()),
    so the callback that routes its readings into signals
    (`_build_on_reading()`, below) has to exist first, which in turn
    needs the fully-built signal graph.

    If `simulate` is True, every sensor is constructed in simulated mode
    regardless of its configured `type`/`settings` -- see each driver
    type's own `create()` for what that means for it.
    """
    sensor_types = discover_sensor_types()

    with open(path) as f:
        config = yaml.safe_load(f)

    entries = (config or {}).get("sensors")
    if not entries:
        raise ValueError(f"{path}: 'sensors' must be a non-empty list")

    parsed = []
    names = set()

    for entry in entries:
        name = entry.get("name")
        if not name:
            raise ValueError(f"{path}: sensor entry is missing 'name': {entry}")
        if name in names:
            raise ValueError(f"{path}: duplicate sensor name '{name}'")
        names.add(name)

        sensor_type = entry.get("type")
        if sensor_type not in sensor_types:
            raise ValueError(
                f"{path}: sensor '{name}' has unknown type '{sensor_type}' (expected one of {sorted(sensor_types)})"
            )

        parsed.append((name, sensor_type, entry.get("settings") or {}))

    signal_groups = load_signals(path, names)

    sensors = {}
    for name, sensor_type, params in parsed:
        group = signal_groups[name]
        on_reading = _build_on_reading(group["signals"], group["configs"])
        driver = sensor_types[sensor_type](params, simulate, on_reading)
        sensors[name] = {"driver": driver, **group}

    return sensors


def _build_on_reading(signals, configs):
    """Builds the callback a sensor's own background thread calls once
    per (reading_key, distance_mm) pair it produces -- the direct
    successor to server.py's old `_route_reading()`, moved here since a
    sensor's thread now starts at construction time, before server.py
    ever sees it.

    Each reading key (e.g. "raw", "processed" -- see LevelSensor.read())
    only feeds the signals rooted at that same `mode`
    (`configs[sname]["mode"]`) -- a signal either reads that reading
    directly (`signal.reads_from_sensor` -- a `sensor`-type signal) or
    reads whatever its `source:`-named signal just computed this same
    call (`configs[sname]["source"]`, already resolved into `results`
    since `signals`' iteration order is a valid dependency order). A
    signal with `owns_read_loop = True` is skipped here entirely -- it's
    fed by its own dedicated thread instead (see LevelSignal), sampling
    its `source:` signal's `current()` on its own pace rather than being
    pushed a value on every one of the sensor's own readings."""

    def on_reading(reading_key, distance_mm):
        results = {}
        for sname, signal in signals.items():
            if signal.owns_read_loop:
                continue
            if configs[sname]["mode"] != reading_key:
                continue
            value = distance_mm if signal.reads_from_sensor else results[configs[sname]["source"]]
            results[sname] = signal.feed(value)

    return on_reading
