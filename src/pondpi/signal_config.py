import yaml

from pondpi.signals import discover_signal_types


def load_signals(path, sensor_objects):
    """Loads named Signal instances from a YAML file's top-level
    `signals:` list and groups them by which sensor each one is
    ultimately rooted at.

    `sensor_objects` is `dict[name -> Sensor instance]` for every
    already-constructed sensor (from sensor_config.py, which builds
    sensors before signals for exactly this reason). Every signal entry
    sets a top-level `source:`, a mapping naming where its data comes
    from: `source.name` is required, `source.options` is an optional
    mapping of extra settings governing *how* it reads from that name --
    what `name` refers to, and whether `options` means anything at all,
    depends on the signal type's `reads_from_sensor` flag (see
    Signal.reads_from_sensor): a `reads_from_sensor` type (currently just
    `SensorSignal`) has `source.name` name a configured sensor directly,
    with `source.options` passed straight through to its constructor as
    `sensor_options` (validating both the sensor name and whatever
    `options` holds, and holding onto the live sensor object it names, is
    entirely that type's own responsibility, not something this module
    knows or checks); every other type has `source.name` name another,
    earlier-defined signal instead, reading *that* signal's own `read()`
    output rather than a sensor's raw reading, and must leave
    `source.options` empty -- there's nothing downstream of a sensor for
    it to configure. This is how sequential composition (e.g. median-
    then-average) is expressed, without a dedicated "chain" type --
    `source:` is simply every signal's one "where do I get data from"
    field, whichever kind of thing it names.

    `unit`/`mode` are tracked and propagated here as generic concepts,
    without this module knowing or caring what specific values either
    one holds: a `reads_from_sensor` signal's own `unit`/`mode` (however
    it derived and validated them, from its own `settings:unit` and
    `source.options.read_mode` respectively, see
    signals/sensor_signal.py) become the root of its group's chain;
    every other signal type inherits them automatically from whichever
    signal its `source.name` names (they're pure numeric transforms -- a
    rolling average of centimeters is still in centimeters) and must not
    set `unit` in its own `settings:` or anything in `source.options`.

    Returns dict[sensor_name -> {"signals", "emit_flags", "configs"}],
    one entry per name in `sensor_objects` (even if that sensor ends up
    with zero signals -- see build_signals, which raises for that case
    rather than silently omitting it). Every non-`reads_from_sensor`
    type is constructed with its already-built `source:` signal, the
    same way regardless of how it actually reads it -- a type that
    maintains its own background thread instead of computing lazily
    (see RollingAverageSignal) starts that thread the moment it's
    constructed, whatever their number per sensor's group (zero, one,
    or more), no config marker needed; this module has no notion of
    that distinction at all, it's entirely between the signal and its
    `source:`. Any signal may set `emit: false` (default true) to keep
    it out of /level's `signals` section while still showing up in full
    on /diag.
    """
    with open(path) as f:
        config = yaml.safe_load(f)

    entries = (config or {}).get("signals")
    if not entries:
        raise ValueError(f"{path}: 'signals' must be a non-empty list")

    return build_signals(entries, sensor_objects, path)


def build_signals(entries, sensor_objects, path):
    """Builds named Signal instances from an already-parsed list of
    signal entries and groups them by root sensor -- the shared core
    `load_signals` also uses after reading its own top-level `signals:`
    key. `path` is used only for error messages.

    This function has no notion of what any particular signal type's
    `settings:` or `source.options` mean or which values are valid for
    them -- it only enforces the structural rules every type is bound by
    regardless of its own semantics (every entry must set a `source:`
    mapping with a `name`; a non-`reads_from_sensor` type's `source.name`
    must name an earlier-defined signal and its `source.options` must be
    empty) and dispatches construction generically on the
    `reads_from_sensor` capability flag. Every other validation (is
    this unit/mode/sensor-name/read_mode actually valid) is each signal
    type's own responsibility, surfaced as a `ValueError` from its own
    constructor and wrapped here with config-file context."""
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
        source = entry.get("source")
        if not source:
            raise ValueError(f"{path}: signal '{name}' must set 'source' naming the sensor or signal it reads from")
        if not isinstance(source, dict) or not source.get("name"):
            raise ValueError(f"{path}: signal '{name}' has invalid 'source' (expected a mapping with a 'name')")
        unexpected_source_keys = set(source) - {"name", "options"}
        if unexpected_source_keys:
            raise ValueError(
                f"{path}: signal '{name}' has invalid 'source' key(s) {sorted(unexpected_source_keys)} "
                "(expected only 'name'/'options')"
            )
        source_name = source["name"]
        source_options = dict(source.get("options") or {})
        settings = dict(entry.get("settings") or {})

        extra_kwargs = {}
        if signal_class.reads_from_sensor:
            extra_kwargs["sensor_objects"] = sensor_objects
            extra_kwargs["sensor"] = source_name
            extra_kwargs["sensor_options"] = source_options
        else:
            if source_name not in entries_by_name:
                raise ValueError(f"{path}: signal '{name}' references undefined source '{source_name}' (must be defined earlier in the file)")
            if "unit" in settings:
                raise ValueError(
                    f"{path}: signal '{name}' must not set 'unit' directly "
                    "(unit is derived automatically from 'source')"
                )
            if source_options:
                raise ValueError(
                    f"{path}: signal '{name}' must not set 'source.options' "
                    "(mode is derived automatically from 'source')"
                )
            extra_kwargs["source_signal"] = instances[source_name]

        try:
            instances[name] = signal_class(**settings, **extra_kwargs)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{path}: signal '{name}' has invalid settings for type '{signal_type}': {e}") from e

        if signal_class.reads_from_sensor:
            root_sensor[name] = instances[name].sensor
            unit_by_name[name] = instances[name].unit
            mode_by_name[name] = instances[name].read_mode
        else:
            root_sensor[name] = root_sensor[source_name]
            unit_by_name[name] = unit_by_name[source_name]
            mode_by_name[name] = mode_by_name[source_name]

        entries_by_name[name] = entry

    grouped = {sensor: {"signals": {}, "emit_flags": {}, "configs": {}} for sensor in sensor_objects}

    for name, entry in entries_by_name.items():
        group = grouped[root_sensor[name]]
        group["signals"][name] = instances[name]
        group["emit_flags"][name] = entry.get("emit", True)
        group["configs"][name] = _config_summary(entry, unit_by_name[name], mode_by_name[name])

    for sensor, group in grouped.items():
        if not group["signals"]:
            raise ValueError(f"{path}: sensor '{sensor}' has no signals rooted at it (add a 'sensor' signal with source: {{name: {sensor}}})")

    return grouped


def _config_summary(entry, unit, mode):
    return {
        "type": entry.get("type"),
        "source": entry.get("source"),
        "settings": entry.get("settings") or {},
        "emit": entry.get("emit", True),
        "unit": unit,
        "mode": mode,
    }
