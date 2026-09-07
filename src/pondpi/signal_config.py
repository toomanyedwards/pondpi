import yaml

from pondpi.signals import discover_signal_types


def load_signals(path, sensor_names):
    """Loads named LevelSignal instances from a YAML file's top-level
    `signals:` list and groups them by which sensor each one is
    ultimately rooted at.

    `sensor_names` is the set of already-configured sensor names (from
    sensor_config.py) -- every `type: sensor` signal must name one of
    them via `params.sensor`, since `sensor` is the only signal type
    that reads directly from a sensor. Every other signal type instead names
    another, earlier-defined signal via a top-level `input:` key,
    reading *that* signal's live output rather than a sensor's raw
    reading -- this is how sequential composition (e.g.
    median-then-average) is expressed, without a dedicated "chain" type.

    Every `type: sensor` signal must also set `params.unit` (e.g.
    "cm") -- required, since it's the boundary where a physical
    reading enters the signal graph and nothing upstream can tell us
    what unit it's in. Every other signal type derives its `unit`
    automatically from whichever signal its `input:` names (they're
    pure numeric transforms -- a rolling average of centimeters is
    still in centimeters), and must not set `params.unit` itself.

    Returns dict[sensor_name -> {"signals", "primary_name",
    "emit_flags", "configs"}], one entry per name in `sensor_names`
    (even if that sensor ends up with zero signals -- see build_signals,
    which raises for that case rather than silently omitting it).
    Within each group: exactly one signal must be marked `primary:
    true`; any signal may set `emit: false` (default true) to keep it
    out of /level's `signals` section while still showing up in full on
    /diag.
    """
    with open(path) as f:
        config = yaml.safe_load(f)

    entries = (config or {}).get("signals")
    if not entries:
        raise ValueError(f"{path}: 'signals' must be a non-empty list")

    return build_signals(entries, sensor_names, path)


def build_signals(entries, sensor_names, path):
    """Builds named LevelSignal instances from an already-parsed list of
    signal entries and groups them by root sensor -- the shared core
    `load_signals` also uses after reading its own top-level `signals:`
    key. `path` is used only for error messages."""
    signal_types = discover_signal_types()

    instances = {}
    entries_by_name = {}
    root_sensor = {}
    unit_by_name = {}

    for entry in entries:
        name = entry.get("name")

        if not name:
            raise ValueError(f"{path}: signal entry is missing 'name': {entry}")
        if name in instances:
            raise ValueError(f"{path}: duplicate signal name '{name}'")

        signal_type = entry.get("type")
        if signal_type not in signal_types:
            raise ValueError(f"{path}: signal '{name}' has unknown type '{signal_type}' (expected one of {sorted(signal_types)})")

        input_name = entry.get("input")
        params = dict(entry.get("params") or {})

        if signal_type == "sensor":
            if input_name is not None:
                raise ValueError(
                    f"{path}: signal '{name}' is type 'sensor' and must not set 'input' "
                    "(sensor signals read from a sensor via params.sensor, not another signal)"
                )
            sensor = params.get("sensor")
            if sensor not in sensor_names:
                raise ValueError(
                    f"{path}: signal '{name}' (type 'sensor') has invalid or missing params.sensor "
                    f"'{sensor}' (expected one of {sorted(sensor_names)})"
                )
            unit = params.pop("unit", None)
            if not unit:
                raise ValueError(f"{path}: signal '{name}' (type 'sensor') is missing required params.unit")
            root_sensor[name] = sensor
            unit_by_name[name] = unit
        else:
            if not input_name:
                raise ValueError(
                    f"{path}: signal '{name}' must set 'input' naming the signal it reads from "
                    "(only 'sensor' signals read directly from a sensor)"
                )
            if input_name not in entries_by_name:
                raise ValueError(f"{path}: signal '{name}' references undefined input '{input_name}' (must be defined earlier in the file)")
            if "unit" in params:
                raise ValueError(
                    f"{path}: signal '{name}' must not set params.unit directly "
                    "(unit is derived automatically from 'input')"
                )
            root_sensor[name] = root_sensor[input_name]
            unit_by_name[name] = unit_by_name[input_name]

        try:
            instances[name] = signal_types[signal_type](**params)
        except TypeError as e:
            raise ValueError(f"{path}: signal '{name}' has invalid params for type '{signal_type}': {e}") from e

        entries_by_name[name] = entry

    grouped = {sensor: {"signals": {}, "primary_name": None, "emit_flags": {}, "configs": {}} for sensor in sensor_names}

    for name, entry in entries_by_name.items():
        group = grouped[root_sensor[name]]
        group["signals"][name] = instances[name]
        group["emit_flags"][name] = entry.get("emit", True)
        group["configs"][name] = _config_summary(entry, unit_by_name[name])

        if entry.get("primary", False):
            if group["primary_name"] is not None:
                raise ValueError(
                    f"{path}: sensor '{root_sensor[name]}': multiple signals marked primary "
                    f"('{group['primary_name']}' and '{name}')"
                )
            group["primary_name"] = name

    for sensor, group in grouped.items():
        if not group["signals"]:
            raise ValueError(f"{path}: sensor '{sensor}' has no signals rooted at it (add a 'sensor' signal with params.sensor: {sensor})")
        if group["primary_name"] is None:
            raise ValueError(f"{path}: sensor '{sensor}': exactly one signal must be marked 'primary: true'")

    return grouped


def _config_summary(entry, unit):
    summary = {
        "type": entry.get("type"),
        "params": entry.get("params") or {},
        "primary": bool(entry.get("primary", False)),
        "emit": entry.get("emit", True),
        "unit": unit,
    }
    if entry.get("input") is not None:
        summary["input"] = entry["input"]
    return summary
