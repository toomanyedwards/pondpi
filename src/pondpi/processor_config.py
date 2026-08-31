import yaml

from pondpi.processors import discover_processor_types


def load_processors(path):
    """Loads named LevelProcessor instances from a YAML config file.

    Returns (dict[name -> LevelProcessor instance], primary_name). Exactly
    one entry must be marked `primary: true` — its output backfills the
    legacy top-level rolling_avg_distance_cm field in /level.
    """
    processor_types = discover_processor_types()

    with open(path) as f:
        config = yaml.safe_load(f)

    entries = (config or {}).get("processors")
    if not entries:
        raise ValueError(f"{path}: 'processors' must be a non-empty list")

    processors = {}
    primary_name = None

    for entry in entries:
        name = entry.get("name")
        processor_type = entry.get("type")

        if not name:
            raise ValueError(f"{path}: processor entry is missing 'name': {entry}")
        if name in processors:
            raise ValueError(f"{path}: duplicate processor name '{name}'")
        if processor_type not in processor_types:
            raise ValueError(
                f"{path}: processor '{name}' has unknown type '{processor_type}' "
                f"(expected one of {sorted(processor_types)})"
            )

        params = entry.get("params") or {}
        try:
            processors[name] = processor_types[processor_type](**params)
        except TypeError as e:
            raise ValueError(f"{path}: processor '{name}' has invalid params for type '{processor_type}': {e}") from e

        if entry.get("primary", False):
            if primary_name is not None:
                raise ValueError(f"{path}: multiple processors marked primary ('{primary_name}' and '{name}')")
            primary_name = name

    if primary_name is None:
        raise ValueError(f"{path}: exactly one processor must be marked 'primary: true'")

    return processors, primary_name
