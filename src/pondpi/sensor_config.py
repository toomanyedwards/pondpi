import yaml

from pondpi.sensors import discover_sensor_types
from pondpi.signal_config import load_signals


def load_sensors(path, simulate=False):
    """Loads named sensors from a YAML config file (config/sensors.yaml),
    each bundled with its own driver instance, plus that sensor's own
    slice of the file's top-level `signals:` list -- every signal
    ultimately rooted (via `input:` chains, see signal_config.py) at a
    `sensor` signal naming this sensor.

    Returns dict[name -> {"driver", "signals", "emit_flags", "configs"}]
    -- the last three fields are exactly what `signal_config.
    load_signals()` returns for that sensor's group.

    If `simulate` is True, every sensor is constructed in simulated mode
    regardless of its configured `type`/`params` -- see each driver
    type's own `create()` for what that means for it.
    """
    sensor_types = discover_sensor_types()

    with open(path) as f:
        config = yaml.safe_load(f)

    entries = (config or {}).get("sensors")
    if not entries:
        raise ValueError(f"{path}: 'sensors' must be a non-empty list")

    sensors = {}

    for entry in entries:
        name = entry.get("name")
        if not name:
            raise ValueError(f"{path}: sensor entry is missing 'name': {entry}")
        if name in sensors:
            raise ValueError(f"{path}: duplicate sensor name '{name}'")

        sensor_type = entry.get("type")
        if sensor_type not in sensor_types:
            raise ValueError(
                f"{path}: sensor '{name}' has unknown type '{sensor_type}' (expected one of {sorted(sensor_types)})"
            )

        driver = sensor_types[sensor_type](entry.get("params") or {}, simulate)
        sensors[name] = {"driver": driver}

    signal_groups = load_signals(path, set(sensors))
    for name, sensor_entry in sensors.items():
        sensor_entry.update(signal_groups[name])

    return sensors
