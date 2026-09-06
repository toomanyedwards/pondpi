import yaml

from pondpi.sensors import discover_sensor_types
from pondpi.signal_processor_config import build_processors


def load_sensors(path, simulate=False):
    """Loads named sensors from a YAML config file (config/sensors.yaml),
    each bundled with its own driver instance and signal processor
    pipeline.

    Returns dict[name -> {"driver", "processors", "primary_name",
    "emit_flags", "configs"}] -- the last four fields are exactly what
    `signal_processor_config.load_signal_processors()` returns, since
    each sensor's `processors:` list uses that identical schema, just
    nested under the sensor instead of living in its own file.

    Exactly one sensor must be marked `default: true` -- server.py's
    bare (not sensor-named) routes operate on that one, so existing
    integrations (e.g. Home Assistant's REST sensors) that were built
    against a single-sensor deployment keep working unchanged after
    adding more sensors.

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
    default_name = None

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

        processor_entries = entry.get("processors")
        if not processor_entries:
            raise ValueError(f"{path}: sensor '{name}' must have a non-empty 'processors' list")

        driver = sensor_types[sensor_type](entry.get("params") or {}, simulate)
        processors, primary_name, emit_flags, configs = build_processors(processor_entries, f"{path} (sensor '{name}')")

        sensors[name] = {
            "driver": driver,
            "processors": processors,
            "primary_name": primary_name,
            "emit_flags": emit_flags,
            "configs": configs,
        }

        if entry.get("default", False):
            if default_name is not None:
                raise ValueError(f"{path}: multiple sensors marked default ('{default_name}' and '{name}')")
            default_name = name

    if default_name is None:
        raise ValueError(f"{path}: exactly one sensor must be marked 'default: true'")

    return sensors, default_name
