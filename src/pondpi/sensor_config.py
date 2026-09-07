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

    Sensors are built *before* any signal -- a `reads_from_sensor`
    signal (see signal_config.py) needs a live `Sensor` object to pull
    from, so it has to exist first. This also matches the YAML's own
    top-to-bottom `sensors:`-then-`signals:` layout. Nothing is ever
    pushed from a sensor into a signal -- each one pulls its `source:`
    on demand via `read()`, so there's no callback to wire up here at
    all.

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

    sensor_objects = {}
    for name, sensor_type, settings in parsed:
        sensor_objects[name] = sensor_types[sensor_type](settings, simulate)

    signal_groups = load_signals(path, sensor_objects)

    return {name: {"driver": obj, **signal_groups[name]} for name, obj in sensor_objects.items()}
