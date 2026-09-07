import yaml

from pondpi.signals import discover_signal_types


def load_signals(path, sensor_names):
    """Loads named LevelSignal instances from a YAML file's top-level
    `signals:` list and groups them by which sensor each one is
    ultimately rooted at.

    `sensor_names` is the set of already-configured sensor names (from
    sensor_config.py). Every signal type either sets `reads_from_sensor
    = True` (see LevelSignal.reads_from_sensor) and connects directly to
    a sensor via its own top-level fields (currently just `SensorSignal`,
    via `sensor:`) -- validating those fields, `sensor_names` included,
    is entirely that type's own responsibility, not something this
    module knows or checks -- or names another, earlier-defined signal
    via a top-level `input:` key instead, reading *that* signal's live
    output rather than a sensor's raw reading. This is how sequential
    composition (e.g. median-then-average) is expressed, without a
    dedicated "chain" type.

    `unit`/`mode` are tracked and propagated here as generic concepts,
    without this module knowing or caring what specific values either
    one holds: a `reads_from_sensor` signal's own `unit`/`mode` (however
    it derived and validated them) become the root of its group's chain;
    every other signal type inherits them automatically from whichever
    signal its `input:` names (they're pure numeric transforms -- a
    rolling average of centimeters is still in centimeters) and must not
    set `unit`/`mode` directly.

    Returns dict[sensor_name -> {"signals", "emit_flags", "configs"}],
    one entry per name in `sensor_names` (even if that sensor ends up
    with zero signals -- see build_signals, which raises for that case
    rather than silently omitting it). Any signal type may set
    `owns_read_loop = True` as a class attribute (see
    LevelSignal.owns_read_loop and RollingAverageSignal) -- such a type
    is constructed with a `get_raw_value` callable (built here, closing
    over its already-constructed `input:` signal) and starts its own
    background thread the moment it's constructed, whatever their
    number per sensor's group (zero, one, or more), no config marker
    needed. Any signal may set `emit: false` (default true) to keep it
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
    key. `path` is used only for error messages.

    This function has no notion of what any particular signal type's
    `params` mean or which values are valid for them -- it only enforces
    the two structural rules every type is bound by regardless of its
    own semantics (a `reads_from_sensor` type must not set `input:`; any
    other type must) and dispatches construction generically on the
    `reads_from_sensor`/`owns_read_loop` capability flags. Every other
    validation (is this unit/mode/sensor-name actually valid) is each
    signal type's own responsibility, surfaced as a `ValueError` from its
    own constructor and wrapped here with config-file context."""
    signal_types = discover_signal_types()

    instances = {}
    entries_by_name = {}
    root_sensor = {}
    unit_by_name = {}
    mode_by_name = {}

    for entry in entries:
        name = entry.get("name")

        if not name:
            raise ValueError(f"{path}: signal entry is missing 'name': {entry}")
        if name in instances:
            raise ValueError(f"{path}: duplicate signal name '{name}'")

        signal_type = entry.get("type")
        if signal_type not in signal_types:
            raise ValueError(f"{path}: signal '{name}' has unknown type '{signal_type}' (expected one of {sorted(signal_types)})")

        signal_class = signal_types[signal_type]
        input_name = entry.get("input")
        params = dict(entry.get("params") or {})

        extra_kwargs = {}
        if signal_class.reads_from_sensor:
            if input_name is not None:
                raise ValueError(
                    f"{path}: signal '{name}' is type '{signal_type}' and must not set 'input' "
                    f"({signal_type} signals read from a sensor directly, not another signal)"
                )
            extra_kwargs["sensor_names"] = sensor_names
            extra_kwargs["sensor"] = entry.get("sensor")
            extra_kwargs["unit"] = entry.get("unit")
            if "mode" in entry:
                extra_kwargs["mode"] = entry["mode"]
        else:
            if not input_name:
                raise ValueError(
                    f"{path}: signal '{name}' must set 'input' naming the signal it reads from "
                    "(only signals that read directly from a sensor may omit it)"
                )
            if input_name not in entries_by_name:
                raise ValueError(f"{path}: signal '{name}' references undefined input '{input_name}' (must be defined earlier in the file)")
            if "unit" in entry:
                raise ValueError(
                    f"{path}: signal '{name}' must not set 'unit' directly "
                    "(unit is derived automatically from 'input')"
                )
            if "mode" in entry:
                raise ValueError(
                    f"{path}: signal '{name}' must not set 'mode' directly "
                    "(mode is derived automatically from 'input')"
                )

        if signal_class.owns_read_loop:
            # input_name is guaranteed set and already constructed --
            # validated above (non-reads_from_sensor types require
            # `input:` naming an earlier-defined signal, and instances
            # only ever gains an entry for names already fully built).
            input_instance = instances[input_name]

            def get_raw_value(input_instance=input_instance):
                result = input_instance.current()
                return result["value"] if result else None

            extra_kwargs["get_raw_value"] = get_raw_value

        try:
            instances[name] = signal_class(**params, **extra_kwargs)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{path}: signal '{name}' has invalid params for type '{signal_type}': {e}") from e

        if signal_class.reads_from_sensor:
            root_sensor[name] = instances[name].sensor
            unit_by_name[name] = instances[name].unit
            mode_by_name[name] = instances[name].mode
        else:
            root_sensor[name] = root_sensor[input_name]
            unit_by_name[name] = unit_by_name[input_name]
            mode_by_name[name] = mode_by_name[input_name]

        entries_by_name[name] = entry

    grouped = {sensor: {"signals": {}, "emit_flags": {}, "configs": {}} for sensor in sensor_names}

    for name, entry in entries_by_name.items():
        group = grouped[root_sensor[name]]
        group["signals"][name] = instances[name]
        group["emit_flags"][name] = entry.get("emit", True)
        group["configs"][name] = _config_summary(entry, unit_by_name[name], mode_by_name[name])

    for sensor, group in grouped.items():
        if not group["signals"]:
            raise ValueError(f"{path}: sensor '{sensor}' has no signals rooted at it (add a 'sensor' signal with sensor: {sensor})")

    return grouped


def _config_summary(entry, unit, mode):
    summary = {
        "type": entry.get("type"),
        "params": entry.get("params") or {},
        "emit": entry.get("emit", True),
        "unit": unit,
        "mode": mode,
    }
    if entry.get("input") is not None:
        summary["input"] = entry["input"]
    return summary
