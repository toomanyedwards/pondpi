import yaml

from pondpi.signals import discover_signal_types


def load_signals(path):
    """Loads named LevelSignal instances from a YAML config file.

    Returns (dict[name -> LevelSignal instance], primary_name,
    dict[name -> emit bool], dict[name -> config summary]). Exactly one
    entry must be marked `primary: true` — its output backfills the
    legacy top-level rolling_avg_distance_cm field in /level.

    Each entry may set `emit: false` (default true) to keep that signal
    out of /level's `signals` section while still showing up in full on
    /diag.

    The config summary dict (used by /diag) reflects each entry's
    *effective* config -- `type`, `params` (raw as written, including
    unresolved chain step dicts), `primary`, `emit` -- with defaults
    applied, not just what was literally typed.

    A `type: chain` entry's `params.steps` runs a value through multiple
    signals in sequence. Each step is either:
      - `{ref: <name>}` — builds a fresh instance using the type/params
        of the signal already defined earlier in this file under that
        name. This is a config alias, not a shared live instance: every
        signal always gets its own independent state, so the same name
        can be reused in multiple places without one throwing off
        another's window.
      - `{type: ..., params: ...}` — builds a fresh instance directly,
        recursively (so a step can itself be a chain).
    """
    with open(path) as f:
        config = yaml.safe_load(f)

    entries = (config or {}).get("signals")
    if not entries:
        raise ValueError(f"{path}: 'signals' must be a non-empty list")

    return build_signals(entries, path)


def build_signals(entries, path):
    """Builds named LevelSignal instances from an already-parsed list of
    signal entries -- the shared core `load_signals` also uses after
    reading its own top-level `signals:` key from a standalone file. A
    sensor's own `signals:` list, nested directly in config/sensors.yaml,
    is built the same way via this function without needing a separate
    file per sensor. `path` is used only for error messages (e.g. a
    sensor's own config file, even though this isn't reading it
    directly)."""
    signal_types = discover_signal_types()

    signals = {}
    entries_by_name = {}
    emit_flags = {}
    configs = {}
    primary_name = None

    for entry in entries:
        name = entry.get("name")

        if not name:
            raise ValueError(f"{path}: signal entry is missing 'name': {entry}")
        if name in signals:
            raise ValueError(f"{path}: duplicate signal name '{name}'")

        signals[name] = _build_signal(entry, signal_types, entries_by_name, path, f"signal '{name}'")
        entries_by_name[name] = entry
        emit_flags[name] = entry.get("emit", True)
        configs[name] = _config_summary(entry)

        if entry.get("primary", False):
            if primary_name is not None:
                raise ValueError(f"{path}: multiple signals marked primary ('{primary_name}' and '{name}')")
            primary_name = name

    if primary_name is None:
        raise ValueError(f"{path}: exactly one signal must be marked 'primary: true'")

    return signals, primary_name, emit_flags, configs


def _config_summary(entry):
    summary = {
        "type": entry.get("type"),
        "params": entry.get("params") or {},
        "primary": bool(entry.get("primary", False)),
        "emit": entry.get("emit", True),
    }
    if entry.get("ref") is not None:
        summary["ref"] = entry["ref"]
    return summary


def _build_signal(entry, signal_types, entries_by_name, path, label):
    """Builds one LevelSignal instance from a config entry -- either a
    top-level signal or a nested chain step. `label` identifies the
    entry in error messages. Always returns a fresh instance, even for
    `ref:` entries (see load_signals).

    `entries_by_name` only contains entries defined earlier in the file
    than the one currently being built (see load_signals's loop), so a
    `ref:` can only point backwards -- this rules out self-reference and
    cycles without any separate cycle-detection code.
    """
    ref = entry.get("ref")
    if ref is not None:
        if ref not in entries_by_name:
            raise ValueError(f"{path}: {label} references undefined signal '{ref}' (must be defined earlier in the file)")
        return _build_signal(entries_by_name[ref], signal_types, entries_by_name, path, f"{label} (ref '{ref}')")

    signal_type = entry.get("type")
    if signal_type not in signal_types:
        raise ValueError(f"{path}: {label} has unknown type '{signal_type}' (expected one of {sorted(signal_types)})")

    params = dict(entry.get("params") or {})

    if signal_type == "chain":
        steps = params.get("steps") or []
        if not steps:
            raise ValueError(f"{path}: {label} (chain) must have at least one step in 'params.steps'")
        params["steps"] = [
            (
                step.get("ref") or step.get("type"),
                _build_signal(step, signal_types, entries_by_name, path, f"{label} step {index + 1}"),
            )
            for index, step in enumerate(steps)
        ]

    try:
        return signal_types[signal_type](**params)
    except TypeError as e:
        raise ValueError(f"{path}: {label} has invalid params for type '{signal_type}': {e}") from e
