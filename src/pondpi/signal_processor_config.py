import yaml

from pondpi.signal_processors import discover_signal_processor_types


def load_signal_processors(path):
    """Loads named LevelSignalProcessor instances from a YAML config file.

    Returns (dict[name -> LevelSignalProcessor instance], primary_name,
    dict[name -> emit bool]). Exactly one entry must be marked
    `primary: true` — its output backfills the legacy top-level
    rolling_avg_distance_cm field in /level.

    Each entry may set `emit: false` (default true) to keep that
    processor out of /level's `signals` section while still showing up
    in `processors` -- for processors that only exist as an intermediate
    step (e.g. a median stage feeding a chain) and aren't a meaningful
    output on their own.

    A `type: chain` entry's `params.steps` runs a value through multiple
    processors in sequence. Each step is either:
      - `{ref: <name>}` — builds a fresh instance using the type/params
        of the processor already defined earlier in this file under that
        name. This is a config alias, not a shared live instance: every
        processor always gets its own independent state, so the same
        name can be reused in multiple places without one throwing off
        another's window.
      - `{type: ..., params: ...}` — builds a fresh instance directly,
        recursively (so a step can itself be a chain).
    """
    processor_types = discover_signal_processor_types()

    with open(path) as f:
        config = yaml.safe_load(f)

    entries = (config or {}).get("processors")
    if not entries:
        raise ValueError(f"{path}: 'processors' must be a non-empty list")

    processors = {}
    entries_by_name = {}
    emit_flags = {}
    primary_name = None

    for entry in entries:
        name = entry.get("name")

        if not name:
            raise ValueError(f"{path}: processor entry is missing 'name': {entry}")
        if name in processors:
            raise ValueError(f"{path}: duplicate processor name '{name}'")

        processors[name] = _build_processor(entry, processor_types, entries_by_name, path, f"processor '{name}'")
        entries_by_name[name] = entry
        emit_flags[name] = entry.get("emit", True)

        if entry.get("primary", False):
            if primary_name is not None:
                raise ValueError(f"{path}: multiple processors marked primary ('{primary_name}' and '{name}')")
            primary_name = name

    if primary_name is None:
        raise ValueError(f"{path}: exactly one processor must be marked 'primary: true'")

    return processors, primary_name, emit_flags


def _build_processor(entry, processor_types, entries_by_name, path, label):
    """Builds one LevelSignalProcessor instance from a config entry --
    either a top-level processor or a nested chain step. `label`
    identifies the entry in error messages. Always returns a fresh
    instance, even for `ref:` entries (see load_signal_processors).

    `entries_by_name` only contains entries defined earlier in the file
    than the one currently being built (see load_signal_processors's
    loop), so a `ref:` can only point backwards -- this rules out
    self-reference and cycles without any separate cycle-detection code.
    """
    ref = entry.get("ref")
    if ref is not None:
        if ref not in entries_by_name:
            raise ValueError(
                f"{path}: {label} references undefined processor '{ref}' (must be defined earlier in the file)"
            )
        return _build_processor(entries_by_name[ref], processor_types, entries_by_name, path, f"{label} (ref '{ref}')")

    processor_type = entry.get("type")
    if processor_type not in processor_types:
        raise ValueError(
            f"{path}: {label} has unknown type '{processor_type}' (expected one of {sorted(processor_types)})"
        )

    params = dict(entry.get("params") or {})

    if processor_type == "chain":
        steps = params.get("steps") or []
        if not steps:
            raise ValueError(f"{path}: {label} (chain) must have at least one step in 'params.steps'")
        params["steps"] = [
            (
                step.get("ref") or step.get("type"),
                _build_processor(step, processor_types, entries_by_name, path, f"{label} step {index + 1}"),
            )
            for index, step in enumerate(steps)
        ]

    try:
        return processor_types[processor_type](**params)
    except TypeError as e:
        raise ValueError(f"{path}: {label} has invalid params for type '{processor_type}': {e}") from e
